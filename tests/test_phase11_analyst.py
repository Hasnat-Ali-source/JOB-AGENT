#!/usr/bin/env python3
"""
Tests for Phase 11: the pre-submission analyst.

The analyst is the last reader before an application reaches an employer, and
the only check in the pipeline that judges the finished thing rather than the
step that made it. Two properties matter and are tested here:

- It catches the failures that reached a real employer's form: a rewritten
  resume with no contact details, the same job listed twice, an answer that is
  not one of the dropdown's choices.
- It does not block a sound application. A gate that refuses good work gets
  routed around, and then it protects nothing.

The fixtures build applications the way the pipeline does — a master document,
a tailored version with a rendered PDF, filled and deferred fields — so the
checks run against the same shapes they see in production.
"""

from pathlib import Path

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from job_agent.config import settings
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
from job_agent.services.application_analyst import ApplicationAnalyst
from job_agent.services.pdf_renderer import PdfRenderer
from job_agent.services.submission_gate import SubmissionGate

MASTER_RESUME = """Hasnat Ali
Petaling Jaya, Malaysia | hasnat@example.com | +60 10 421 5890

Summary
Customer service professional with 4+ years of experience in resolving
complex issues and delivering exceptional support.

Experience
Customer Service Representative / Trainer | Mindbridge Pvt. Ltd. | Aug 2023 – May 2026
- Delivered high-quality customer support through calls, chats, and emails,
  achieving 95%+ customer satisfaction scores.
- Trained and mentored two batches of new CSR employees, improving onboarding
  efficiency and team performance.
- Resolved complex customer issues, ensuring quick turnaround and reducing
  escalation rates.

Education
Bachelor of Science in Computer Science (BSCS) | University of Lahore | 2017 – 2021

Certifications
ASP.NET Certification – PNY Training
"""

# What a good rewrite looks like: same header, same credentials, bullets
# reordered for the posting.
GOOD_TAILORED = MASTER_RESUME

# The rewrite that actually went out: the name survived, the header did not.
HEADERLESS_TAILORED = MASTER_RESUME.replace(
    "Petaling Jaya, Malaysia | hasnat@example.com | +60 10 421 5890",
    "linkedin.com/in/hasnat-ali-tahir",
)

# The other half of that failure: one job written twice under two headings.
DUPLICATED_TAILORED = MASTER_RESUME.replace(
    "Experience\nCustomer Service Representative / Trainer | Mindbridge Pvt. Ltd. | Aug 2023 – May 2026",
    "Experience\n"
    "Dedicated Customer Service Representative (CSR), Mindbridge Pvt. Ltd. | Aug 2023 – May 2026\n"
    "- Managed and resolved end-to-end employee exits.\n"
    "\n"
    "Professional Experience\n"
    "Customer Service Representative / Trainer | Mindbridge Pvt. Ltd. | Aug 2023 – May 2026",
)

# The same person's header and credentials over somebody else's career: the
# document is honest and has nothing to do with the posting.
UNRELATED_TAILORED = """Hasnat Ali
Petaling Jaya, Malaysia | hasnat@example.com | +60 10 421 5890

Summary
Agricultural technician with four seasons of field experience.

Experience
Field Technician | Greenacre Farms Ltd. | Aug 2023 – May 2026
- Maintained irrigation equipment across two hundred hectares.
- Operated a combine harvester through two harvest seasons.
- Managed soil sampling schedules and logged yield data.

Education
Bachelor of Science in Computer Science (BSCS) | University of Lahore | 2017 – 2021

Certifications
ASP.NET Certification – PNY Training
"""

POSTING = """
We are hiring a Customer Service Representative.

- Experience delivering customer support over calls, chats and emails
- Experience training and mentoring new customer service employees
- Track record of resolving complex customer issues and reducing escalations
- Bachelor's degree
"""


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def session():
    """In-memory database."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)

    with Session(engine) as db_session:
        yield db_session


@pytest.fixture
def job(session) -> Job:
    """The posting being applied to."""
    record = Job(
        platform="greenhouse",
        external_id="REQ-1",
        title="Customer Service Representative",
        company="Acme Robotics",
        location="Remote",
        description=POSTING,
        apply_method="web_form",
        dedup_hash="h1",
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


@pytest.fixture
def account(session) -> PlatformAccount:
    """A platform account with a proven track record."""
    record = PlatformAccount(
        platform="greenhouse",
        profile_dir="/tmp/job-agent-test",
        automation_mode=AutomationMode.SEARCH_FILL_SUBMIT,
        clean_submissions_count=3,
        daily_apply_limit=5,
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


@pytest.fixture
def master(session) -> MasterDocument:
    """The user's real resume."""
    record = MasterDocument(
        doc_type=DocumentType.RESUME,
        name="Hasnat_Ali_CSR_Resume",
        source_path="/tmp/master.txt",
        source_format=DocumentFormat.TXT,
        content_text=MASTER_RESUME,
        is_active=True,
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


@pytest.fixture
def make_application(session, job, account, master, tmp_path):
    """
    Build an application the way the pipeline does.

    The PDF is really rendered, because one of the checks reads it back —
    a stub path would leave that check permanently untested.
    """
    def _make(
        resume_text: str = GOOD_TAILORED,
        cover_letter_text: str = None,
        deferred: dict = None,
        render_pdf: bool = True,
        **overrides,
    ) -> Application:
        version = DocumentVersion(
            master_document_id=master.id,
            job_id=job.id,
            doc_type=DocumentType.RESUME,
            content_text=resume_text,
            generator="deterministic",
        )
        session.add(version)
        session.commit()
        session.refresh(version)

        if render_pdf:
            pdf_path = tmp_path / f"resume_v{version.id}.pdf"
            PdfRenderer().render(resume_text, pdf_path, title="resume")
            version.pdf_path = str(pdf_path)
            session.commit()

        cover = None

        if cover_letter_text is not None:
            cover = DocumentVersion(
                master_document_id=master.id,
                job_id=job.id,
                doc_type=DocumentType.COVER_LETTER,
                content_text=cover_letter_text,
                generator="deterministic",
            )
            session.add(cover)
            session.commit()
            session.refresh(cover)

        application = Application(
            job_id=job.id,
            platform_account_id=account.id,
            resume_version_id=version.id,
            cover_letter_version_id=cover.id if cover else None,
            filled_fields={
                "First Name": {"value": "Hasnat", "selector": "#first_name"},
                "Attach resume": {
                    "value": version.pdf_path, "selector": "#resume",
                },
            },
            deferred_fields=deferred if deferred is not None else {
                "Are you legally eligible to work in this country?": {
                    "selector": "#q1",
                    "required": True,
                    "field_type": "text",
                    "options": ["Yes", "No"],
                    "value_entered_by_user": "Yes",
                },
            },
            submission_status=ApplicationStatus.QUEUED_FOR_REVIEW,
            reviewed_by_user=True,
            form_url="https://example.test/apply",
            **overrides,
        )
        session.add(application)
        session.commit()
        session.refresh(application)
        return application

    return _make


def blockers(report) -> list:
    """The slugs of everything blocking, for readable assertions."""
    return [f.check for f in report.blockers]


def warnings(report) -> list:
    """The slugs of everything worth seeing."""
    return [f.check for f in report.warnings]


# ============================================================================
# A sound application passes
# ============================================================================

class TestSoundApplication:
    """The analyst must not stand in the way of good work."""

    def test_a_complete_application_is_ready(self, session, make_application):
        report = ApplicationAnalyst(session).analyse(make_application())

        assert report.ready, f"blocked by {blockers(report)}"

    def test_it_reports_what_it_checked(self, session, make_application):
        report = ApplicationAnalyst(session).analyse(make_application())

        assert "the resume carries an email address" in report.checks_passed
        assert "the PDF matches the text that was reviewed" in report.checks_passed

    def test_it_scores_the_application(self, session, make_application):
        report = ApplicationAnalyst(session).analyse(make_application())

        assert report.ats_score is not None
        assert report.fit_score is not None and report.fit_score > 0


# ============================================================================
# The failures that reached a real form
# ============================================================================

class TestResumeDefects:
    """Each of these was found on an application that was about to be sent."""

    def test_a_resume_with_no_email_blocks(self, session, make_application):
        report = ApplicationAnalyst(session).analyse(
            make_application(resume_text=HEADERLESS_TAILORED)
        )

        assert "resume_reaches_you" in blockers(report)
        assert "resume_keeps_phone" in warnings(report)

    def test_the_same_job_listed_twice_blocks(self, session, make_application):
        report = ApplicationAnalyst(session).analyse(
            make_application(resume_text=DUPLICATED_TAILORED)
        )

        assert "no_repeated_history" in blockers(report)

    def test_a_dropped_degree_blocks(self, session, make_application):
        stripped = MASTER_RESUME.replace(
            "Bachelor of Science in Computer Science (BSCS) | University of Lahore | 2017 – 2021",
            "University of Lahore | 2017 – 2021",
        )

        report = ApplicationAnalyst(session).analyse(
            make_application(resume_text=stripped)
        )

        assert "resume_keeps_credentials" in blockers(report)

    def test_template_placeholders_block(self, session, make_application):
        report = ApplicationAnalyst(session).analyse(
            make_application(
                resume_text=MASTER_RESUME.replace("Hasnat Ali", "[Your Name]")
            )
        )

        assert "no_placeholders" in blockers(report)

    def test_a_missing_pdf_blocks(self, session, make_application):
        report = ApplicationAnalyst(session).analyse(
            make_application(render_pdf=False)
        )

        assert "resume_attached" in blockers(report)

    def test_a_resume_tailored_for_another_job_blocks(
        self, session, make_application, job
    ):
        application = make_application()
        version = session.query(DocumentVersion).filter(
            DocumentVersion.id == application.resume_version_id
        ).first()
        version.job_id = job.id + 999
        session.commit()

        report = ApplicationAnalyst(session).analyse(application)

        assert "resume_is_for_this_job" in blockers(report)

    def test_a_pdf_that_lost_the_header_blocks(
        self, session, make_application, tmp_path
    ):
        # The reviewed text is sound and the rendered file is not — the case
        # no check that reads the database can see.
        application = make_application()
        version = session.query(DocumentVersion).filter(
            DocumentVersion.id == application.resume_version_id
        ).first()
        PdfRenderer().render(
            HEADERLESS_TAILORED, Path(version.pdf_path), title="resume"
        )

        report = ApplicationAnalyst(session).analyse(application)

        assert "pdf_matches_review" in blockers(report)


# ============================================================================
# The answers
# ============================================================================

class TestAnswers:
    """What will actually be submitted, not what was intended."""

    def test_an_answer_not_on_the_dropdown_blocks(self, session, make_application):
        # The failure this prevents: the answer is written to the form, the
        # dropdown discards it, and the question is submitted blank.
        report = ApplicationAnalyst(session).analyse(
            make_application(deferred={
                "Which country are you located in?": {
                    "selector": "#country",
                    "required": True,
                    "field_type": "text",
                    "options": ["Malaysia", "Singapore", "Thailand"],
                    "value_entered_by_user": "I live in Malaysia",
                },
            })
        )

        assert "answers_are_on_the_menu" in blockers(report)

    def test_whitespace_and_case_do_not_count_as_a_mismatch(
        self, session, make_application
    ):
        # EEO labels arrive with non-breaking spaces; a literal comparison
        # would reject an answer the user picked from the form's own list.
        report = ApplicationAnalyst(session).analyse(
            make_application(deferred={
                "What is your Race/Ethnicity?": {
                    "selector": "#race",
                    "required": False,
                    "field_type": "text",
                    "options": ["Asian (Not Hispanic or Latino):\xa0A person having origins…"],
                    "value_entered_by_user": (
                        "asian (not hispanic or latino): A person having origins…"
                    ),
                },
            })
        )

        assert "answers_are_on_the_menu" not in blockers(report)

    def test_an_unanswered_required_question_blocks(self, session, make_application):
        report = ApplicationAnalyst(session).analyse(
            make_application(deferred={
                "Are you legally eligible to work in this country?": {
                    "selector": "#q1",
                    "required": True,
                    "field_type": "text",
                    "options": ["Yes", "No"],
                    "value_entered_by_user": None,
                },
            })
        )

        assert "required_questions_answered" in blockers(report)

    def test_a_one_word_answer_to_an_open_question_warns(
        self, session, make_application
    ):
        report = ApplicationAnalyst(session).analyse(
            make_application(deferred={
                "Why do you want to work at this company in particular?": {
                    "selector": "#why",
                    "required": True,
                    "field_type": "textarea",
                    "options": [],
                    "value_entered_by_user": "Yes",
                },
            })
        )

        assert "answers_look_written" in warnings(report)


# ============================================================================
# The cover letter
# ============================================================================

class TestCoverLetter:
    """A field the form offers and the application leaves empty."""

    def test_a_missing_optional_letter_warns(self, session, make_application):
        report = ApplicationAnalyst(session).analyse(
            make_application(deferred={
                "Attach cover letter": {
                    "selector": "#cover_letter",
                    "required": False,
                    "field_type": "file",
                    "options": [],
                    "value_entered_by_user": None,
                },
            })
        )

        assert "cover_letter_present" in warnings(report)

    def test_a_missing_required_letter_blocks(self, session, make_application):
        report = ApplicationAnalyst(session).analyse(
            make_application(deferred={
                "Attach cover letter": {
                    "selector": "#cover_letter",
                    "required": True,
                    "field_type": "file",
                    "options": [],
                    "value_entered_by_user": None,
                },
            })
        )

        assert "cover_letter_present" in blockers(report)

    def test_a_letter_naming_the_wrong_company_blocks(
        self, session, make_application
    ):
        report = ApplicationAnalyst(session).analyse(
            make_application(
                cover_letter_text=(
                    "Dear Hiring Manager,\n\nI am writing to apply for the "
                    "Customer Service Representative position at Globex.\n\n"
                    "Sincerely,\nHasnat Ali"
                )
            )
        )

        assert "cover_letter_addresses_this_job" in blockers(report)

    def test_a_letter_naming_this_company_passes(self, session, make_application):
        report = ApplicationAnalyst(session).analyse(
            make_application(
                deferred={
                    "Attach cover letter": {
                        "selector": "#cover_letter",
                        "required": False,
                        "field_type": "file",
                        "options": [],
                        "value_entered_by_user": None,
                    },
                },
                cover_letter_text=(
                    "Dear Hiring Manager,\n\nI am writing to apply for the "
                    "Customer Service Representative position at Acme Robotics.\n\n"
                    "Sincerely,\nHasnat Ali"
                ),
            )
        )

        assert "cover_letter_addresses_this_job" not in blockers(report)
        assert "a cover letter is attached" in report.checks_passed


# ============================================================================
# Fit — the one judgement the user may overrule
# ============================================================================

class TestThePosting:
    """What the posting itself admits."""

    def test_a_talent_pipeline_posting_is_flagged(
        self, session, make_application, job
    ):
        # A real one: twenty questions answered, a resume tailored, and the
        # posting says in its own words that nobody is being hired.
        job.description = (
            POSTING
            + "\nThis is not an active job opening, but a way for us to "
              "connect with talent as we plan for future opportunities."
        )
        session.commit()

        report = ApplicationAnalyst(session).analyse(make_application())

        assert "posting_is_a_real_vacancy" in warnings(report)
        assert report.ready, "A talent pool is a legitimate thing to join"

    def test_an_ordinary_posting_is_not_flagged(self, session, make_application):
        report = ApplicationAnalyst(session).analyse(make_application())

        assert "posting_is_a_real_vacancy" not in warnings(report)


class TestFit:
    """Whether the application is worth sending is the user's call."""

    def test_an_unrelated_resume_blocks(self, session, make_application, monkeypatch):
        monkeypatch.setattr(settings, "analyst_min_fit_score", 25)

        report = ApplicationAnalyst(session).analyse(
            make_application(resume_text=UNRELATED_TAILORED)
        )

        assert "answers_the_posting" in blockers(report)
        assert report.fit_score < 25

    def test_the_fit_blocker_is_the_only_overridable_one(
        self, session, make_application
    ):
        # Everything else the analyst blocks on is a defect: sending anyway
        # would send a document the user would not have chosen.
        report = ApplicationAnalyst(session).analyse(
            make_application(resume_text=HEADERLESS_TAILORED)
        )

        assert all(not f.overridable for f in report.blockers)


# ============================================================================
# The gate
# ============================================================================

class TestGateIntegration:
    """The analyst's blockers are the submission gate's blockers."""

    def test_the_gate_refuses_an_application_the_analyst_blocks(
        self, session, make_application
    ):
        application = make_application(resume_text=HEADERLESS_TAILORED)

        decision = SubmissionGate(session).check_user_directed(application)

        assert not decision.allowed
        assert any("email address" in blocker for blocker in decision.blockers)

    def test_the_gate_allows_a_sound_application(self, session, make_application):
        decision = SubmissionGate(session).check_user_directed(make_application())

        assert decision.allowed, decision.blockers

    def test_the_report_travels_with_the_decision(self, session, make_application):
        decision = SubmissionGate(session).check_user_directed(make_application())

        assert decision.analysis is not None
        assert decision.analysis["ready"] is True

    def test_accepting_a_weak_fit_waives_only_that_finding(
        self, session, make_application
    ):
        # An unrelated career *and* a missing email: the judgement is waived,
        # the defect is not.
        application = make_application(
            resume_text=UNRELATED_TAILORED.replace(
                "Petaling Jaya, Malaysia | hasnat@example.com | +60 10 421 5890",
                "linkedin.com/in/hasnat-ali-tahir",
            )
        )
        decision = SubmissionGate(session).check_user_directed(
            application, accept_weak_fit=True
        )

        assert not decision.allowed  # The missing email still blocks
        assert any("despite the analyst" in w for w in decision.warnings)

    def test_a_weak_fit_alone_can_be_overridden(self, session, make_application):
        decision = SubmissionGate(session).check_user_directed(
            make_application(resume_text=UNRELATED_TAILORED), accept_weak_fit=True
        )

        assert decision.allowed, decision.blockers

    def test_an_unattended_run_cannot_waive_the_judgement(
        self, session, make_application, account
    ):
        decision = SubmissionGate(session).check_auto(
            make_application(resume_text=UNRELATED_TAILORED), account
        )

        assert not decision.allowed

    def test_turning_the_analyst_off_is_visible(
        self, session, make_application, monkeypatch
    ):
        monkeypatch.setattr(settings, "analyst_enabled", False)

        decision = SubmissionGate(session).check_user_directed(
            make_application(resume_text=HEADERLESS_TAILORED)
        )

        assert decision.allowed
        assert any("turned off" in w for w in decision.warnings)


# ============================================================================
# Robustness
# ============================================================================

class TestRobustness:
    """A check that cannot run must not take the submission down with it."""

    def test_an_application_with_no_documents_is_not_blocked(
        self, session, job, account
    ):
        application = Application(
            job_id=job.id,
            platform_account_id=account.id,
            filled_fields={},
            deferred_fields={},
            submission_status=ApplicationStatus.QUEUED_FOR_REVIEW,
            reviewed_by_user=True,
        )
        session.add(application)
        session.commit()
        session.refresh(application)

        report = ApplicationAnalyst(session).analyse(application)

        assert report.ready
        assert "resume_attached" in warnings(report)

    def test_a_broken_check_becomes_a_warning(
        self, session, make_application, monkeypatch
    ):
        def explode(*args, **kwargs):
            raise RuntimeError("the ATS checker fell over")

        monkeypatch.setattr(
            "job_agent.services.ats_check.check_ats", explode
        )

        report = ApplicationAnalyst(session).analyse(make_application())

        assert report.ready
        assert "machine_readable" in warnings(report)
