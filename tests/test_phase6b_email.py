#!/usr/bin/env python3
"""
Tests for Phase 6b: Email-Based Applications.

Covers the Phase 6b acceptance criteria:
- A posting saying "Email resume to: hiring@company.com" is detected
- A draft composes with subject, cover-letter body, and PDF attachments
- The draft appears for review rather than being sent
- Approval then sending logs the message-id and sent_at
- Replies are polled and linked back to the application

The safety properties: a draft can never go out unapproved, editing revokes a
prior approval, sending is rate-limited, and no account password is ever used.
"""

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from job_agent.models.database import (
    Application,
    ApplicationStatus,
    AuditAction,
    AuditLog,
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
    PlatformAccount,
)
from job_agent.services.email_composer import EmailComposer
from job_agent.services.email_detector import EmailApplicationDetector
from job_agent.services.email_sender import EmailSender, SendResult
from job_agent.services.email_service import (
    EmailApplicationError,
    EmailApplicationService,
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
def profile(session) -> CandidateProfile:
    """The user's profile."""
    record = CandidateProfile(
        full_name="Alex Rivera", email="alex.rivera@example.com",
        phone="+1 555 0100", linkedin_url="https://linkedin.com/in/alexrivera",
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


@pytest.fixture
def job(session) -> Job:
    """A posting that asks for email applications."""
    record = Job(
        platform="generic_ats", external_id="REQ-42",
        title="Senior Backend Engineer", company="Acme Robotics", location="Remote",
        description=(
            "We are hiring a backend engineer. Please send your resume to "
            "careers@acme-robotics.test. For privacy questions contact "
            "privacy@acme-robotics.test."
        ),
        apply_method="email", dedup_hash="h1",
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


@pytest.fixture
def documents(session, job, tmp_path):
    """A verified tailored resume and cover letter with real PDFs."""
    from job_agent.services.pdf_renderer import PdfRenderer

    master = MasterDocument(
        doc_type=DocumentType.RESUME, name="master", source_path="/tmp/m.txt",
        source_format=DocumentFormat.TXT, content_text="Alex Rivera\nExperience",
    )
    session.add(master)
    session.commit()
    session.refresh(master)

    resume_pdf = tmp_path / "resume.pdf"
    letter_pdf = tmp_path / "cover.pdf"
    PdfRenderer().render("Alex Rivera\n\nExperience\n- Built systems", resume_pdf)
    PdfRenderer().render("Dear Hiring Manager,\n\nI am applying.", letter_pdf)

    resume = DocumentVersion(
        master_document_id=master.id, job_id=job.id, doc_type=DocumentType.RESUME,
        content_text="tailored resume", pdf_path=str(resume_pdf), generator="deterministic",
    )
    letter = DocumentVersion(
        master_document_id=master.id, job_id=job.id, doc_type=DocumentType.COVER_LETTER,
        content_text=(
            "Dear Hiring Manager,\n\nI am applying for the Senior Backend Engineer "
            "role at Acme Robotics.\n\nSincerely,\nAlex Rivera"
        ),
        pdf_path=str(letter_pdf), generator="deterministic",
    )
    session.add_all([resume, letter])
    session.commit()
    session.refresh(resume)
    session.refresh(letter)

    return {"resume": resume, "cover_letter": letter}


class FakeSender(EmailSender):
    """Records sends instead of performing them."""

    def __init__(self, succeed: bool = True):
        super().__init__(prefer_mail_app=False, allow_smtp=False)
        self.succeed = succeed
        self.sent = []

    def send(self, to_email, subject, body, attachments=None):
        self.sent.append({
            "to": to_email, "subject": subject,
            "body": body, "attachments": attachments or [],
        })

        if not self.succeed:
            return SendResult(sent=False, error_message="SMTP refused the connection")

        return SendResult(
            sent=True, method="smtp", message_id=f"<test-{len(self.sent)}@example.test>"
        )


@pytest.fixture
def service(session) -> EmailApplicationService:
    """Service with a fake sender."""
    return EmailApplicationService(session, sender=FakeSender())


# ============================================================================
# Recipient detection
# ============================================================================

class TestRecipientDetection:
    """Emailing the wrong address sends the user's resume to a stranger."""

    def test_detects_the_application_address(self, job):
        """Acceptance: "send your resume to X" is detected."""
        candidate = EmailApplicationDetector.detect(job)

        assert candidate is not None
        assert candidate.email == "careers@acme-robotics.test"

    def test_prefers_careers_over_privacy(self, job):
        candidates = EmailApplicationDetector.rank(job.description)
        ranked = [c.email for c in candidates]

        assert ranked[0] == "careers@acme-robotics.test"
        assert "privacy@acme-robotics.test" in ranked  # found, but ranked below

    @pytest.mark.parametrize("local_part", [
        "noreply", "no-reply", "unsubscribe", "privacy", "support", "press", "billing",
    ])
    def test_non_application_inboxes_are_rejected(self, session, local_part):
        record = Job(
            platform="p", external_id="1", title="T", company="C", location="L",
            description=f"Questions? Write to {local_part}@acme.test",
            apply_method="email", dedup_hash=f"h-{local_part}",
        )

        assert EmailApplicationDetector.detect(record) is None

    def test_explicit_recruiter_contact_wins(self, session):
        record = Job(
            platform="p", external_id="1", title="T", company="C", location="L",
            description="Lots of text with other@acme.test in it",
            recruiter_contact="recruiter@acme.test",
            apply_method="email", dedup_hash="h2",
        )

        candidate = EmailApplicationDetector.detect(record)

        assert candidate.email == "recruiter@acme.test"
        assert candidate.score == 10

    def test_address_with_no_supporting_context_is_not_proposed(self, session):
        """A bare address in a footer isn't an invitation to apply."""
        record = Job(
            platform="p", external_id="1", title="T", company="C", location="L",
            description="Acme Robotics. Building things. info@acme.test",
            apply_method="email", dedup_hash="h3",
        )

        assert EmailApplicationDetector.detect(record) is None

    def test_hyphenated_inbox_is_recognized(self, session):
        record = Job(
            platform="p", external_id="1", title="T", company="C", location="L",
            description="Send your CV to careers-emea@acme.test",
            apply_method="email", dedup_hash="h4",
        )

        candidate = EmailApplicationDetector.detect(record)

        assert candidate.email == "careers-emea@acme.test"

    def test_no_email_in_posting_returns_none(self, session):
        record = Job(
            platform="p", external_id="1", title="T", company="C", location="L",
            description="Apply through our website.", apply_method="web_form",
            dedup_hash="h5",
        )

        assert EmailApplicationDetector.detect(record) is None

    def test_detection_reports_its_reasoning(self, job):
        candidate = EmailApplicationDetector.detect(job)

        assert candidate.reasons
        assert any("application inbox" in r for r in candidate.reasons)

    def test_neighbouring_addresses_do_not_contaminate_reasons(self, session):
        """
        Postings list several addresses close together. A good address must not
        inherit a neighbour's penalty, or the user is shown contradictory
        reasons for the same pick.
        """
        record = Job(
            platform="p", external_id="1", title="T", company="C", location="L",
            description=(
                "Please send your CV to careers@acme.test. "
                "Unsubscribe: no-reply@acme.test."
            ),
            apply_method="email", dedup_hash="h-neighbour",
        )

        candidate = EmailApplicationDetector.detect(record)

        assert candidate.email == "careers@acme.test"
        assert not any("something else" in r for r in candidate.reasons)

    def test_context_is_clipped_at_the_next_address(self, session):
        record = Job(
            platform="p", external_id="1", title="T", company="C", location="L",
            description="Apply to jobs@acme.test. Press: press@acme.test.",
            apply_method="email", dedup_hash="h-clip",
        )

        ranked = {c.email: c for c in EmailApplicationDetector.rank(record.description)}

        assert "press@acme.test" not in ranked["jobs@acme.test"].context


# ============================================================================
# Composition
# ============================================================================

class TestComposition:
    """The email is built from already-verified material."""

    def test_body_comes_from_the_cover_letter(self, session, job, profile, documents):
        composed = EmailComposer(session).compose(
            job, "careers@acme.test", profile,
            documents["resume"], documents["cover_letter"],
        )

        assert "Dear Hiring Manager" in composed.body
        assert "Acme Robotics" in composed.body

    def test_subject_names_the_role_and_candidate(self, session, job, profile, documents):
        composed = EmailComposer(session).compose(
            job, "careers@acme.test", profile, documents["resume"], documents["cover_letter"])

        assert "Senior Backend Engineer" in composed.subject
        assert "Alex Rivera" in composed.subject
        assert "REQ-42" in composed.subject

    def test_both_pdfs_are_attached(self, session, job, profile, documents):
        composed = EmailComposer(session).compose(
            job, "careers@acme.test", profile, documents["resume"], documents["cover_letter"])

        names = [Path(a).name for a in composed.attachments]

        assert "resume.pdf" in names
        assert "cover.pdf" in names

    def test_contact_details_are_appended(self, session, job, profile, documents):
        composed = EmailComposer(session).compose(
            job, "careers@acme.test", profile, documents["resume"], documents["cover_letter"])

        assert profile.email in composed.body

    def test_missing_cover_letter_produces_a_factual_note(self, session, job, profile, documents):
        """
        With no cover letter the body must not invent prose about the
        candidate — the resume speaks for that.
        """
        composed = EmailComposer(session).compose(
            job, "careers@acme.test", profile, documents["resume"], None)

        assert "Senior Backend Engineer" in composed.body
        assert "resume is attached" in composed.body
        assert any("no tailored cover letter" in w.lower() for w in composed.warnings)

    def test_unverified_document_is_reported(self, session, job, profile, documents):
        documents["resume"].fabrication_flags = ["Number '60%' does not appear..."]
        session.commit()

        composed = EmailComposer(session).compose(
            job, "careers@acme.test", profile, documents["resume"], documents["cover_letter"])

        assert documents["resume"].id in composed.unverified_documents
        assert any("not supported by your master" in w for w in composed.warnings)

    def test_missing_pdf_is_reported(self, session, job, profile, documents):
        documents["resume"].pdf_path = "/tmp/definitely-not-here.pdf"
        session.commit()

        composed = EmailComposer(session).compose(
            job, "careers@acme.test", profile, documents["resume"], documents["cover_letter"])

        assert any("missing from disk" in w for w in composed.warnings)


# ============================================================================
# Draft → approve → send
# ============================================================================

class TestDraftLifecycle:
    """Acceptance: drafts stop for review; only approval releases them."""

    def test_draft_is_created_for_review(self, service, job, profile, documents):
        draft = service.create_draft(job)

        assert draft.status == EmailDraftStatus.DRAFT
        assert draft.reviewed_by_user is False
        assert draft.to_email == "careers@acme-robotics.test"
        assert draft.sent_at is None

    def test_drafting_is_audited_as_paused(self, service, session, job, documents):
        service.create_draft(job)

        entry = session.query(AuditLog).filter(
            AuditLog.action == AuditAction.EMAIL_DRAFTED).one()

        assert entry.result == "paused"

    def test_unapproved_draft_cannot_be_sent(self, service, job, documents):
        """The core rule: no path from DRAFT to SENT."""
        draft = service.create_draft(job)

        with pytest.raises(EmailApplicationError, match="not been approved"):
            service.send(draft)

        assert service.sender.sent == []

    def test_approved_draft_sends(self, service, job, profile, documents):
        draft = service.create_draft(job)
        service.approve(draft)

        sent = service.send(draft)

        assert sent.status == EmailDraftStatus.SENT
        assert sent.sent_at is not None
        assert sent.sent_message_id
        assert sent.send_method == "smtp"

    def test_sending_delivers_the_reviewed_content(self, service, job, profile, documents):
        draft = service.create_draft(job)
        service.approve(draft)
        service.send(draft)

        delivered = service.sender.sent[0]

        assert delivered["to"] == "careers@acme-robotics.test"
        assert "Senior Backend Engineer" in delivered["subject"]
        assert len(delivered["attachments"]) == 2

    def test_sending_twice_is_refused(self, service, job, documents):
        draft = service.create_draft(job)
        service.approve(draft)
        service.send(draft)

        with pytest.raises(EmailApplicationError, match="Already sent"):
            service.send(draft)

        assert len(service.sender.sent) == 1

    def test_discarded_draft_cannot_be_approved(self, service, job, documents):
        draft = service.create_draft(job)
        service.discard(draft, reason="Wrong company")

        with pytest.raises(EmailApplicationError, match="discarded"):
            service.approve(draft)

    def test_send_failure_is_recorded_not_raised(self, session, job, documents):
        service = EmailApplicationService(session, sender=FakeSender(succeed=False))
        draft = service.create_draft(job)
        service.approve(draft)

        result = service.send(draft)

        assert result.status == EmailDraftStatus.FAILED
        assert "SMTP refused" in result.error_message

    def test_failure_is_audited(self, session, job, documents):
        service = EmailApplicationService(session, sender=FakeSender(succeed=False))
        draft = service.create_draft(job)
        service.approve(draft)
        service.send(draft)

        entry = session.query(AuditLog).filter(
            AuditLog.action == AuditAction.EMAIL_SEND_FAILED).one()

        assert entry.result == "failure"

    def test_invalid_recipient_is_refused(self, service, job):
        with pytest.raises(EmailApplicationError, match="not a valid email"):
            service.create_draft(job, to_email="not-an-address")

    def test_undetectable_recipient_is_refused(self, service, session):
        record = Job(
            platform="p", external_id="1", title="T", company="C", location="L",
            description="Apply on our website.", apply_method="web_form", dedup_hash="h9",
        )
        session.add(record)
        session.commit()
        session.refresh(record)

        with pytest.raises(EmailApplicationError, match="No application email"):
            service.create_draft(record)


class TestApplicationLinkage:
    """A sent email updates the application it belongs to."""

    def test_sending_marks_the_application(self, service, session, job, documents):
        account = PlatformAccount(platform="generic_ats", profile_dir="/tmp/x")
        session.add(account)
        session.commit()
        session.refresh(account)

        application = Application(
            job_id=job.id, platform_account_id=account.id,
            submission_status=ApplicationStatus.QUEUED_FOR_REVIEW,
        )
        session.add(application)
        session.commit()
        session.refresh(application)

        draft = service.create_draft(job, application=application)
        service.approve(draft)
        service.send(draft)

        session.refresh(application)

        assert application.submission_status == ApplicationStatus.EMAIL_SENT
        assert application.recipient_email == "careers@acme-robotics.test"
        assert application.email_message_id == draft.sent_message_id
        assert application.submitted_at is not None

    def test_sending_opens_a_thread_for_reply_tracking(self, service, session, job, documents):
        draft = service.create_draft(job)
        service.approve(draft)
        service.send(draft)

        thread = session.query(EmailThread).one()

        assert thread.recipient_email == "careers@acme-robotics.test"
        assert thread.thread_status == EmailThreadStatus.AWAITING_REPLY
        assert thread.sent_message_id == draft.sent_message_id


# ============================================================================
# Rate limiting
# ============================================================================

class TestRateLimiting:
    """A loop bug must not fire a hundred emails at one company."""

    def test_quota_reflects_recent_sends(self, service, session, job, documents):
        from job_agent.config import settings

        assert service.remaining_hourly_quota() == settings.email_rate_limit

        draft = service.create_draft(job)
        service.approve(draft)
        service.send(draft)

        assert service.remaining_hourly_quota() == settings.email_rate_limit - 1

    def test_limit_blocks_further_sends(self, service, session, job, documents, monkeypatch):
        from job_agent.config import settings

        monkeypatch.setattr(settings, "email_rate_limit", 1)

        first = service.create_draft(job)
        service.approve(first)
        service.send(first)

        second = service.create_draft(job)
        service.approve(second)

        with pytest.raises(EmailApplicationError, match="Hourly email limit"):
            service.send(second)

        assert len(service.sender.sent) == 1

    def test_older_sends_do_not_count(self, service, session, job, documents, monkeypatch):
        from job_agent.config import settings

        monkeypatch.setattr(settings, "email_rate_limit", 1)

        stale = EmailDraft(
            job_id=job.id, to_email="old@acme.test", subject="s", body="b",
            status=EmailDraftStatus.SENT, sent_at=utcnow() - timedelta(hours=2),
        )
        session.add(stale)
        session.commit()

        assert service.remaining_hourly_quota() == 1


# ============================================================================
# Sender internals
# ============================================================================

class TestSenderSafety:
    """Credential handling and escaping."""

    def test_smtp_without_configuration_fails_clearly(self, monkeypatch):
        from job_agent.config import settings

        monkeypatch.setattr(settings, "smtp_host", None)
        monkeypatch.setattr(settings, "smtp_username", None)

        sender = EmailSender(prefer_mail_app=False, allow_smtp=True)
        result = sender.send("a@b.test", "s", "b")

        assert not result.sent
        assert "app-specific password" in result.error_message

    def test_smtp_without_keychain_password_names_the_fix(self, monkeypatch):
        """The error must steer the user to an app password, not their real one."""
        from job_agent.config import settings

        monkeypatch.setattr(settings, "smtp_host", "smtp.example.test")
        monkeypatch.setattr(settings, "smtp_username", "user@example.test")
        monkeypatch.setattr(
            "job_agent.services.email_sender.retrieve_app_password", lambda s, u: None)

        sender = EmailSender(prefer_mail_app=False, allow_smtp=True)
        result = sender.send("a@b.test", "s", "b")

        assert not result.sent
        assert "never" in result.error_message.lower()
        assert "account password" in result.error_message

    def test_applescript_escaping_handles_quotes_and_newlines(self):
        escaped = EmailSender._applescript_escape('He said "hi"\nSecond line\\path')

        assert '\\"hi\\"' in escaped
        assert "\\n" in escaped
        assert "\\\\path" in escaped
        assert "\n" not in escaped  # real newlines would break the literal

    def test_applescript_includes_recipient_and_attachments(self, tmp_path):
        attachment = tmp_path / "resume.pdf"
        attachment.write_bytes(b"%PDF-1.4")

        script = EmailSender._build_applescript(
            "careers@acme.test", "Application", "Body text", [str(attachment)])

        assert 'address:"careers@acme.test"' in script
        assert "resume.pdf" in script
        assert "send" in script

    @pytest.mark.parametrize("address,valid", [
        ("careers@acme.test", True),
        ("first.last+tag@sub.acme.co.uk", True),
        ("not-an-address", False),
        ("@acme.test", False),
        ("careers@", False),
        ("", False),
    ])
    def test_address_validation(self, address, valid):
        assert EmailSender.is_valid_address(address) is valid

    def test_no_send_method_available(self):
        sender = EmailSender(prefer_mail_app=False, allow_smtp=False)
        result = sender.send("a@b.test", "s", "b")

        assert not result.sent
        assert "No send method" in result.error_message


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
