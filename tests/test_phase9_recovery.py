#!/usr/bin/env python3
"""
Tests for Phase 9: Session Monitoring & Recovery.

Covers the Phase 9 acceptance criteria:
- Mid-run, a platform's session expires → that platform pauses, others continue
- The user reconnects → browser opens to login → the platform shows Connected
- Work queued for that platform resumes

Plus the property that keeps a challenge from becoming a ban: a paused platform
stays paused until someone actually resolves it, and the agent never decides on
its own that a CAPTCHA passed.
"""

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from job_agent.models.database import (
    Application,
    ApplicationStatus,
    AuditAction,
    AuditLog,
    ConnectionStatus,
    Job,
    PlatformAccount,
    PlatformInterruption,
)
from job_agent.services.interruption_detector import Interruption, InterruptionKind
from job_agent.services.session_monitor import SessionMonitor
from job_agent.utils.dates import utcnow


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def session():
    """In-memory database."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)

    with Session(engine) as db_session:
        yield db_session


@pytest.fixture
def monitor(session) -> SessionMonitor:
    """A session monitor."""
    return SessionMonitor(session)


def make_account(session, platform="greenhouse", **overrides) -> PlatformAccount:
    """Create a connected platform account."""
    defaults = dict(
        platform=platform,
        profile_dir=f"/tmp/job-agent-test/{platform}",
        status=ConnectionStatus.CONNECTED,
        daily_search_limit=50,
        daily_apply_limit=5,
        # A per-company board platform is skipped until it knows whose jobs
        # to read; these accounts stand for ones already pointed at a board.
        search_url=f"https://{platform}.test/board",
    )
    defaults.update(overrides)

    account = PlatformAccount(**defaults)
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


def captcha(url="https://board.test/jobs") -> Interruption:
    """A detected CAPTCHA."""
    return Interruption(
        kind=InterruptionKind.CAPTCHA,
        url=url,
        evidence="matched div.g-recaptcha",
        guidance="Complete the challenge yourself in the open browser window.",
    )


def patch_session_check(monkeypatch, healthy: bool, reason: str = "still blocked"):
    """Make SessionMonitor.check_session report a fixed verdict."""
    async def fake_check(self, account):
        return {
            "healthy": healthy,
            "reason": None if healthy else reason,
            "interruption": None,
        }

    monkeypatch.setattr(
        "job_agent.services.session_monitor.SessionMonitor.check_session", fake_check)


# ============================================================================
# Recording interruptions
# ============================================================================

class TestRecordingInterruptions:
    """An interruption is a task for the user, not a log line."""

    def test_recording_pauses_the_platform(self, monitor, session):
        account = make_account(session)

        record = monitor.record(account, captcha())

        assert record.id is not None
        assert record.is_open
        session.refresh(account)
        assert account.status == ConnectionStatus.SESSION_EXPIRED
        assert "captcha" in account.last_error

    def test_it_survives_the_run_that_hit_it(self, monitor, session):
        """The record outlives the run — that's the point of persisting it."""
        account = make_account(session)
        monitor.record(account, captcha())

        assert session.query(PlatformInterruption).count() == 1
        assert len(monitor.open_interruptions("greenhouse")) == 1

    def test_duplicate_interruptions_are_not_stacked(self, monitor, session):
        """Three CAPTCHAs is one thing for the user to do, not three."""
        account = make_account(session)

        first = monitor.record(account, captcha())
        second = monitor.record(account, captcha())

        assert first.id == second.id
        assert session.query(PlatformInterruption).count() == 1

    def test_different_kinds_are_separate(self, monitor, session):
        account = make_account(session)

        monitor.record(account, captcha())
        monitor.record(account, Interruption(
            kind=InterruptionKind.RATE_LIMITED, url="u", evidence="e", guidance="slow down"))

        assert len(monitor.open_interruptions("greenhouse")) == 2

    def test_recording_is_audited(self, monitor, session):
        account = make_account(session)
        monitor.record(account, captcha())

        entry = session.query(AuditLog).filter(
            AuditLog.action == AuditAction.INTERRUPTION_RAISED).one()

        assert entry.result == "paused"
        assert "captcha" in entry.detail

    def test_interruptions_are_scoped_per_platform(self, monitor, session):
        make_account(session, "greenhouse")
        indeed = make_account(session, "indeed")

        monitor.record(indeed, captcha())

        assert len(monitor.open_interruptions("indeed")) == 1
        assert len(monitor.open_interruptions("greenhouse")) == 0


# ============================================================================
# Health reporting
# ============================================================================

class TestHealth:
    """What the dashboard shows."""

    def test_a_clean_platform_is_healthy(self, monitor, session):
        make_account(session)

        health = monitor.health()[0]

        assert health.healthy is True
        assert health.needs_reconnect is False
        assert health.open_interruptions == []

    def test_an_interrupted_platform_needs_reconnect(self, monitor, session):
        account = make_account(session)
        monitor.record(account, captcha())

        health = monitor.health()[0]

        assert health.healthy is False
        assert health.needs_reconnect is True
        assert "Complete the challenge yourself" in health.reason

    @pytest.mark.parametrize("status", [
        ConnectionStatus.SESSION_EXPIRED,
        ConnectionStatus.NEEDS_SIGNIN,
        ConnectionStatus.ERROR,
    ])
    def test_blocked_statuses_are_unhealthy(self, monitor, session, status):
        make_account(session, status=status)

        assert monitor.health()[0].healthy is False

    def test_health_is_per_platform(self, monitor, session):
        make_account(session, "greenhouse")
        indeed = make_account(session, "indeed")
        monitor.record(indeed, captcha())

        health = {h.platform: h for h in monitor.health()}

        assert health["greenhouse"].healthy is True
        assert health["indeed"].healthy is False

    def test_pending_jobs_are_counted(self, monitor, session):
        """The work waiting for a platform to come back."""
        account = make_account(session)

        for i in range(3):
            session.add(Job(
                platform="greenhouse", external_id=str(i), title="Backend Engineer",
                company=f"C{i}", location="Remote", description="d",
                apply_method="web_form", dedup_hash=f"h{i}",
                hard_filter_pass=True, fit_score=0.9,
            ))
        session.commit()

        assert monitor.health_for(account).pending_jobs == 3

    def test_applied_jobs_are_not_pending(self, monitor, session):
        account = make_account(session)

        job = Job(
            platform="greenhouse", external_id="1", title="Backend Engineer",
            company="C", location="Remote", description="d", apply_method="web_form",
            dedup_hash="h", hard_filter_pass=True, fit_score=0.9,
        )
        session.add(job)
        session.commit()
        session.refresh(job)

        session.add(Application(
            job_id=job.id, platform_account_id=account.id,
            submission_status=ApplicationStatus.QUEUED_FOR_REVIEW,
        ))
        session.commit()

        assert monitor.health_for(account).pending_jobs == 0


# ============================================================================
# Resolution
# ============================================================================

@pytest.mark.asyncio
class TestResolution:
    """Closing an interruption needs evidence."""

    async def test_resolution_is_verified_by_default(self, monitor, session, monkeypatch):
        account = make_account(session)
        record = monitor.record(account, captcha())

        patch_session_check(monkeypatch, healthy=True)

        result = await monitor.resolve(record)

        assert result["resolved"] is True
        assert result["verified"] is True

        session.refresh(record)
        assert not record.is_open

    async def test_resolution_is_refused_while_still_blocked(
        self, monitor, session, monkeypatch
    ):
        """
        A user who says "I've done it" but hasn't would otherwise send the next
        run straight back into the same wall.
        """
        account = make_account(session)
        record = monitor.record(account, captcha())

        patch_session_check(monkeypatch, healthy=False, reason="captcha still showing")

        result = await monitor.resolve(record)

        assert result["resolved"] is False
        assert "still looks blocked" in result["message"]

        session.refresh(record)
        assert record.is_open

    async def test_verification_can_be_overridden(self, monitor, session, monkeypatch):
        account = make_account(session)
        record = monitor.record(account, captcha())

        patch_session_check(monkeypatch, healthy=False)

        result = await monitor.resolve(record, verify=False)

        assert result["resolved"] is True
        assert result["verified"] is False
        assert "without verification" in record.resolution_note

    async def test_resolving_restores_the_platform(self, monitor, session, monkeypatch):
        account = make_account(session)
        record = monitor.record(account, captcha())
        patch_session_check(monkeypatch, healthy=True)

        await monitor.resolve(record)

        session.refresh(account)
        assert account.status == ConnectionStatus.CONNECTED
        assert account.last_error is None

    async def test_platform_stays_paused_while_another_is_open(
        self, monitor, session, monkeypatch
    ):
        account = make_account(session)
        first = monitor.record(account, captcha())
        monitor.record(account, Interruption(
            kind=InterruptionKind.MFA, url="u", evidence="e", guidance="enter your code"))

        patch_session_check(monkeypatch, healthy=True)

        result = await monitor.resolve(first)

        assert result["resolved"] is True
        assert "still open" in result["message"]

        session.refresh(account)
        assert account.status == ConnectionStatus.SESSION_EXPIRED

    async def test_resolution_is_audited(self, monitor, session, monkeypatch):
        account = make_account(session)
        record = monitor.record(account, captcha())
        patch_session_check(monkeypatch, healthy=True)

        await monitor.resolve(record, resolved_by="user")

        entry = session.query(AuditLog).filter(
            AuditLog.action == AuditAction.INTERRUPTION_RESOLVED).one()

        assert entry.actor == "user"


# ============================================================================
# Reconnect and resume
# ============================================================================

@pytest.mark.asyncio
class TestReconnectAndResume:
    """Acceptance: reconnect, then the platform works and its work resumes."""

    async def test_reconnect_opens_a_browser_without_credentials(
        self, monitor, session, monkeypatch
    ):
        account = make_account(session, status=ConnectionStatus.SESSION_EXPIRED)

        opened = {}

        class FakePage:
            url = "about:blank"

            async def goto(self, url, timeout=None):
                opened["url"] = url

        class FakeContext:
            pages = [FakePage()]

        class FakeManager:
            @staticmethod
            async def launch_browser_for_connection(platform):
                opened["platform"] = platform
                return FakeContext()

        async def fake_manager():
            return FakeManager()

        monkeypatch.setattr(
            "job_agent.core.session_manager.get_session_manager", fake_manager)

        result = await monitor.reconnect(account, login_url="https://board.test/login")

        assert result["opened"] is True
        assert opened["url"] == "https://board.test/login"
        assert "never enters credentials" in result["message"]

        session.refresh(account)
        assert account.status == ConnectionStatus.NEEDS_SIGNIN

    async def test_reconnect_failure_is_reported(self, monitor, session, monkeypatch):
        account = make_account(session)

        async def broken_manager():
            raise RuntimeError("no display")

        monkeypatch.setattr(
            "job_agent.core.session_manager.get_session_manager", broken_manager)

        result = await monitor.reconnect(account)

        assert result["opened"] is False
        assert "Could not open a browser" in result["message"]

    async def test_resume_clears_interruptions_and_reports_work(
        self, monitor, session, monkeypatch
    ):
        """Acceptance: tasks queued for that platform resume."""
        account = make_account(session)
        monitor.record(account, captcha())

        session.add(Job(
            platform="greenhouse", external_id="1", title="Backend Engineer",
            company="C", location="Remote", description="d", apply_method="web_form",
            dedup_hash="h", hard_filter_pass=True, fit_score=0.9,
        ))
        session.commit()

        patch_session_check(monkeypatch, healthy=True)

        result = await monitor.resume(account)

        assert result["resumed"] is True
        assert result["interruptions_closed"] == 1
        assert result["pending_jobs"] == 1
        assert "waiting" in result["next_step"]

        session.refresh(account)
        assert account.status == ConnectionStatus.CONNECTED
        assert monitor.open_interruptions("greenhouse") == []

    async def test_resume_refuses_while_still_blocked(self, monitor, session, monkeypatch):
        account = make_account(session)
        monitor.record(account, captcha())

        patch_session_check(monkeypatch, healthy=False, reason="captcha still showing")

        result = await monitor.resume(account)

        assert result["resumed"] is False
        assert "captcha still showing" in result["reason"]
        assert len(monitor.open_interruptions("greenhouse")) == 1

    async def test_resume_is_audited(self, monitor, session, monkeypatch):
        account = make_account(session)
        monitor.record(account, captcha())
        patch_session_check(monkeypatch, healthy=True)

        await monitor.resume(account)

        entry = session.query(AuditLog).filter(
            AuditLog.action == AuditAction.PLATFORM_RESUMED).one()

        assert "resumed" in entry.detail

    async def test_monitor_marks_a_recovered_platform_healthy(
        self, monitor, session, monkeypatch
    ):
        account = make_account(session, status=ConnectionStatus.SESSION_EXPIRED)
        patch_session_check(monkeypatch, healthy=True)

        results = await monitor.monitor()

        assert results[0]["healthy"] is True

        session.refresh(account)
        assert account.status == ConnectionStatus.CONNECTED
        assert account.last_verified_at is not None


# ============================================================================
# Interaction with the run loop
# ============================================================================

@pytest.mark.asyncio
class TestRunLoopIntegration:
    """A paused platform stays out of runs until it's resolved."""

    async def test_open_interruption_skips_the_platform(
        self, monitor, session, monkeypatch
    ):
        from job_agent.core.orchestrator import RunOrchestrator
        from job_agent.models.database import SearchProfile

        account = make_account(session)
        monitor.record(account, captcha())

        # A CONNECTED status must not be enough to run it
        account.status = ConnectionStatus.CONNECTED
        session.commit()

        profile = SearchProfile(name="p", target_titles=["Backend"], is_active=True)
        session.add(profile)
        session.commit()
        session.refresh(profile)

        async def never_called(self, acct, sp, connector=None):
            raise AssertionError("the pipeline ran on an interrupted platform")

        monkeypatch.setattr(
            "job_agent.core.search_pipeline.SearchPipeline.search", never_called)

        run = await RunOrchestrator(session).run(profile)

        assert run.platforms_run == []
        assert "captcha needs you" in run.platforms_skipped["greenhouse"]

    async def test_other_platforms_still_run(self, monitor, session, monkeypatch):
        """Acceptance: one platform pauses, others continue."""
        from job_agent.core.orchestrator import RunOrchestrator
        from job_agent.models.database import SearchProfile

        make_account(session, "greenhouse")
        indeed = make_account(session, "indeed")
        monitor.record(indeed, captcha())

        profile = SearchProfile(name="p", target_titles=["Backend"], is_active=True)
        session.add(profile)
        session.commit()
        session.refresh(profile)

        class Result:
            jobs_found, new_jobs, duplicates_skipped, hard_filters_failed = 10, 2, 1, 0
            errors, jobs_stored, limit_reached = [], [], False

        async def fake_search(self, acct, sp, connector=None):
            return Result()

        async def no_interruption(self, acct):
            return None

        monkeypatch.setattr(
            "job_agent.core.search_pipeline.SearchPipeline.search", fake_search)
        monkeypatch.setattr(
            "job_agent.core.orchestrator.RunOrchestrator._check_for_interruption",
            no_interruption)

        run = await RunOrchestrator(session).run(profile)

        assert run.platforms_run == ["greenhouse"]
        assert run.jobs_found == 10
        assert "indeed" in run.platforms_skipped

    async def test_resolving_lets_the_platform_run_again(
        self, monitor, session, monkeypatch
    ):
        from job_agent.core.orchestrator import RunOrchestrator
        from job_agent.models.database import SearchProfile

        account = make_account(session)
        record = monitor.record(account, captcha())

        profile = SearchProfile(name="p", target_titles=["Backend"], is_active=True)
        session.add(profile)
        session.commit()
        session.refresh(profile)

        patch_session_check(monkeypatch, healthy=True)
        await monitor.resolve(record)

        class Result:
            jobs_found, new_jobs, duplicates_skipped, hard_filters_failed = 5, 5, 0, 0
            errors, jobs_stored, limit_reached = [], [], False

        async def fake_search(self, acct, sp, connector=None):
            return Result()

        async def no_interruption(self, acct):
            return None

        monkeypatch.setattr(
            "job_agent.core.search_pipeline.SearchPipeline.search", fake_search)
        monkeypatch.setattr(
            "job_agent.core.orchestrator.RunOrchestrator._check_for_interruption",
            no_interruption)

        run = await RunOrchestrator(session).run(profile)

        assert run.platforms_run == ["greenhouse"]
        assert run.jobs_found == 5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
