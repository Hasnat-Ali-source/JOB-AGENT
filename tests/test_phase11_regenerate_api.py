#!/usr/bin/env python3
"""
API tests for rewriting a queued application's documents.

Generating documents for a job and attaching them to the application waiting in
the tray are two different things. Only the first had an endpoint, so an
application the analyst refused for its resume could be fixed and still carry
the refused file — the new version existed and nothing pointed at it.

- POST /api/v1/review/{id}/regenerate-documents
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

MASTER_RESUME = """Alex Rivera
alex.rivera@example.com | +1 555 0100 | San Francisco, CA

Summary
Backend engineer with 8 years building distributed systems.

Experience
Staff Engineer, Northwind Systems (2021 - Present)
- Led migration of the billing platform to PostgreSQL
- Designed an event pipeline processing 12M events per day

Education
BSc Computer Science, University of Washington (2018)
"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient on an isolated database, writing documents to a temp dir."""
    from job_agent.config import settings

    monkeypatch.setattr(
        type(settings), "documents_dir", property(lambda _self: tmp_path)
    )

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    app.dependency_overrides[get_session] = lambda: session

    with TestClient(app) as test_client:
        test_client.db = session  # type: ignore[attr-defined]
        yield test_client

    app.dependency_overrides.clear()
    session.close()


@pytest.fixture
def queued(client):
    """
    An approved application carrying a resume that should never be sent.

    The stand-in for the real failure: a rewrite that kept the name, dropped
    the contact line, and was approved before anyone read it.
    """
    session = client.db

    job = Job(
        platform="generic_ats", external_id="REQ-1", title="Senior Backend Engineer",
        company="Acme Robotics", location="Remote",
        description="Kafka, PostgreSQL, distributed systems", apply_method="web_form",
        dedup_hash="h1",
    )
    account = PlatformAccount(
        platform="generic_ats", profile_dir="/tmp/x",
        automation_mode=AutomationMode.SEARCH_FILL_SUBMIT,
        clean_submissions_count=3, daily_apply_limit=5,
    )
    master = MasterDocument(
        doc_type=DocumentType.RESUME, name="Alex_Rivera_Resume",
        source_path="/tmp/master.txt", source_format=DocumentFormat.TXT,
        content_text=MASTER_RESUME, is_active=True,
    )
    session.add_all([job, account, master])
    session.commit()
    session.refresh(job)
    session.refresh(account)
    session.refresh(master)

    headerless = DocumentVersion(
        master_document_id=master.id, job_id=job.id, doc_type=DocumentType.RESUME,
        content_text=MASTER_RESUME.replace(
            "alex.rivera@example.com | +1 555 0100 | San Francisco, CA",
            "linkedin.com/in/alex-rivera",
        ),
        pdf_path="/tmp/old_resume.pdf", generator="llm:ollama:tiny",
    )
    session.add(headerless)
    session.commit()
    session.refresh(headerless)

    application = Application(
        job_id=job.id,
        platform_account_id=account.id,
        resume_version_id=headerless.id,
        resume_version=headerless.pdf_path,
        filled_fields={
            "Attach resume": {
                "value": "/tmp/old_resume.pdf", "selector": "#resume",
                "category": "known", "field_type": "file",
            },
        },
        deferred_fields={
            "Attach cover letter": {
                "selector": "#cover_letter", "category": "unknown",
                "field_type": "file", "required": False,
                "value_entered_by_user": None,
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

    return {"application": application, "job": job, "old_version": headerless}


class TestRegenerateDocuments:
    """POST /api/v1/review/{id}/regenerate-documents"""

    def test_the_new_resume_is_attached(self, client, queued):
        application = queued["application"]

        body = client.post(
            f"/api/v1/review/{application.id}/regenerate-documents"
        ).json()

        assert body["status"] == "regenerated"
        assert body["resume_version_id"] != queued["old_version"].id

    def test_the_upload_field_points_at_the_new_file(self, client, queued):
        # The submission path replays filled_fields onto the reopened form, so
        # a document nobody named there is a document nobody uploads.
        application = queued["application"]

        client.post(f"/api/v1/review/{application.id}/regenerate-documents")
        client.db.refresh(application)

        attached = application.filled_fields["Attach resume"]["value"]

        assert attached != "/tmp/old_resume.pdf"
        assert attached.endswith(".pdf")

    def test_a_cover_letter_field_stops_being_a_gap(self, client, queued):
        application = queued["application"]

        client.post(f"/api/v1/review/{application.id}/regenerate-documents")
        client.db.refresh(application)

        assert "Attach cover letter" not in application.deferred_fields
        assert "Attach cover letter" in application.filled_fields

    def test_approval_does_not_survive(self, client, queued):
        # The user approved documents that no longer exist.
        application = queued["application"]

        body = client.post(
            f"/api/v1/review/{application.id}/regenerate-documents"
        ).json()
        client.db.refresh(application)

        assert body["reviewed_by_user"] is False
        assert application.reviewed_by_user is False
        assert application.reviewed_at is None

    def test_the_analyst_stops_objecting(self, client, queued):
        application = queued["application"]

        before = client.get(f"/api/v1/review/{application.id}/analysis").json()
        after = client.post(
            f"/api/v1/review/{application.id}/regenerate-documents"
        ).json()["analysis"]

        assert "resume_reaches_you" in [b["check"] for b in before["blockers"]]
        assert "resume_reaches_you" not in [b["check"] for b in after["blockers"]]

    def test_submission_is_refused_until_it_is_fixed(self, client, queued):
        application = queued["application"]

        response = client.post(f"/api/v1/review/{application.id}/submit")

        assert response.status_code == 400
        assert "email address" in response.json()["detail"]

    def test_unknown_application_returns_404(self, client):
        assert client.post(
            "/api/v1/review/999/regenerate-documents"
        ).status_code == 404
