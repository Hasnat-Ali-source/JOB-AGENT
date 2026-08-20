#!/usr/bin/env python3
"""
Tests for Phase 8: Scheduling & Automation Loop.

Covers the Phase 8 acceptance criteria:
- A run iterates platforms, respects daily limits, and writes a summary
- The summary reports jobs found / new / duplicates / queued
- Schedules can be installed at 9 AM and 6 PM

Plus the properties that matter for an unattended loop: a scheduled run never
submits, one platform's failure doesn't end the run, and hitting a CAPTCHA
pauses that platform instead of retrying into it.
"""

import plistlib
from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from job_agent.core.orchestrator import RunOrchestrator
from job_agent.core.scheduler import LaunchdScheduler, ScheduleTime
from job_agent.models.database import (
    AgentRun,
    AuditAction,
    AuditLog,
    ConnectionStatus,
    Job,
    PlatformAccount,
    RunStatus,
    SearchProfile,
)
from job_agent.services.interruption_detector import (
    InterruptionDetector,
    InterruptionKind,
)
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
def profile(session) -> SearchProfile:
    """An active search profile."""
    record = SearchProfile(
        name="Backend - Remote", target_titles=["Backend Engineer"],
        remote_pref="remote", is_active=True,
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


def make_account(session, platform: str, **overrides) -> PlatformAccount:
    """Create a connected platform account."""
    defaults = dict(
        platform=platform,
        profile_dir=f"/tmp/job-agent-test/{platform}",
        status=ConnectionStatus.CONNECTED,
        daily_search_limit=50,
        daily_apply_limit=5,
        # Greenhouse, Lever and the rest host one board per company, so a
        # station on them is skipped until it has been told whose jobs to
        # read. These accounts stand for ones that have been.
        search_url=f"https://{platform}.test/board",
    )
    defaults.update(overrides)

    account = PlatformAccount(**defaults)
    session.add(account)
    session.commit()
    session.refresh(account)
    return account


class FakeSearchResult:
    """Stands in for a SearchPipeline result."""

    def __init__(self, found=0, new=0, duplicates=0, filtered=0, errors=None, jobs=None):
        self.jobs_found = found
        self.new_jobs = new
        self.duplicates_skipped = duplicates
        self.hard_filters_failed = filtered
        self.limit_reached = False
        self.jobs_stored = jobs or []
        self.errors = errors or []


def patch_pipeline(monkeypatch, results: dict):
    """
    Make SearchPipeline.search return canned results per platform.

    Args:
        results: {platform: FakeSearchResult or Exception}
    """
    async def fake_search(self, account, search_profile, connector=None):
        outcome = results.get(account.platform, FakeSearchResult())

        if isinstance(outcome, Exception):
            raise outcome

        return outcome

    monkeypatch.setattr(
        "job_agent.core.search_pipeline.SearchPipeline.search", fake_search)


@pytest.fixture(autouse=True)
def no_interruption_checks(monkeypatch):
    """
    Skip the live-browser interruption check by default.

    It needs a SessionManager and a real page; tests that care about it patch
    it back explicitly.
    """
    async def none(self, account):
        return None

    monkeypatch.setattr(
        "job_agent.core.orchestrator.RunOrchestrator._check_for_interruption", none)


# ============================================================================
# The run loop
# ============================================================================

@pytest.mark.asyncio
class TestRunLoop:
    """Acceptance: iterate platforms, aggregate, summarize."""

    async def test_run_aggregates_across_platforms(self, session, profile, monkeypatch):
        make_account(session, "greenhouse")
        make_account(session, "lever")

        patch_pipeline(monkeypatch, {
            "greenhouse": FakeSearchResult(found=30, new=2, duplicates=1),
            "lever": FakeSearchResult(found=12, new=1, duplicates=1),
        })

        run = await RunOrchestrator(session).run(profile)

        assert run.jobs_found == 42
        assert run.new_jobs == 3
        assert run.duplicates_skipped == 2
        assert set(run.platforms_run) == {"greenhouse", "lever"}
        assert run.status == RunStatus.COMPLETED

    async def test_summary_reads_like_the_spec(self, session, profile, monkeypatch):
        """Acceptance: "42 jobs found, 3 new, 2 duplicates..."."""
        make_account(session, "greenhouse")
        patch_pipeline(monkeypatch, {
            "greenhouse": FakeSearchResult(found=42, new=3, duplicates=2),
        })

        run = await RunOrchestrator(session).run(profile)
        summary = RunOrchestrator.summarize(run)

        assert "42 jobs found" in summary
        assert "3 new" in summary
        assert "2 duplicates" in summary

    async def test_run_never_submits(self, session, profile, monkeypatch):
        """
        A scheduled run happens while the user is asleep. Preparing work is
        allowed; sending it to an employer is not.
        """
        make_account(session, "greenhouse")
        patch_pipeline(monkeypatch, {"greenhouse": FakeSearchResult(found=5, new=5)})

        run = await RunOrchestrator(session).run(profile)

        assert run.applications_submitted == 0

    async def test_run_is_recorded_with_timing(self, session, profile, monkeypatch):
        make_account(session, "greenhouse")
        patch_pipeline(monkeypatch, {"greenhouse": FakeSearchResult(found=1)})

        run = await RunOrchestrator(session).run(profile)

        assert run.id is not None
        assert run.finished_at is not None
        assert run.duration_seconds is not None
        assert session.query(AgentRun).count() == 1

    async def test_run_start_and_finish_are_audited(self, session, profile, monkeypatch):
        make_account(session, "greenhouse")
        patch_pipeline(monkeypatch, {"greenhouse": FakeSearchResult(found=1)})

        await RunOrchestrator(session).run(profile)

        actions = [
            entry.action for entry in session.query(AuditLog).all()
        ]

        assert AuditAction.RUN_STARTED in actions
        assert AuditAction.RUN_COMPLETED in actions

    async def test_platforms_can_be_restricted(self, session, profile, monkeypatch):
        make_account(session, "greenhouse")
        make_account(session, "lever")
        patch_pipeline(monkeypatch, {
            "greenhouse": FakeSearchResult(found=5),
            "lever": FakeSearchResult(found=5),
        })

        run = await RunOrchestrator(session).run(profile, platforms=["lever"])

        assert run.platforms_run == ["lever"]
        assert run.jobs_found == 5

    async def test_trigger_is_recorded(self, session, profile, monkeypatch):
        make_account(session, "greenhouse")
        patch_pipeline(monkeypatch, {"greenhouse": FakeSearchResult()})

        run = await RunOrchestrator(session).run(profile, trigger="scheduled")

        assert run.trigger == "scheduled"


@pytest.mark.asyncio
class TestPlatformIsolation:
    """One platform's problem must not end the run."""

    async def test_a_failing_platform_does_not_stop_the_others(
        self, session, profile, monkeypatch
    ):
        make_account(session, "greenhouse")
        make_account(session, "indeed")

        patch_pipeline(monkeypatch, {
            "greenhouse": FakeSearchResult(found=40, new=4),
            "indeed": RuntimeError("page crashed"),
        })

        run = await RunOrchestrator(session).run(profile)

        assert "greenhouse" in run.platforms_run
        assert run.jobs_found == 40
        assert any("indeed" in error for error in run.errors)
        assert run.status == RunStatus.PARTIAL

    async def test_errors_from_the_pipeline_are_collected(
        self, session, profile, monkeypatch
    ):
        make_account(session, "greenhouse")
        patch_pipeline(monkeypatch, {
            "greenhouse": FakeSearchResult(found=3, errors=["one posting failed"]),
        })

        run = await RunOrchestrator(session).run(profile)

        assert run.errors == ["one posting failed"]
        assert run.status == RunStatus.PARTIAL


@pytest.mark.asyncio
class TestQueueingNeedsABrowser:
    """
    Which platforms have to be signed into before a form can be filled.

    Preparing an application on a public Greenhouse board reported "no
    authenticated browser session" — a sign-in that board neither has nor
    wants. The search path had been fixed for this; the filling path asked for
    a signed-in session on every platform alike.
    """

    @pytest.fixture
    def candidate(self, session):
        """A candidate profile — without one, filling stops before the browser."""
        from job_agent.models.database import CandidateProfile

        record = CandidateProfile(
            full_name="Alex Rivera", email="alex@example.com", is_active=True
        )
        session.add(record)
        session.commit()
        return record

    async def test_a_public_board_asks_for_a_browser_without_a_signin(
        self, session, profile, candidate, monkeypatch
    ):
        account = make_account(session, "greenhouse")
        asked = {}

        class FakeSessionManager:
            async def get_page(self, platform, needs_signin=True):
                asked["platform"] = platform
                asked["needs_signin"] = needs_signin
                return None  # Stop before a browser is needed

        async def fake_manager():
            return FakeSessionManager()

        monkeypatch.setattr(
            "job_agent.core.session_manager.get_session_manager", fake_manager)

        from job_agent.core.orchestrator import PlatformOutcome, RunOrchestrator

        outcome = PlatformOutcome(platform="greenhouse")
        job = Job(
            platform="greenhouse", external_id="1", title="Backend Engineer",
            company="GitLab", location="Remote", description="Python, Go",
            apply_method="web_form", dedup_hash="h1",
        )
        session.add(job)
        session.commit()

        await RunOrchestrator(session)._queue_applications(account, [job], outcome)

        assert asked == {"platform": "greenhouse", "needs_signin": False}

    async def test_a_platform_behind_a_login_still_needs_one(
        self, session, profile, candidate, monkeypatch
    ):
        account = make_account(session, "greenhouse", requires_signin=True)
        asked = {}

        class FakeSessionManager:
            async def get_page(self, platform, needs_signin=True):
                asked["needs_signin"] = needs_signin
                return None

        async def fake_manager():
            return FakeSessionManager()

        monkeypatch.setattr(
            "job_agent.core.session_manager.get_session_manager", fake_manager)

        from job_agent.core.orchestrator import PlatformOutcome, RunOrchestrator

        outcome = PlatformOutcome(platform="greenhouse")
        job = Job(
            platform="greenhouse", external_id="1", title="Backend Engineer",
            company="GitLab", location="Remote", description="Python, Go",
            apply_method="web_form", dedup_hash="h1",
        )
        session.add(job)
        session.commit()

        await RunOrchestrator(session)._queue_applications(account, [job], outcome)

        assert asked["needs_signin"] is True
        assert any("connect it under Stations" in e for e in outcome.errors)


@pytest.mark.asyncio
class TestSkipping:
    """Platforms that shouldn't run, and why."""

    @pytest.mark.parametrize("status,fragment", [
        (ConnectionStatus.SESSION_EXPIRED, "session expired"),
        (ConnectionStatus.NEEDS_SIGNIN, "not signed in"),
        (ConnectionStatus.DISABLED, "disabled"),
        (ConnectionStatus.ERROR, "error state"),
    ])
    async def test_unusable_platforms_are_skipped_with_a_reason(
        self, session, profile, monkeypatch, status, fragment
    ):
        make_account(session, "greenhouse", status=status)
        patch_pipeline(monkeypatch, {"greenhouse": FakeSearchResult(found=5)})

        run = await RunOrchestrator(session).run(profile)

        assert run.platforms_run == []
        assert fragment in run.platforms_skipped["greenhouse"]

    async def test_daily_search_limit_skips_a_platform(
        self, session, profile, monkeypatch
    ):
        """Acceptance: each run respects daily_search_limit."""
        account = make_account(session, "greenhouse", daily_search_limit=10)

        # An earlier run today already used the budget
        session.add(AuditLog(
            timestamp=utcnow(), action=AuditAction.SEARCH_RUN,
            platform="greenhouse", actor="agent", detail="earlier run",
            detail_json={"jobs_found": 10}, result="success",
        ))
        session.commit()

        patch_pipeline(monkeypatch, {"greenhouse": FakeSearchResult(found=5)})

        run = await RunOrchestrator(session).run(profile)

        assert "daily search limit reached" in run.platforms_skipped["greenhouse"]

    async def test_skips_are_audited(self, session, profile, monkeypatch):
        make_account(session, "greenhouse", status=ConnectionStatus.SESSION_EXPIRED)
        patch_pipeline(monkeypatch, {})

        await RunOrchestrator(session).run(profile)

        entry = session.query(AuditLog).filter(
            AuditLog.action == AuditAction.PLATFORM_SKIPPED).one()

        assert entry.result == "paused"

    async def test_a_run_with_only_skips_is_a_failure(
        self, session, profile, monkeypatch
    ):
        make_account(session, "greenhouse", status=ConnectionStatus.DISABLED)
        patch_pipeline(monkeypatch, {})

        run = await RunOrchestrator(session).run(profile)

        assert run.status == RunStatus.FAILED

    async def test_unknown_connector_is_skipped(self, session, profile, monkeypatch):
        make_account(session, "myspace")
        patch_pipeline(monkeypatch, {})

        run = await RunOrchestrator(session).run(profile)

        assert "no connector registered" in run.platforms_skipped["myspace"]


# ============================================================================
# Interruption handling
# ============================================================================

class TestInterruptionDetection:
    """CAPTCHA, MFA and sign-in walls must be recognized, never solved."""

    class FakePage:
        """Minimal page stand-in."""

        def __init__(self, text="", selectors=None, url="https://board.test/jobs"):
            self.url = url
            self._text = text
            self._selectors = selectors or set()

        def locator(self, selector):
            page = self

            class Locator:
                async def count(self):
                    return 1 if selector in page._selectors else 0

            return Locator()

        async def inner_text(self, _selector):
            return self._text

    @pytest.mark.asyncio
    async def test_recaptcha_challenge_is_detected(self):
        page = self.FakePage(
            selectors={"iframe[src*='recaptcha'][src*='bframe']"}
        )

        interruption = await InterruptionDetector.detect(page)

        assert interruption.kind == InterruptionKind.CAPTCHA
        assert interruption.halts_platform

    @pytest.mark.asyncio
    async def test_recaptcha_badge_is_not_a_challenge(self):
        """
        An ATS embeds an invisible reCAPTCHA on every application form. Its
        badge is not something the user can solve, and treating it as a
        challenge paused the station on every form the agent ever opened.
        """
        page = self.FakePage(selectors={"iframe[src*='recaptcha']"})

        assert await InterruptionDetector.detect(page) is None

    @pytest.mark.asyncio
    async def test_captcha_guidance_never_offers_to_solve_it(self):
        page = self.FakePage(selectors={"div.g-recaptcha"})

        interruption = await InterruptionDetector.detect(page)

        assert "yourself" in interruption.guidance
        assert "never solve" in interruption.guidance.lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text,kind", [
        ("Please verify you are human to continue", InterruptionKind.CAPTCHA),
        ("Enter the 6-digit code from your authenticator app", InterruptionKind.MFA),
        ("Your session has expired. Please sign in.", InterruptionKind.SIGNIN_REQUIRED),
        ("Too many requests. Try again later.", InterruptionKind.RATE_LIMITED),
        ("Access denied — suspicious activity detected", InterruptionKind.BLOCKED),
    ])
    async def test_wording_is_detected(self, text, kind):
        interruption = await InterruptionDetector.detect(self.FakePage(text=text))

        assert interruption is not None
        assert interruption.kind == kind

    @pytest.mark.asyncio
    async def test_an_ordinary_board_is_not_flagged(self):
        page = self.FakePage(
            text="Senior Backend Engineer. Remote. Apply now. 42 open roles.")

        assert await InterruptionDetector.detect(page) is None

    @pytest.mark.asyncio
    async def test_a_login_widget_alone_is_not_a_wall(self):
        """
        Job boards show a sign-in box beside their results. A password field is
        only a wall when the page says so.
        """
        page = self.FakePage(
            text="Senior Backend Engineer. 42 open roles.",
            selectors={"input[type='password']"},
        )

        assert await InterruptionDetector.detect(page) is None

    @pytest.mark.asyncio
    async def test_interruption_serializes_for_the_run_record(self):
        interruption = await InterruptionDetector.detect(
            self.FakePage(selectors={"div.h-captcha"}))

        data = interruption.to_dict()

        assert data["kind"] == "captcha"
        assert data["halts_platform"] is True
        assert data["guidance"]


@pytest.mark.asyncio
class TestInterruptionsInARun:
    """A wall pauses its platform rather than being retried into."""

    async def test_interruption_pauses_the_platform_and_flags_the_run(
        self, session, profile, monkeypatch
    ):
        account = make_account(session, "indeed")
        patch_pipeline(monkeypatch, {"indeed": FakeSearchResult(found=0)})

        async def captcha(self, acct):
            acct.status = ConnectionStatus.SESSION_EXPIRED
            acct.last_error = "captcha: complete it yourself"
            self.db_session.commit()
            return {
                "kind": "captcha", "url": "https://indeed.test",
                "evidence": "matched div.g-recaptcha",
                "guidance": "Complete the challenge yourself",
                "halts_platform": True, "screenshot_path": None,
            }

        monkeypatch.setattr(
            "job_agent.core.orchestrator.RunOrchestrator._check_for_interruption", captcha)

        run = await RunOrchestrator(session).run(profile)

        assert len(run.interruptions) == 1
        assert run.interruptions[0]["kind"] == "captcha"
        assert run.status == RunStatus.PARTIAL

        session.refresh(account)
        assert account.status == ConnectionStatus.SESSION_EXPIRED

    async def test_a_paused_platform_is_skipped_next_run(
        self, session, profile, monkeypatch
    ):
        """This is what stops a retry loop into a challenge."""
        make_account(session, "indeed", status=ConnectionStatus.SESSION_EXPIRED)
        patch_pipeline(monkeypatch, {"indeed": FakeSearchResult(found=5)})

        run = await RunOrchestrator(session).run(profile)

        assert run.platforms_run == []
        assert "session expired" in run.platforms_skipped["indeed"]


# ============================================================================
# launchd scheduling
# ============================================================================

class TestScheduleTimes:
    """Parsing and validating run times."""

    def test_parses_hour_and_minute(self):
        assert str(ScheduleTime.parse("09:00")) == "09:00"
        assert str(ScheduleTime.parse("18:30")) == "18:30"

    def test_parses_bare_hour(self):
        assert str(ScheduleTime.parse("9")) == "09:00"

    @pytest.mark.parametrize("value", ["25:00", "09:99", "nine", ""])
    def test_rejects_nonsense(self, value):
        with pytest.raises(ValueError):
            ScheduleTime.parse(value)


class TestLaunchdScheduler:
    """Installing, enabling and reporting the schedule."""

    @pytest.fixture
    def scheduler(self, tmp_path) -> LaunchdScheduler:
        """A scheduler writing into a temp LaunchAgents directory."""
        return LaunchdScheduler(
            label="net.jobagent.test", agents_dir=tmp_path / "LaunchAgents",
            project_dir=tmp_path / "project",
        )

    def test_install_writes_a_valid_plist(self, scheduler):
        """Acceptance: schedule a daily search at 9 AM and 6 PM."""
        path = scheduler.install(
            [ScheduleTime(9, 0), ScheduleTime(18, 0)], profile_name="Backend")

        assert path.exists()

        with open(path, "rb") as handle:
            plist = plistlib.load(handle)

        assert plist["Label"] == "net.jobagent.test"
        assert plist["StartCalendarInterval"] == [
            {"Hour": 9, "Minute": 0}, {"Hour": 18, "Minute": 0}]
        assert "--scheduled" in plist["ProgramArguments"]
        assert "Backend" in plist["ProgramArguments"]

    def test_installing_does_not_enable(self, scheduler):
        """Writing a config file must never be what starts an agent."""
        scheduler.install([ScheduleTime(9)])

        status = scheduler.status()

        assert status["installed"] is True
        assert status["enabled"] is False

    def test_does_not_run_at_load(self, scheduler):
        """A missed run must not fire the moment the Mac wakes."""
        scheduler.install([ScheduleTime(9)])

        assert scheduler.status()["runs_at_load"] is False

    def test_scheduled_times_are_read_back(self, scheduler):
        scheduler.install([ScheduleTime(9, 15), ScheduleTime(18, 45)])

        assert [str(t) for t in scheduler.scheduled_times()] == ["09:15", "18:45"]

    def test_profile_is_read_back(self, scheduler):
        scheduler.install([ScheduleTime(9)], profile_name="Backend - Remote")

        assert scheduler.status()["profile"] == "Backend - Remote"

    def test_status_before_install(self, scheduler):
        status = scheduler.status()

        assert status["installed"] is False
        assert status["enabled"] is False
        assert status["times"] == []

    def test_empty_schedule_is_rejected(self, scheduler):
        with pytest.raises(ValueError, match="at least one run time"):
            scheduler.build_plist([])

    def test_uninstall_removes_the_plist(self, scheduler):
        scheduler.install([ScheduleTime(9)])
        assert scheduler.plist_path.exists()

        success, _ = scheduler.uninstall()

        assert success
        assert not scheduler.plist_path.exists()

    def test_enable_without_install_fails_clearly(self, scheduler):
        success, message = scheduler.enable()

        assert not success
        assert "No schedule is installed" in message

    def test_the_command_uses_the_current_interpreter(self, scheduler):
        """A venv install must keep working from launchd."""
        import sys

        plist = scheduler.build_plist([ScheduleTime(9)])

        assert plist["ProgramArguments"][0] == sys.executable
        assert plist["ProgramArguments"][1:4] == ["-m", "job_agent", "run"]

    def test_package_entry_point_exists(self):
        """
        The scheduled command is `python -m job_agent`, which needs
        job_agent/__main__.py — without it every scheduled run fails silently
        in a log file.
        """
        import job_agent

        assert (Path(job_agent.__file__).parent / "__main__.py").exists()

    def test_scheduled_command_actually_runs(self):
        """
        Run the exact command launchd will run, with --help.

        Asserting the file exists isn't enough: an import error inside the CLI
        would still fail at 9 AM. This invokes the real entry point in a
        subprocess and checks it starts.
        """
        import subprocess
        import sys

        completed = subprocess.run(
            [sys.executable, "-m", "job_agent", "run", "--help"],
            capture_output=True, text=True, timeout=60,
        )

        assert completed.returncode == 0, completed.stderr
        assert "--scheduled" in completed.stdout


class TestLaunchdIntegration:
    """
    The launchctl lifecycle, against the real launchd.

    These load a plist whose program is /usr/bin/true rather than the agent:
    the point is to prove enable/disable/is_loaded work against the real
    system, and loading the actual agent would schedule it to start applying
    for jobs on this machine. The real plist is validated separately with
    plutil.
    """

    @pytest.fixture
    def noop_scheduler(self, tmp_path):
        """A scheduler wired to a harmless program, cleaned up afterwards."""
        import platform

        if platform.system() != "Darwin":
            pytest.skip("launchd is macOS-only")

        scheduler = LaunchdScheduler(
            label="net.jobagent.test.integration",
            agents_dir=tmp_path / "LaunchAgents",
            project_dir=tmp_path,
        )

        yield scheduler

        # Never leave a loaded job behind
        scheduler.uninstall()

    def test_generated_plist_passes_plutil(self, tmp_path):
        """The real plist must be something launchd can parse."""
        import platform
        import subprocess

        if platform.system() != "Darwin":
            pytest.skip("plutil is macOS-only")

        scheduler = LaunchdScheduler(
            label="net.jobagent.test.lint",
            agents_dir=tmp_path / "LaunchAgents",
            project_dir=tmp_path,
        )
        path = scheduler.install(
            [ScheduleTime(9, 0), ScheduleTime(18, 0)], profile_name="Backend")

        completed = subprocess.run(
            ["plutil", "-lint", str(path)], capture_output=True, text=True, timeout=30)

        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "OK" in completed.stdout

    def test_enable_disable_lifecycle(self, noop_scheduler):
        """
        Load and unload a real launchd job.

        The program is /usr/bin/true and RunAtLoad is false, so nothing
        executes — but launchctl really does load, list and unload it.
        """
        import plistlib

        plist = noop_scheduler.build_plist([ScheduleTime(3, 0)])
        plist["ProgramArguments"] = ["/usr/bin/true"]

        noop_scheduler.agents_dir.mkdir(parents=True, exist_ok=True)
        with open(noop_scheduler.plist_path, "wb") as handle:
            plistlib.dump(plist, handle)

        assert noop_scheduler.is_loaded() is False

        enabled, message = noop_scheduler.enable()
        assert enabled, message
        assert noop_scheduler.is_loaded() is True
        assert noop_scheduler.status()["enabled"] is True

        disabled, message = noop_scheduler.disable()
        assert disabled, message
        assert noop_scheduler.is_loaded() is False

    def test_uninstall_unloads_and_removes(self, noop_scheduler):
        import plistlib

        plist = noop_scheduler.build_plist([ScheduleTime(3, 0)])
        plist["ProgramArguments"] = ["/usr/bin/true"]

        noop_scheduler.agents_dir.mkdir(parents=True, exist_ok=True)
        with open(noop_scheduler.plist_path, "wb") as handle:
            plistlib.dump(plist, handle)

        noop_scheduler.enable()
        success, _ = noop_scheduler.uninstall()

        assert success
        assert not noop_scheduler.plist_path.exists()
        assert noop_scheduler.is_loaded() is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
