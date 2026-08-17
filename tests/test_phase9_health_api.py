#!/usr/bin/env python3
"""
API tests for Phase 9: health and recovery endpoints.

This is what a dashboard's "Reconnect" button calls.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from job_agent.dashboard.deps import get_session
from job_agent.dashboard.main import app
from job_agent.models.database import (
    ConnectionStatus,
    Job,
    PlatformAccount,
    PlatformInterruption,
)
from job_agent.utils.dates import utcnow


@pytest.fixture
def client():
    """TestClient bound to an isolated database."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    app.dependency_overrides[get_session] = lambda: session

    with TestClient(app) as test_client:
        test_client.db = session  # type: ignore[attr-defined]
        yield test_client

    app.dependency_overrides.clear()
    session.close()


@pytest.fixture
def seeded(client):
    """One healthy platform and one blocked by a CAPTCHA."""
    session = client.db

    healthy = PlatformAccount(
        platform="greenhouse", profile_dir="/tmp/a",
        status=ConnectionStatus.CONNECTED, daily_apply_limit=5,
    )
    blocked = PlatformAccount(
        platform="indeed", profile_dir="/tmp/b",
        status=ConnectionStatus.SESSION_EXPIRED,
        last_error="captcha: complete it yourself", daily_apply_limit=5,
    )
    session.add_all([healthy, blocked])
    session.commit()

    interruption = PlatformInterruption(
        platform="indeed", kind="captcha",
        url="https://indeed.test/jobs", evidence="matched div.g-recaptcha",
        guidance="Complete the challenge yourself in the open browser window.",
        detected_at=utcnow(),
    )
    session.add(interruption)

    session.add(Job(
        platform="greenhouse", external_id="1", title="Backend Engineer",
        company="Acme", location="Remote", description="d", apply_method="web_form",
        dedup_hash="h", hard_filter_pass=True, fit_score=0.9,
    ))
    session.commit()
    session.refresh(interruption)

    return {"interruption": interruption}


class TestHealthEndpoint:
    """GET /api/v1/health"""

    def test_reports_each_platform(self, client, seeded):
        body = client.get("/api/v1/health").json()

        assert body["total"] == 2
        assert body["healthy"] == 1
        assert body["needs_attention"] == 1
        assert body["open_interruptions"] == 1

    def test_flags_the_platform_needing_reconnect(self, client, seeded):
        body = client.get("/api/v1/health").json()
        by_platform = {p["platform"]: p for p in body["platforms"]}

        assert by_platform["indeed"]["needs_reconnect"] is True
        assert by_platform["greenhouse"]["needs_reconnect"] is False
        assert "Complete the challenge" in by_platform["indeed"]["reason"]

    def test_reports_pending_work(self, client, seeded):
        body = client.get("/api/v1/health").json()
        by_platform = {p["platform"]: p for p in body["platforms"]}

        assert by_platform["greenhouse"]["pending_jobs"] == 1

    def test_empty_when_nothing_is_connected(self, client):
        body = client.get("/api/v1/health").json()

        assert body["total"] == 0
        assert body["platforms"] == []


class TestInterruptionEndpoints:
    """GET/POST /api/v1/interruptions"""

    def test_lists_open_interruptions(self, client, seeded):
        body = client.get("/api/v1/interruptions").json()

        assert body["total"] == 1
        assert body["interruptions"][0]["kind"] == "captcha"
        assert body["interruptions"][0]["is_open"] is True

    def test_filters_by_platform(self, client, seeded):
        assert client.get(
            "/api/v1/interruptions", params={"platform": "greenhouse"}).json()["total"] == 0
        assert client.get(
            "/api/v1/interruptions", params={"platform": "indeed"}).json()["total"] == 1

    def test_guidance_is_surfaced(self, client, seeded):
        body = client.get("/api/v1/interruptions").json()

        assert "yourself" in body["interruptions"][0]["guidance"]

    def test_resolve_is_refused_while_still_blocked(self, client, seeded, monkeypatch):
        async def blocked(self, account):
            return {"healthy": False, "reason": "captcha still showing", "interruption": None}

        monkeypatch.setattr(
            "job_agent.services.session_monitor.SessionMonitor.check_session", blocked)

        response = client.post(
            f"/api/v1/interruptions/{seeded['interruption'].id}/resolve")

        assert response.status_code == 409
        assert "still looks blocked" in response.json()["detail"]

    def test_resolve_succeeds_once_healthy(self, client, seeded, monkeypatch):
        async def healthy(self, account):
            return {"healthy": True, "reason": None, "interruption": None}

        monkeypatch.setattr(
            "job_agent.services.session_monitor.SessionMonitor.check_session", healthy)

        body = client.post(
            f"/api/v1/interruptions/{seeded['interruption'].id}/resolve").json()

        assert body["resolved"] is True
        assert body["verified"] is True
        assert client.get("/api/v1/interruptions").json()["total"] == 0

    def test_resolve_without_verification(self, client, seeded, monkeypatch):
        async def blocked(self, account):
            return {"healthy": False, "reason": "still blocked", "interruption": None}

        monkeypatch.setattr(
            "job_agent.services.session_monitor.SessionMonitor.check_session", blocked)

        body = client.post(
            f"/api/v1/interruptions/{seeded['interruption'].id}/resolve",
            params={"verify": False}).json()

        assert body["resolved"] is True
        assert body["verified"] is False

    def test_unknown_interruption_returns_404(self, client):
        assert client.post("/api/v1/interruptions/999/resolve").status_code == 404

    def test_missing_screenshot_reports_clearly(self, client, seeded):
        response = client.get(
            f"/api/v1/interruptions/{seeded['interruption'].id}/screenshot")

        assert response.status_code == 404
        assert "No screenshot" in response.json()["detail"]


class TestRecoveryEndpoints:
    """Reconnect and resume."""

    def test_reconnect_opens_a_browser(self, client, seeded, monkeypatch):
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
                return FakeContext()

        async def fake_manager():
            return FakeManager()

        monkeypatch.setattr(
            "job_agent.core.session_manager.get_session_manager", fake_manager)

        body = client.post("/api/v1/platforms/indeed/reconnect").json()

        assert body["opened"] is True
        assert "never enters credentials" in body["message"]
        assert "resume" in body["next_step"]

    def test_reconnect_unknown_platform_returns_404(self, client):
        assert client.post("/api/v1/platforms/myspace/reconnect").status_code == 404

    def test_resume_refuses_while_blocked(self, client, seeded, monkeypatch):
        async def blocked(self, account):
            return {"healthy": False, "reason": "captcha still showing", "interruption": None}

        monkeypatch.setattr(
            "job_agent.services.session_monitor.SessionMonitor.check_session", blocked)

        response = client.post("/api/v1/platforms/indeed/resume")

        assert response.status_code == 409
        assert "Reconnect it first" in response.json()["detail"]

    def test_resume_restores_the_platform(self, client, seeded, monkeypatch):
        """Acceptance: the platform shows Connected and its work resumes."""
        async def healthy(self, account):
            return {"healthy": True, "reason": None, "interruption": None}

        monkeypatch.setattr(
            "job_agent.services.session_monitor.SessionMonitor.check_session", healthy)

        body = client.post("/api/v1/platforms/indeed/resume").json()

        assert body["resumed"] is True
        assert body["interruptions_closed"] == 1

        health = client.get("/api/v1/health").json()
        by_platform = {p["platform"]: p for p in health["platforms"]}

        assert by_platform["indeed"]["status"] == "connected"
        assert by_platform["indeed"]["healthy"] is True

    def test_check_now_reports_per_platform(self, client, seeded, monkeypatch):
        async def healthy(self, account):
            return {"healthy": True, "reason": None, "interruption": None}

        monkeypatch.setattr(
            "job_agent.services.session_monitor.SessionMonitor.check_session", healthy)

        body = client.post("/api/v1/health/check").json()

        assert body["checked"] == 2
        assert body["healthy"] == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
