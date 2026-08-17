#!/usr/bin/env python3
"""
API tests for Phase 5: the Review Queue.

The user's side of the gate: see what was filled, answer what the agent
wouldn't, then approve or discard. Nothing here sends an application to an
employer.
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
    AuditAction,
    AuditLog,
    CandidateProfile,
    DocumentVersion,
    DocumentType,
    FieldCategory,
    Job,
    MasterDocument,
    DocumentFormat,
    PlatformAccount,
)
from job_agent.services.field_classifier import FieldClassifier


@pytest.fixture
def client(tmp_path):
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
def seeded(client, tmp_path):
    """A queued application with filled, sensitive, and unknown fields."""
    session = client.db

    job = Job(
        platform="generic_ats", external_id="REQ-1",
        title="Senior Backend Engineer", company="Acme Robotics", location="Remote",
        description="Kafka and PostgreSQL.", requirements="Python",
        apply_method="web_form", dedup_hash="h1", fit_score=0.82,
    )
    account = PlatformAccount(platform="generic_ats", profile_dir="/tmp/x")
    profile = CandidateProfile(full_name="Alex Rivera", email="alex@example.com")
    session.add_all([job, account, profile])
    session.commit()
    session.refresh(job)
    session.refresh(account)
    session.refresh(profile)

    screenshot = tmp_path / "form.png"
    screenshot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    application = Application(
        job_id=job.id,
        platform_account_id=account.id,
        candidate_profile_id=profile.id,
        filled_fields={
            "Full name": {"value": "Alex Rivera", "category": "known",
                          "selector": "#fullName", "reason": "from your profile"},
            "Email address": {"value": "alex@example.com", "category": "known",
                              "selector": "#emailAddr", "reason": "from your profile"},
        },
        deferred_fields={
            "Gender": {
                "category": FieldCategory.SENSITIVE.value,
                "reason": "Looks like a demographics question — the agent never answers this.",
                "question": "Gender", "field_type": "select",
                "options": ["Female", "Male"], "required": False,
                "value_entered_by_user": None, "selector": "#genderField",
            },
            "Expected salary": {
                "category": FieldCategory.SENSITIVE.value,
                "reason": "Looks like a compensation expectation question — never answered.",
                "question": "Expected salary", "field_type": "text",
                "options": [], "required": True,
                "value_entered_by_user": None, "selector": "#expectedSalary",
            },
            "Why do you want to work here?": {
                "category": FieldCategory.UNKNOWN.value,
                "reason": "The agent could not map this question to your profile",
                "question": "Why do you want to work here?", "field_type": "textarea",
                "options": [], "required": True,
                "value_entered_by_user": None, "selector": "#whyUs",
            },
        },
        screenshot_path=str(screenshot),
        form_url="http://127.0.0.1:9/apply",
        submission_status=ApplicationStatus.QUEUED_FOR_REVIEW,
    )
    session.add(application)
    session.commit()
    session.refresh(application)

    return {"application": application, "job": job, "profile": profile}


# ============================================================================
# Candidate profile
# ============================================================================

class TestProfileApi:
    """The profile the agent fills forms from."""

    def test_profile_starts_unconfigured(self, client):
        body = client.get("/api/v1/review/profile").json()

        assert body["configured"] is False

    def test_profile_can_be_saved_and_read(self, client):
        response = client.post("/api/v1/review/profile", json={
            "full_name": "Alex Rivera",
            "email": "alex@example.com",
            "phone": "+1 555 0100",
            "willing_to_relocate": True,
        })

        assert response.status_code == 200

        body = client.get("/api/v1/review/profile").json()
        assert body["configured"] is True
        assert body["full_name"] == "Alex Rivera"
        assert body["willing_to_relocate"] is True

    def test_profile_requires_name_and_email(self, client):
        response = client.post("/api/v1/review/profile", json={"phone": "123"})

        assert response.status_code == 400
        assert "full_name and email" in response.json()["detail"]

    def test_saving_again_updates_in_place(self, client):
        client.post("/api/v1/review/profile",
                    json={"full_name": "A", "email": "a@b.com"})
        client.post("/api/v1/review/profile",
                    json={"full_name": "A B", "email": "a@b.com", "phone": "999"})

        body = client.get("/api/v1/review/profile").json()
        assert body["full_name"] == "A B"
        assert body["phone"] == "999"


# ============================================================================
# Queue listing and detail
# ============================================================================

class TestQueueListing:
    """GET /api/v1/review"""

    def test_queue_lists_pending_applications(self, client, seeded):
        body = client.get("/api/v1/review").json()

        assert body["total"] == 1
        entry = body["applications"][0]
        assert entry["company"] == "Acme Robotics"
        assert entry["filled_count"] == 2
        assert entry["deferred_count"] == 3

    def test_queue_flags_required_unanswered(self, client, seeded):
        body = client.get("/api/v1/review").json()
        entry = body["applications"][0]

        assert set(entry["required_unanswered"]) == {
            "Expected salary", "Why do you want to work here?"}
        assert entry["ready_to_submit"] is False
        assert body["needs_answers"] == 1

    def test_empty_queue(self, client):
        body = client.get("/api/v1/review").json()

        assert body["total"] == 0
        assert body["applications"] == []


class TestReviewDetail:
    """GET /api/v1/review/{id} — the side-by-side view."""

    def test_detail_separates_sensitive_from_unknown(self, client, seeded):
        body = client.get(f"/api/v1/review/{seeded['application'].id}").json()

        assert set(body["sensitive_questions"]) == {"Gender", "Expected salary"}
        assert set(body["other_questions"]) == {"Why do you want to work here?"}

    def test_detail_includes_the_source_posting(self, client, seeded):
        """Acceptance: filled form shown beside the job it came from."""
        body = client.get(f"/api/v1/review/{seeded['application'].id}").json()

        assert body["job"]["title"] == "Senior Backend Engineer"
        assert body["job"]["description"] == "Kafka and PostgreSQL."
        assert body["job"]["fit_score"] == 0.82

    def test_detail_includes_filled_values(self, client, seeded):
        body = client.get(f"/api/v1/review/{seeded['application'].id}").json()

        assert body["filled_fields"]["Full name"]["value"] == "Alex Rivera"

    def test_detail_links_the_screenshot(self, client, seeded):
        application_id = seeded["application"].id
        body = client.get(f"/api/v1/review/{application_id}").json()

        assert body["screenshot_url"] == f"/api/v1/review/{application_id}/screenshot"

    def test_screenshot_downloads(self, client, seeded):
        response = client.get(f"/api/v1/review/{seeded['application'].id}/screenshot")

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content.startswith(b"\x89PNG")

    def test_unknown_application_returns_404(self, client):
        assert client.get("/api/v1/review/999").status_code == 404
        assert client.get("/api/v1/review/999/screenshot").status_code == 404

    def test_unverified_documents_are_surfaced(self, client, seeded):
        """A flagged resume must be visible at the moment of review."""
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
            doc_type=DocumentType.RESUME, content_text="tailored",
            pdf_path="/tmp/r.pdf", generator="llm:test",
            fabrication_flags=["Number '60%' does not appear in the master document"],
        )
        session.add(version)
        session.commit()
        session.refresh(version)

        application = seeded["application"]
        application.resume_version_id = version.id
        session.commit()

        body = client.get(f"/api/v1/review/{application.id}").json()

        assert len(body["unverified_documents"]) == 1
        assert body["unverified_documents"][0]["fabrication_flags"]


# ============================================================================
# Answering and deciding
# ============================================================================

class TestAnsweringQuestions:
    """POST /api/v1/review/{id}/answers"""

    def test_answers_are_recorded(self, client, seeded):
        application_id = seeded["application"].id

        response = client.post(
            f"/api/v1/review/{application_id}/answers",
            json={"answers": {"Expected salary": "180000"}},
        )

        assert response.status_code == 200
        assert response.json()["required_unanswered"] == ["Why do you want to work here?"]

        detail = client.get(f"/api/v1/review/{application_id}").json()
        assert (
            detail["sensitive_questions"]["Expected salary"]["value_entered_by_user"]
            == "180000"
        )

    def test_answering_everything_makes_it_ready(self, client, seeded):
        application_id = seeded["application"].id

        body = client.post(
            f"/api/v1/review/{application_id}/answers",
            json={"answers": {
                "Expected salary": "180000",
                "Why do you want to work here?": "I admire the product.",
            }},
        ).json()

        assert body["required_unanswered"] == []
        assert body["ready_to_submit"] is True

    def test_ordinary_answers_are_remembered(self, client, seeded):
        application_id = seeded["application"].id

        client.post(
            f"/api/v1/review/{application_id}/answers",
            json={"answers": {"Why do you want to work here?": "I admire the product."}},
        )

        profile = client.get("/api/v1/review/profile").json()
        key = FieldClassifier.remember_key("Why do you want to work here?")

        assert profile["remembered_answers"][key] == "I admire the product."

    def test_sensitive_answers_are_not_remembered_by_default(self, client, seeded):
        """
        Demographic and compensation answers must never be kept as a side
        effect of saving a form — only because the user asked for it.
        """
        application_id = seeded["application"].id

        body = client.post(
            f"/api/v1/review/{application_id}/answers",
            json={"answers": {"Gender": "Female", "Expected salary": "180000"}},
        ).json()

        assert body["remembered_for_future_forms"] == 0

        profile = client.get("/api/v1/review/profile").json()
        assert profile["remembered_answers"] == {}

    def test_sensitive_answers_are_kept_when_asked_for(self, client, seeded):
        """
        Retyping gender on every application protects nobody. When the user
        opts in, the answer is kept so the same question isn't asked twice.
        """
        application_id = seeded["application"].id

        body = client.post(
            f"/api/v1/review/{application_id}/answers",
            json={
                "answers": {"Gender": "Female"},
                "remember_sensitive": True,
            },
        ).json()

        assert body["remembered_for_future_forms"] == 1

        profile = client.get("/api/v1/review/profile").json()
        key = FieldClassifier.remember_key("Gender")

        assert profile["remembered_answers"][key] == "Female"

    def test_opting_in_for_sensitive_does_not_leak_across_applications(
        self, client, seeded
    ):
        """The opt-in is per save, not a standing setting on the profile."""
        application_id = seeded["application"].id

        client.post(
            f"/api/v1/review/{application_id}/answers",
            json={"answers": {"Gender": "Female"}, "remember_sensitive": True},
        )
        client.post(
            f"/api/v1/review/{application_id}/answers",
            json={"answers": {"Expected salary": "180000"}},
        )

        profile = client.get("/api/v1/review/profile").json()

        assert FieldClassifier.remember_key("Gender") in profile["remembered_answers"]
        assert (
            FieldClassifier.remember_key("Expected salary")
            not in profile["remembered_answers"]
        )

    def test_remember_can_be_declined(self, client, seeded):
        application_id = seeded["application"].id

        client.post(
            f"/api/v1/review/{application_id}/answers",
            json={
                "answers": {"Why do you want to work here?": "Private reason."},
                "remember": False,
            },
        )

        profile = client.get("/api/v1/review/profile").json()
        assert profile["remembered_answers"] == {}

    def test_answering_a_field_that_was_not_deferred_is_rejected(self, client, seeded):
        response = client.post(
            f"/api/v1/review/{seeded['application'].id}/answers",
            json={"answers": {"Not a real question": "x"}},
        )

        assert response.status_code == 400
        assert "Not deferred" in response.json()["detail"]

    def test_empty_answers_rejected(self, client, seeded):
        response = client.post(
            f"/api/v1/review/{seeded['application'].id}/answers", json={"answers": {}})

        assert response.status_code == 400


class TestApproveAndDiscard:
    """The two ways an application leaves the queue."""

    def test_approve_blocked_while_required_answers_are_missing(self, client, seeded):
        """Approving a half-filled form would send a broken application."""
        response = client.post(f"/api/v1/review/{seeded['application'].id}/approve")

        assert response.status_code == 400
        assert "required question(s) still unanswered" in response.json()["detail"]

    def test_approve_after_answering(self, client, seeded):
        application_id = seeded["application"].id

        client.post(f"/api/v1/review/{application_id}/answers", json={"answers": {
            "Expected salary": "180000",
            "Why do you want to work here?": "I admire the product.",
        }})

        response = client.post(
            f"/api/v1/review/{application_id}/approve", json={"notes": "Looks right"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "approved"
        assert "Submit the form yourself" in body["next_step"]

    def test_approval_does_not_submit(self, client, seeded):
        """Acceptance: approval logs the data and awaits manual submission."""
        application_id = seeded["application"].id

        client.post(f"/api/v1/review/{application_id}/answers", json={"answers": {
            "Expected salary": "1", "Why do you want to work here?": "x"}})
        client.post(f"/api/v1/review/{application_id}/approve")

        application = client.db.query(Application).filter(
            Application.id == application_id).one()

        assert application.reviewed_by_user is True
        assert application.reviewed_at is not None
        assert application.submission_status == ApplicationStatus.QUEUED_FOR_REVIEW
        assert application.submitted_at is None

    def test_approval_is_audited_as_a_user_action(self, client, seeded):
        application_id = seeded["application"].id

        client.post(f"/api/v1/review/{application_id}/answers", json={"answers": {
            "Expected salary": "1", "Why do you want to work here?": "x"}})
        client.post(f"/api/v1/review/{application_id}/approve")

        entry = client.db.query(AuditLog).filter(
            AuditLog.action == AuditAction.APPLICATION_APPROVED).one()

        assert entry.actor == "user"

    def test_discard_marks_rejected(self, client, seeded):
        application_id = seeded["application"].id

        response = client.post(
            f"/api/v1/review/{application_id}/discard", json={"reason": "Wrong seniority"})

        assert response.status_code == 200

        application = client.db.query(Application).filter(
            Application.id == application_id).one()
        assert application.submission_status == ApplicationStatus.REJECTED
        assert application.user_notes == "Wrong seniority"

    def test_discarded_application_leaves_the_queue(self, client, seeded):
        client.post(f"/api/v1/review/{seeded['application'].id}/discard")

        assert client.get("/api/v1/review").json()["total"] == 0
        assert client.get("/api/v1/review", params={"status": "all"}).json()["total"] == 1

    def test_discard_is_audited(self, client, seeded):
        client.post(f"/api/v1/review/{seeded['application'].id}/discard",
                    json={"reason": "Not interested"})

        entry = client.db.query(AuditLog).filter(
            AuditLog.action == AuditAction.APPLICATION_DISCARDED).one()

        assert entry.actor == "user"
        assert "Not interested" in entry.detail


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
