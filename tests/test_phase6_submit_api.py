#!/usr/bin/env python3
"""
API tests for Phase 6: submission endpoints.

Covers the guards around the irreversible action — an application reaching a
real employer — at the HTTP boundary:

- GET  /api/v1/review/{id}/eligibility
- POST /api/v1/review/{id}/submit
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from job_agent.dashboard.deps import get_session
from job_agent.dashboard.main import app
from job_agent.models.database import (
    Application,
    ApplicationStatus,
    AutomationMode,
    DocumentFormat,
    DocumentType,
    DocumentVersion,
    Job,
    MasterDocument,
    PlatformAccount,
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
    """A reviewed, complete application on an auto-submit-enabled platform."""
    session = client.db

    job = Job(
        platform="generic_ats", external_id="REQ-1", title="Senior Backend Engineer",
        company="Acme Robotics", location="Remote", description="Kafka",
        apply_method="web_form", dedup_hash="h1",
    )
    account = PlatformAccount(
        platform="generic_ats", profile_dir="/tmp/x",
        automation_mode=AutomationMode.SEARCH_FILL_SUBMIT,
        clean_submissions_count=3, daily_apply_limit=5,
    )
    session.add_all([job, account])
    session.commit()
    session.refresh(job)
    session.refresh(account)

    application = Application(
        job_id=job.id, platform_account_id=account.id,
        filled_fields={"Full name": {"value": "Alex Rivera"}},
        deferred_fields={
            "Expected salary": {
                "category": "sensitive", "required": True,
                "value_entered_by_user": "180000", "question": "Expected salary",
            },
        },
        submission_status=ApplicationStatus.QUEUED_FOR_REVIEW,
        reviewed_by_user=True,
        reviewed_at=utcnow(),
        form_url="http://127.0.0.1:9/apply",
    )
    session.add(application)
    session.commit()
    session.refresh(application)

    return {"application": application, "job": job, "account": account}


class TestEligibilityEndpoint:
    """GET /api/v1/review/{id}/eligibility"""

    def test_reports_both_paths_and_counters(self, client, seeded):
        body = client.get(
            f"/api/v1/review/{seeded['application'].id}/eligibility").json()

        assert body["clean_submissions_count"] == 3
        assert body["clean_submissions_threshold"] == 3
        assert body["daily_apply_limit"] == 5
        assert body["submitted_today"] == 0
        assert body["user_directed"]["allowed"] is True

    def test_explains_why_auto_submit_is_blocked(self, client, seeded):
        account = seeded["account"]
        account.clean_submissions_count = 1
        client.db.commit()

        body = client.get(
            f"/api/v1/review/{seeded['application'].id}/eligibility").json()

        assert body["auto"]["allowed"] is False
        assert any("reviewed submissions" in b for b in body["auto"]["blockers"])

    def test_unknown_application_returns_404(self, client):
        assert client.get("/api/v1/review/999/eligibility").status_code == 404


class TestSubmitEndpoint:
    """POST /api/v1/review/{id}/submit"""

    def test_unreviewed_application_is_refused(self, client, seeded):
        """Submission follows review, never precedes it."""
        application = seeded["application"]
        application.reviewed_by_user = False
        client.db.commit()

        response = client.post(f"/api/v1/review/{application.id}/submit")

        assert response.status_code == 400
        assert "Approve this application first" in response.json()["detail"]

    def test_unanswered_required_question_is_refused(self, client, seeded):
        application = seeded["application"]
        application.deferred_fields = {
            "Why do you want to work here?": {
                "category": "unknown", "required": True, "value_entered_by_user": None,
            },
        }
        client.db.commit()

        response = client.post(f"/api/v1/review/{application.id}/submit")

        assert response.status_code == 400
        assert "required question" in response.json()["detail"]

    def test_unverified_document_is_refused(self, client, seeded):
        """A resume with unsupported claims must not reach an employer."""
        session = client.db
        master = MasterDocument(
            doc_type=DocumentType.RESUME, name="m", source_path="/tmp/m.txt",
            source_format=DocumentFormat.TXT, content_text="x",
        )
        session.add(master)
        session.commit()
        session.refresh(master)

        version = DocumentVersion(
            master_document_id=master.id, job_id=seeded["job"].id,
            doc_type=DocumentType.RESUME, content_text="t", pdf_path="/tmp/r.pdf",
            generator="llm:test",
            fabrication_flags=["Number '60%' does not appear in the master document"],
        )
        session.add(version)
        session.commit()
        session.refresh(version)

        application = seeded["application"]
        application.resume_version_id = version.id
        session.commit()

        response = client.post(f"/api/v1/review/{application.id}/submit")

        assert response.status_code == 400
        assert "not supported by your master" in response.json()["detail"]

    def test_already_submitted_is_refused(self, client, seeded):
        application = seeded["application"]
        application.submission_status = ApplicationStatus.SUBMITTED
        application.submitted_at = utcnow()
        client.db.commit()

        response = client.post(f"/api/v1/review/{application.id}/submit")

        assert response.status_code == 400
        assert "Already submitted" in response.json()["detail"]

    def test_missing_browser_session_is_reported(self, client, seeded, monkeypatch):
        """
        With no live browser there is nothing to submit — the request must fail
        clearly rather than appearing to succeed.
        """
        async def no_page(platform, needs_signin=True):
            return None

        class FakeManager:
            get_page = staticmethod(no_page)

        async def fake_manager():
            return FakeManager()

        monkeypatch.setattr(
            "job_agent.dashboard.routes.review.get_session_manager", fake_manager)

        response = client.post(f"/api/v1/review/{seeded['application'].id}/submit")

        assert response.status_code == 409
        assert "No authenticated browser session" in response.json()["detail"]

    def test_a_browser_on_the_wrong_page_never_submits_it(self, client, seeded, monkeypatch):
        """
        What the user reviewed must be what gets sent.

        An application is filled during a run and submitted later, often after
        that browser has closed, so the route reopens the form and writes the
        reviewed answers back. What it must never do is submit whatever page
        happens to be on screen: if the form cannot be restored, the request
        fails instead.
        """
        class FakePage:
            url = "http://127.0.0.1:9/somewhere-else"

            async def goto(self, url, **kwargs):
                raise RuntimeError("form unreachable")

        class FakeManager:
            @staticmethod
            async def get_page(platform, needs_signin=True):
                return FakePage()

        async def fake_manager():
            return FakeManager()

        monkeypatch.setattr(
            "job_agent.dashboard.routes.review.get_session_manager", fake_manager)

        response = client.post(f"/api/v1/review/{seeded['application'].id}/submit")

        assert response.status_code == 409
        assert "Could not reopen the filled form" in response.json()["detail"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
