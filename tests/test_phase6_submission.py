#!/usr/bin/env python3
"""
Tests for Phase 6: Auto-Submit Path & Clean Submissions Gate.

Covers the Phase 6 acceptance criteria:
- Three reviewed submissions raise clean_submissions_count to 3
- The 4th application auto-submits under search_fill_submit, or queues under
  a fill-only mode
- Confirmation is captured and linked to the application record
- The daily counter stops submissions at the configured limit

The gate is the safety-critical part of this phase: it decides when the agent
may send an application to a real employer with nobody watching.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from job_agent.connectors.base import PlatformCapabilities
from job_agent.models.database import (
    Application,
    ApplicationStatus,
    AuditAction,
    AuditLog,
    AutomationMode,
    DocumentFormat,
    DocumentType,
    DocumentVersion,
    Job,
    MasterDocument,
    PlatformAccount,
)
from job_agent.services.submission_gate import SubmissionGate
from job_agent.services.submission_recorder import SubmissionRecorder
from job_agent.services.submitter import ApplicationSubmitter, SubmissionOutcome
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
def job(session) -> Job:
    """A stored job."""
    record = Job(
        platform="generic_ats", external_id="REQ-1", title="Senior Backend Engineer",
        company="Acme Robotics", location="Remote", description="Kafka",
        apply_method="web_form", dedup_hash="h1",
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


@pytest.fixture
def account(session) -> PlatformAccount:
    """A platform account opted in to auto-submit, with a proven record."""
    record = PlatformAccount(
        platform="generic_ats",
        profile_dir="/tmp/job-agent-test",
        automation_mode=AutomationMode.SEARCH_FILL_SUBMIT,
        clean_submissions_count=3,
        daily_apply_limit=5,
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


def make_application(session, job, account, **overrides) -> Application:
    """Create a reviewed, complete application ready to submit."""
    defaults = dict(
        job_id=job.id,
        platform_account_id=account.id,
        filled_fields={"Full name": {"value": "Alex Rivera"}},
        deferred_fields={
            "Expected salary": {
                "category": "sensitive", "required": True,
                "value_entered_by_user": "180000", "question": "Expected salary",
            },
        },
        submission_status=ApplicationStatus.QUEUED_FOR_REVIEW,
        reviewed_by_user=True,
        form_url="http://127.0.0.1:9/apply",
    )
    defaults.update(overrides)

    application = Application(**defaults)
    session.add(application)
    session.commit()
    session.refresh(application)
    return application


class FakeConnector:
    """Stands in for a connector's capability declaration."""

    def __init__(self, can_submit: bool = True, tos_note: str = None):
        self.platform_name = "generic_ats"
        self.capabilities = PlatformCapabilities(
            can_submit_automatically=can_submit, tos_risk_note=tos_note
        )


def flagged_document(session, job) -> DocumentVersion:
    """A tailored resume carrying unsupported claims."""
    master = MasterDocument(
        doc_type=DocumentType.RESUME, name="m", source_path="/tmp/m.txt",
        source_format=DocumentFormat.TXT, content_text="master text",
    )
    session.add(master)
    session.commit()
    session.refresh(master)

    version = DocumentVersion(
        master_document_id=master.id, job_id=job.id, doc_type=DocumentType.RESUME,
        content_text="tailored", pdf_path="/tmp/r.pdf", generator="llm:test",
        fabrication_flags=["Number '60%' does not appear in the master document"],
    )
    session.add(version)
    session.commit()
    session.refresh(version)
    return version


# ============================================================================
# The gate
# ============================================================================

class TestUserDirectedGate:
    """What the user may submit while looking at it."""

    def test_complete_application_may_be_submitted(self, session, job, account):
        application = make_application(session, job, account)

        decision = SubmissionGate(session).check_user_directed(application)

        assert decision.allowed
        assert "all required questions answered" in decision.checks_passed

    def test_unanswered_required_question_blocks(self, session, job, account):
        application = make_application(session, job, account, deferred_fields={
            "Why do you want to work here?": {
                "category": "unknown", "required": True, "value_entered_by_user": None,
            },
        })

        decision = SubmissionGate(session).check_user_directed(application)

        assert not decision.allowed
        assert any("required question" in b for b in decision.blockers)

    def test_already_submitted_blocks(self, session, job, account):
        application = make_application(
            session, job, account,
            submission_status=ApplicationStatus.SUBMITTED, submitted_at=utcnow(),
        )

        decision = SubmissionGate(session).check_user_directed(application)

        assert not decision.allowed
        assert any("Already submitted" in b for b in decision.blockers)

    def test_discarded_application_blocks(self, session, job, account):
        application = make_application(
            session, job, account, submission_status=ApplicationStatus.REJECTED)

        decision = SubmissionGate(session).check_user_directed(application)

        assert not decision.allowed

    def test_unverified_document_blocks_even_the_user_path(self, session, job, account):
        """
        A resume with claims the master doesn't support must not reach an
        employer, whoever pressed the button.
        """
        version = flagged_document(session, job)
        application = make_application(session, job, account, resume_version_id=version.id)

        decision = SubmissionGate(session).check_user_directed(application)

        assert not decision.allowed
        assert any("not supported by your master" in b for b in decision.blockers)

    def test_missing_documents_warn_but_do_not_block(self, session, job, account):
        application = make_application(session, job, account)

        decision = SubmissionGate(session).check_user_directed(application)

        assert decision.allowed
        assert any("No tailored documents" in w for w in decision.warnings)


class TestAutoSubmitGate:
    """What the agent may submit with nobody watching."""

    def test_all_conditions_met_allows_auto_submit(self, session, job, account):
        application = make_application(session, job, account)

        decision = SubmissionGate(session).check_auto(
            application, account, FakeConnector())

        assert decision.allowed, decision.blockers

    def test_fill_only_mode_blocks(self, session, job, account):
        """Acceptance: fill_only queues for review instead of submitting."""
        account.automation_mode = AutomationMode.SEARCH_AND_PREPARE
        session.commit()
        application = make_application(session, job, account)

        decision = SubmissionGate(session).check_auto(
            application, account, FakeConnector())

        assert not decision.allowed
        assert any("automation mode" in b.lower() for b in decision.blockers)

    @pytest.mark.parametrize("count", [0, 1, 2])
    def test_below_threshold_blocks(self, session, job, account, count):
        """The first three applications on a platform always go to review."""
        account.clean_submissions_count = count
        session.commit()
        application = make_application(session, job, account)

        decision = SubmissionGate(session).check_auto(
            application, account, FakeConnector())

        assert not decision.allowed
        assert any("reviewed submissions" in b for b in decision.blockers)

    def test_threshold_reached_passes_that_check(self, session, job, account):
        application = make_application(session, job, account)

        decision = SubmissionGate(session).check_auto(
            application, account, FakeConnector())

        assert any("3 reviewed submissions" in c for c in decision.checks_passed)

    def test_connector_without_capability_blocks(self, session, job, account):
        application = make_application(session, job, account)

        decision = SubmissionGate(session).check_auto(
            application, account, FakeConnector(can_submit=False))

        assert not decision.allowed
        assert any("does not support automatic submission" in b for b in decision.blockers)

    def test_tos_risk_note_is_surfaced_as_a_warning(self, session, job, account):
        application = make_application(session, job, account)

        decision = SubmissionGate(session).check_auto(
            application, account, FakeConnector(tos_note="Consumer board: review ToS"))

        assert any("review ToS" in w for w in decision.warnings)

    def test_daily_limit_blocks(self, session, job, account):
        """Acceptance: the daily counter stops submissions at the limit."""
        account.daily_apply_limit = 2
        session.commit()

        for _ in range(2):
            make_application(
                session, job, account,
                submission_status=ApplicationStatus.SUBMITTED, submitted_at=utcnow(),
            )

        application = make_application(session, job, account)
        decision = SubmissionGate(session).check_auto(
            application, account, FakeConnector())

        assert not decision.allowed
        assert any("Daily apply limit" in b for b in decision.blockers)

    def test_yesterdays_submissions_do_not_count(self, session, job, account):
        from datetime import timedelta

        account.daily_apply_limit = 1
        session.commit()

        make_application(
            session, job, account,
            submission_status=ApplicationStatus.SUBMITTED,
            submitted_at=utcnow() - timedelta(days=1),
        )

        application = make_application(session, job, account)
        decision = SubmissionGate(session).check_auto(
            application, account, FakeConnector())

        assert decision.allowed, decision.blockers

    def test_blank_sensitive_questions_warn_but_do_not_block(self, session, job, account):
        """Declining an optional demographic question is a valid choice."""
        application = make_application(session, job, account, deferred_fields={
            "Gender": {
                "category": "sensitive", "required": False,
                "value_entered_by_user": None, "question": "Gender",
            },
        })

        decision = SubmissionGate(session).check_auto(
            application, account, FakeConnector())

        assert decision.allowed
        assert any("submitted blank" in w for w in decision.warnings)

    def test_describe_reports_both_paths(self, session, job, account):
        application = make_application(session, job, account)

        described = SubmissionGate(session).describe(
            application, account, FakeConnector())

        assert described["clean_submissions_count"] == 3
        assert described["clean_submissions_threshold"] == 3
        assert described["user_directed"]["allowed"] is True
        assert described["auto"]["allowed"] is True


# ============================================================================
# Recording and the clean-submission count
# ============================================================================

class TestSubmissionRecorder:
    """What a submission attempt does to the record."""

    def _clean_outcome(self) -> SubmissionOutcome:
        return SubmissionOutcome(
            submitted=True, confirmed=True,
            confirmation_ref="TEST-12345",
            confirmation_url="http://example.test/thanks",
            confirmation_message="Application received.",
        )

    def test_successful_submission_updates_the_application(self, session, job, account):
        application = make_application(session, job, account)

        SubmissionRecorder(session).record(application, account, self._clean_outcome())

        assert application.submission_status == ApplicationStatus.SUBMITTED
        assert application.submitted_at is not None
        assert application.confirmation_ref == "TEST-12345"
        assert application.confirmation_url == "http://example.test/thanks"

    def test_reviewed_clean_submission_advances_the_count(self, session, job, account):
        account.clean_submissions_count = 0
        session.commit()
        application = make_application(session, job, account)

        SubmissionRecorder(session).record(application, account, self._clean_outcome())

        assert account.clean_submissions_count == 1

    def test_three_reviewed_submissions_reach_the_threshold(self, session, job, account):
        """Acceptance: three manual submissions take the count to 3."""
        account.clean_submissions_count = 0
        session.commit()
        recorder = SubmissionRecorder(session)

        for _ in range(3):
            application = make_application(session, job, account)
            recorder.record(application, account, self._clean_outcome())

        assert account.clean_submissions_count == 3

        fourth = make_application(session, job, account)
        assert SubmissionGate(session).check_auto(
            fourth, account, FakeConnector()).allowed

    def test_unconfirmed_submission_stays_in_the_tray(self, session, job, account):
        """
        Clicking submit is not the employer receiving anything.

        Recorded as SUBMITTED, an unconfirmed click tells the user the job was
        applied for and locks the application so it can never be sent again —
        which is exactly what happened on a real form whose submit silently
        did nothing. It stays reviewable so the send can be retried.
        """
        account.clean_submissions_count = 0
        session.commit()
        application = make_application(session, job, account)

        outcome = SubmissionOutcome(submitted=True, confirmed=False)
        SubmissionRecorder(session).record(application, account, outcome)

        assert application.submission_status == ApplicationStatus.QUEUED_FOR_REVIEW
        assert application.submitted_at is None
        assert account.clean_submissions_count == 0

    def test_validation_errors_do_not_advance_the_count(self, session, job, account):
        account.clean_submissions_count = 0
        session.commit()
        application = make_application(session, job, account)

        outcome = SubmissionOutcome(
            submitted=True, confirmed=True,
            validation_errors=["This field is required"],
        )
        SubmissionRecorder(session).record(application, account, outcome)

        assert account.clean_submissions_count == 0

    def test_auto_submitted_application_does_not_advance_the_count(
        self, session, job, account
    ):
        """
        The gate must not certify itself: an application submitted without
        human review cannot raise the threshold that permitted it.
        """
        account.clean_submissions_count = 3
        session.commit()
        application = make_application(session, job, account, reviewed_by_user=False)

        SubmissionRecorder(session).record(
            application, account, self._clean_outcome(), initiated_by="agent")

        assert account.clean_submissions_count == 3

    def test_failed_submission_leaves_it_in_the_queue(self, session, job, account):
        application = make_application(session, job, account)

        outcome = SubmissionOutcome(
            submitted=False, error_message="No submit control found on the form")
        SubmissionRecorder(session).record(application, account, outcome)

        assert application.submission_status == ApplicationStatus.QUEUED_FOR_REVIEW
        assert application.submitted_at is None

    def test_submission_is_audited(self, session, job, account):
        application = make_application(session, job, account)

        SubmissionRecorder(session).record(
            application, account, self._clean_outcome(), initiated_by="user")

        entry = session.query(AuditLog).filter(
            AuditLog.action == AuditAction.APPLICATION_SUBMITTED).one()

        assert entry.actor == "user"
        assert entry.result == "success"
        assert entry.detail_json["confirmation_ref"] == "TEST-12345"

    def test_unconfirmed_submission_is_audited_as_partial(self, session, job, account):
        application = make_application(session, job, account)

        SubmissionRecorder(session).record(
            application, account, SubmissionOutcome(submitted=True, confirmed=False))

        entry = session.query(AuditLog).filter(
            AuditLog.action == AuditAction.APPLICATION_SUBMITTED).one()

        assert entry.result == "partial"
        assert "nothing confirmed the send" in entry.detail

    def test_failure_is_audited_as_an_error(self, session, job, account):
        application = make_application(session, job, account)

        SubmissionRecorder(session).record(
            application, account,
            SubmissionOutcome(submitted=False, error_message="boom"))

        entry = session.query(AuditLog).filter(
            AuditLog.action == AuditAction.ERROR_OCCURRED).one()

        assert entry.result == "failure"


# ============================================================================
# Submitting a real form in a real browser
# ============================================================================

CONFIRMING_FORM = """
<!DOCTYPE html><html><head><meta charset="utf-8"><title>Apply</title></head><body>
<form id="f">
  <label for="n">Full name</label><input id="n" name="name" required>
  <button type="submit">Submit Application</button>
</form>
<script>
document.getElementById('f').addEventListener('submit', function (e) {
  e.preventDefault();
  document.body.innerHTML =
    '<h1>Thank you for applying</h1>' +
    '<p>Your application has been received. Confirmation number: ACME-88421</p>';
});
</script></body></html>
"""

REJECTING_FORM = """
<!DOCTYPE html><html><head><meta charset="utf-8"><title>Apply</title></head><body>
<form id="f">
  <label for="n">Full name</label><input id="n" name="name">
  <button type="submit">Submit Application</button>
</form>
<div id="err"></div>
<script>
document.getElementById('f').addEventListener('submit', function (e) {
  e.preventDefault();
  document.getElementById('err').textContent = 'This field is required: Full name';
});
</script></body></html>
"""

SILENT_FORM = """
<!DOCTYPE html><html><head><meta charset="utf-8"><title>Apply</title></head><body>
<form id="f">
  <label for="n">Full name</label><input id="n" name="name">
  <button type="submit">Submit Application</button>
</form>
<script>
document.getElementById('f').addEventListener('submit', (e) => e.preventDefault());
</script></body></html>
"""


@pytest.fixture
def form_server():
    """Serve whichever form the test asks for by path."""
    pages = {
        "/confirm": CONFIRMING_FORM,
        "/reject": REJECTING_FORM,
        "/silent": SILENT_FORM,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = pages.get(self.path, "<html><body>no form</body></html>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://127.0.0.1:{server.server_port}"

    server.shutdown()
    thread.join(timeout=5)


@pytest_asyncio.fixture
async def browser():
    """A Chromium instance, or skip."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        try:
            instance = await p.chromium.launch()
        except Exception as e:
            pytest.skip(f"No browser available: {e}")

        yield instance
        await instance.close()


@pytest.mark.asyncio
class TestRealSubmission:
    """The submitter against real pages."""

    async def test_confirmation_is_captured(self, browser, form_server, tmp_path):
        """Acceptance: the confirmation reference is captured."""
        page = await browser.new_page()
        await page.goto(f"{form_server}/confirm")
        await page.fill("#n", "Alex Rivera")

        outcome = await ApplicationSubmitter(screenshots_dir=tmp_path).submit(page)

        assert outcome.submitted
        assert outcome.confirmed
        assert outcome.confirmation_ref == "ACME-88421"
        assert "received" in outcome.confirmation_message.lower()
        assert outcome.is_clean

    async def test_before_and_after_screenshots_are_captured(
        self, browser, form_server, tmp_path
    ):
        page = await browser.new_page()
        await page.goto(f"{form_server}/confirm")

        outcome = await ApplicationSubmitter(screenshots_dir=tmp_path).submit(page)

        assert Path(outcome.before_screenshot).exists()
        assert Path(outcome.after_screenshot).exists()

    async def test_validation_errors_are_detected(self, browser, form_server, tmp_path):
        """A form that rejects the submission must not look like a success."""
        page = await browser.new_page()
        await page.goto(f"{form_server}/reject")

        outcome = await ApplicationSubmitter(screenshots_dir=tmp_path).submit(page)

        assert outcome.submitted
        assert not outcome.confirmed
        assert outcome.validation_errors
        assert not outcome.is_clean

    async def test_silent_form_is_reported_unconfirmed(
        self, browser, form_server, tmp_path
    ):
        """
        A click that changes nothing must be reported honestly rather than
        assumed successful.
        """
        page = await browser.new_page()
        await page.goto(f"{form_server}/silent")

        outcome = await ApplicationSubmitter(screenshots_dir=tmp_path).submit(page)

        assert outcome.submitted
        assert not outcome.confirmed
        assert not outcome.is_clean

    async def test_missing_submit_control_is_reported(self, browser, form_server, tmp_path):
        page = await browser.new_page()
        await page.goto(f"{form_server}/none")

        outcome = await ApplicationSubmitter(screenshots_dir=tmp_path).submit(page)

        assert not outcome.submitted
        assert "No submit control" in outcome.error_message

    async def test_connector_submit_returns_confirmation(
        self, browser, form_server, tmp_path
    ):
        from job_agent.connectors.base import ApplicationSession, JobPosting
        from job_agent.connectors.generic_ats import GenericATSConnector

        page = await browser.new_page()
        await page.goto(f"{form_server}/confirm")
        await page.fill("#n", "Alex Rivera")

        connector = GenericATSConnector()
        connector.set_page(page)

        posting = JobPosting(
            platform="generic_ats", external_id="1", title="T", company="C", location="L")
        app_session = ApplicationSession(
            job=posting, platform_account_id=1,
            form_state={"screenshots_dir": tmp_path},
        )

        result = await connector.submit_application(app_session)

        assert result.success
        assert result.confirmation_ref == "ACME-88421"

    async def test_connector_reports_rejection_as_failure(
        self, browser, form_server, tmp_path
    ):
        from job_agent.connectors.base import ApplicationSession, JobPosting
        from job_agent.connectors.generic_ats import GenericATSConnector

        page = await browser.new_page()
        await page.goto(f"{form_server}/reject")

        connector = GenericATSConnector()
        connector.set_page(page)

        posting = JobPosting(
            platform="generic_ats", external_id="1", title="T", company="C", location="L")
        result = await connector.submit_application(
            ApplicationSession(job=posting, platform_account_id=1,
                               form_state={"screenshots_dir": tmp_path}))

        assert not result.success
        assert "rejected the submission" in result.error_message


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
