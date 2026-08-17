#!/usr/bin/env python3
"""
API tests for Phase 6b: email application endpoints.

The HTTP boundary around an irreversible, outbound action: composing a draft,
reviewing it, approving, sending, and tracking replies.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from job_agent.dashboard.deps import get_session
from job_agent.dashboard.main import app
from job_agent.models.database import (
    CandidateProfile,
    DocumentFormat,
    DocumentType,
    DocumentVersion,
    EmailDraft,
    EmailDraftStatus,
    EmailThread,
    EmailThreadStatus,
    Job,
    MasterDocument,
)
from job_agent.services.email_sender import EmailSender, SendResult
from job_agent.utils.dates import utcnow


class RecordingSender(EmailSender):
    """
    Captures sends instead of performing them.

    Subclasses the real sender so patched-in uses keep its class-level helpers
    (`is_valid_address`), and records to a class-level list because each route
    call constructs its own service — and therefore its own sender.
    """

    sent: list = []

    def __init__(self, *args, **kwargs):
        super().__init__(prefer_mail_app=False, allow_smtp=False)

    def send(self, to_email, subject, body, attachments=None):
        RecordingSender.sent.append(
            {"to": to_email, "subject": subject, "body": body,
             "attachments": attachments or []}
        )
        return SendResult(sent=True, method="smtp", message_id="<api-test@example.test>")


@pytest.fixture
def sender():
    """The fake sender, with its record cleared for each test."""
    RecordingSender.sent = []
    yield RecordingSender
    RecordingSender.sent = []


@pytest.fixture
def client(monkeypatch, sender, tmp_path):
    """TestClient with an isolated database and a fake email sender."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    # Every route builds its own service; swap the class it constructs
    monkeypatch.setattr("job_agent.services.email_service.EmailSender", RecordingSender)

    app.dependency_overrides[get_session] = lambda: session

    with TestClient(app) as test_client:
        test_client.db = session  # type: ignore[attr-defined]
        yield test_client

    app.dependency_overrides.clear()
    session.close()


@pytest.fixture
def seeded(client, tmp_path):
    """A job asking for email applications, with tailored documents."""
    from job_agent.services.pdf_renderer import PdfRenderer

    session = client.db

    job = Job(
        platform="generic_ats", external_id="REQ-42",
        title="Senior Backend Engineer", company="Acme Robotics", location="Remote",
        description=(
            "Please send your resume to careers@acme-robotics.test. "
            "Privacy enquiries: privacy@acme-robotics.test."
        ),
        apply_method="email", dedup_hash="h1",
    )
    profile = CandidateProfile(full_name="Alex Rivera", email="alex@example.com")
    master = MasterDocument(
        doc_type=DocumentType.RESUME, name="m", source_path="/tmp/m.txt",
        source_format=DocumentFormat.TXT, content_text="Alex Rivera",
    )
    session.add_all([job, profile, master])
    session.commit()
    session.refresh(job)
    session.refresh(master)

    resume_pdf = tmp_path / "resume.pdf"
    PdfRenderer().render("Alex Rivera\n\nExperience\n- Work", resume_pdf)

    resume = DocumentVersion(
        master_document_id=master.id, job_id=job.id, doc_type=DocumentType.RESUME,
        content_text="tailored", pdf_path=str(resume_pdf), generator="deterministic",
    )
    letter = DocumentVersion(
        master_document_id=master.id, job_id=job.id, doc_type=DocumentType.COVER_LETTER,
        content_text="Dear Hiring Manager,\n\nI am applying.\n\nSincerely,\nAlex Rivera",
        generator="deterministic",
    )
    session.add_all([resume, letter])
    session.commit()

    return {"job": job}


class TestDetectEndpoint:
    """GET /api/v1/email/detect"""

    def test_shows_chosen_address_and_all_candidates(self, client, seeded):
        body = client.get(
            "/api/v1/email/detect", params={"job_id": seeded["job"].id}).json()

        assert body["chosen"]["email"] == "careers@acme-robotics.test"
        emails = [c["email"] for c in body["candidates"]]
        assert "privacy@acme-robotics.test" in emails

    def test_rejected_candidates_are_visible(self, client, seeded):
        """A wrong pick should be visible, not silent."""
        body = client.get(
            "/api/v1/email/detect", params={"job_id": seeded["job"].id}).json()

        privacy = next(
            c for c in body["candidates"] if c["email"] == "privacy@acme-robotics.test")

        assert privacy["confident"] is False

    def test_unknown_job_returns_404(self, client):
        assert client.get("/api/v1/email/detect", params={"job_id": 999}).status_code == 404


class TestDraftEndpoints:
    """POST/GET/PATCH /api/v1/email/drafts"""

    def test_draft_is_created_awaiting_review(self, client, seeded):
        response = client.post(
            "/api/v1/email/drafts", params={"job_id": seeded["job"].id})

        assert response.status_code == 200
        body = response.json()
        assert body["action"] == "drafted"
        assert body["status"] == "draft"  # the draft's own state
        assert body["to_email"] == "careers@acme-robotics.test"
        assert body["reviewed_by_user"] is False

    def test_draft_detail_shows_exactly_what_would_be_sent(self, client, seeded):
        draft_id = client.post(
            "/api/v1/email/drafts", params={"job_id": seeded["job"].id}).json()["id"]

        body = client.get(f"/api/v1/email/drafts/{draft_id}").json()

        assert "Dear Hiring Manager" in body["body"]
        assert body["attachments"][0]["name"] == "resume.pdf"
        assert body["attachments"][0]["exists"] is True

    def test_recipient_override_is_honoured(self, client, seeded):
        body = client.post("/api/v1/email/drafts", params={
            "job_id": seeded["job"].id, "to_email": "talent@acme-robotics.test",
        }).json()

        assert body["to_email"] == "talent@acme-robotics.test"

    def test_invalid_recipient_is_rejected(self, client, seeded):
        response = client.post("/api/v1/email/drafts", params={
            "job_id": seeded["job"].id, "to_email": "nonsense",
        })

        assert response.status_code == 400

    def test_listing_reports_quota(self, client, seeded):
        client.post("/api/v1/email/drafts", params={"job_id": seeded["job"].id})

        body = client.get("/api/v1/email/drafts").json()

        assert body["total"] == 1
        assert body["awaiting_review"] == 1
        assert body["hourly_quota_remaining"] == body["hourly_limit"]

    def test_editing_revokes_approval(self, client, seeded):
        """
        An approval applies to the text the user read. Changing the text after
        approval must require re-approval.
        """
        draft_id = client.post(
            "/api/v1/email/drafts", params={"job_id": seeded["job"].id}).json()["id"]
        client.post(f"/api/v1/email/drafts/{draft_id}/approve")

        body = client.patch(
            f"/api/v1/email/drafts/{draft_id}", json={"body": "Rewritten text"}).json()

        assert body["reapproval_required"] is True
        assert body["status"] == "draft"

        response = client.post(f"/api/v1/email/drafts/{draft_id}/send")
        assert response.status_code == 400

    def test_unknown_draft_returns_404(self, client):
        assert client.get("/api/v1/email/drafts/999").status_code == 404


class TestSendEndpoint:
    """POST /api/v1/email/drafts/{id}/send"""

    def test_unapproved_draft_is_refused(self, client, seeded, sender):
        draft_id = client.post(
            "/api/v1/email/drafts", params={"job_id": seeded["job"].id}).json()["id"]

        response = client.post(f"/api/v1/email/drafts/{draft_id}/send")

        assert response.status_code == 400
        assert "not been approved" in response.json()["detail"]
        assert sender.sent == []

    def test_approved_draft_sends(self, client, seeded, sender):
        """Acceptance: approve → send, with message-id and sent_at logged."""
        draft_id = client.post(
            "/api/v1/email/drafts", params={"job_id": seeded["job"].id}).json()["id"]
        client.post(f"/api/v1/email/drafts/{draft_id}/approve")

        body = client.post(f"/api/v1/email/drafts/{draft_id}/send").json()

        assert body["status"] == "sent"
        assert body["message_id"] == "<api-test@example.test>"
        assert body["sent_at"]
        assert len(sender.sent) == 1

    def test_sending_twice_is_refused(self, client, seeded, sender):
        draft_id = client.post(
            "/api/v1/email/drafts", params={"job_id": seeded["job"].id}).json()["id"]
        client.post(f"/api/v1/email/drafts/{draft_id}/approve")
        client.post(f"/api/v1/email/drafts/{draft_id}/send")

        response = client.post(f"/api/v1/email/drafts/{draft_id}/send")

        assert response.status_code == 400
        assert len(sender.sent) == 1

    def test_discarded_draft_is_not_sent(self, client, seeded, sender):
        draft_id = client.post(
            "/api/v1/email/drafts", params={"job_id": seeded["job"].id}).json()["id"]
        client.post(f"/api/v1/email/drafts/{draft_id}/discard",
                    json={"reason": "Wrong role"})

        response = client.post(f"/api/v1/email/drafts/{draft_id}/send")

        assert response.status_code == 400
        assert sender.sent == []


class TestThreadEndpoints:
    """Sent applications and their replies."""

    def test_sending_creates_a_thread(self, client, seeded):
        draft_id = client.post(
            "/api/v1/email/drafts", params={"job_id": seeded["job"].id}).json()["id"]
        client.post(f"/api/v1/email/drafts/{draft_id}/approve")
        client.post(f"/api/v1/email/drafts/{draft_id}/send")

        body = client.get("/api/v1/email/threads").json()

        assert body["total"] == 1
        assert body["awaiting_reply"] == 1
        assert body["threads"][0]["recipient_email"] == "careers@acme-robotics.test"

    def test_reply_is_surfaced(self, client, seeded):
        """A linked reply appears against the thread."""
        session = client.db
        thread = EmailThread(
            application_id=0, sent_message_id="<x@test>",
            recipient_email="careers@acme-robotics.test", subject="Application",
            sent_at=utcnow(),
            reply_received_at=utcnow(),
            reply_snippet="Thanks for applying — we'd like to schedule a call.",
            thread_status=EmailThreadStatus.REPLY_RECEIVED,
        )
        session.add(thread)
        session.commit()

        body = client.get("/api/v1/email/threads").json()

        assert body["replies_received"] == 1
        assert "schedule a call" in body["threads"][0]["reply_snippet"]

    def test_check_replies_without_imap_config_explains(self, client, monkeypatch):
        from job_agent.config import settings

        monkeypatch.setattr(settings, "imap_host", None)

        response = client.post("/api/v1/email/check-replies")

        assert response.status_code == 400
        assert "IMAP is not configured" in response.json()["detail"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
