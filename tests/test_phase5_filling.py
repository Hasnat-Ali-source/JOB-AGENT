#!/usr/bin/env python3
"""
Tests for Phase 5: Application Filling & Review Gate.

Covers the Phase 5 acceptance criteria:
- A simple form is filled with known fields (name, email, phone, resume)
- An unknown field pauses the application into the Review Queue
- The screenshot shows the filled form
- Approval logs the form data, awaiting manual submission

And the safety property the phase turns on: sensitive questions —
demographics, disability, veteran status, compensation — are never answered by
the agent, no matter what the profile holds.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from job_agent.models.database import (
    Application,
    ApplicationStatus,
    AuditAction,
    AuditLog,
    CandidateProfile,
    FieldCategory,
    Job,
    PlatformAccount,
)
from job_agent.services.application_filler import ApplicationFiller
from job_agent.services.field_classifier import (
    FieldClassifier,
    FormField,
)
from job_agent.services.form_reader import FormReader

# Reuse the same form the test server serves, so tests and manual runs agree
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.test_server import APPLICATION_FORM_HTML  # noqa: E402


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def session():
    """In-memory database with every table created."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)

    with Session(engine) as db_session:
        yield db_session


@pytest.fixture
def profile(session) -> CandidateProfile:
    """A fully populated candidate profile."""
    record = CandidateProfile(
        full_name="Alex Rivera",
        email="alex.rivera@example.com",
        phone="+1 555 0100",
        location="San Francisco, CA",
        linkedin_url="https://linkedin.com/in/alexrivera",
        github_url="https://github.com/alexrivera",
        work_authorization="Yes",
        notice_period="4 weeks",
        years_experience=8,
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


@pytest.fixture
def job(session) -> Job:
    """A stored job."""
    record = Job(
        platform="generic_ats", external_id="REQ-1",
        title="Senior Backend Engineer", company="Acme Robotics", location="Remote",
        description="PostgreSQL and Kafka work.", apply_method="web_form",
        dedup_hash="hash-1",
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


@pytest.fixture
def account(session) -> PlatformAccount:
    """A connected platform account."""
    record = PlatformAccount(platform="generic_ats", profile_dir="/tmp/job-agent-test")
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


@pytest.fixture
def form_site():
    """Serve the application form over HTTP; yields the base URL."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = APPLICATION_FORM_HTML.encode("utf-8")
            self.send_response(200)
            # Without the charset the browser falls back to a legacy encoding
            # and label text arrives mojibaked — which would corrupt the very
            # strings the classifier matches sensitive questions against.
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
async def form_page(form_site):
    """A real browser page showing the application form."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch()
        except Exception as e:
            pytest.skip(f"No browser available (run `playwright install chromium`): {e}")

        page = await browser.new_page()
        await page.goto(f"{form_site}/apply")
        yield page
        await browser.close()


@pytest.fixture
def resume_pdf(tmp_path) -> Path:
    """A rendered resume to attach."""
    from job_agent.services.pdf_renderer import PdfRenderer

    path = tmp_path / "resume.pdf"
    PdfRenderer().render("Alex Rivera\n\nExperience\n- Built things", path)
    return path


# ============================================================================
# Classification — the safety-critical decisions
# ============================================================================

def _field(label: str, name: str = "", field_type: str = "text", **kwargs) -> FormField:
    """Build a FormField for classification tests."""
    return FormField(
        selector=f"#{name or 'f'}", tag=kwargs.pop("tag", "input"),
        field_type=field_type, name=name, label=label, **kwargs,
    )


class TestSensitiveFieldDetection:
    """
    Demographics, disability, veteran status and compensation are always the
    user's to answer. These are the cases that must never regress.
    """

    @pytest.mark.parametrize("label", [
        "Gender",
        "Race / ethnicity",
        "Are you Hispanic or Latino?",
        "Voluntary self-identification of gender",
        "Do you have a disability?",
        "Disability status",
        "Do you require any accommodations?",
        "Protected veteran status",
        "Have you served in the military?",
        "Current salary",
        "Salary history",
        "What is your expected salary?",
        "Desired compensation",
        "Have you ever been convicted of a felony?",
        "Date of birth",
        "Citizenship status",
    ])
    def test_sensitive_labels_are_detected(self, label, profile):
        classification = FieldClassifier(profile).classify(_field(label))

        assert classification.category == FieldCategory.SENSITIVE, label
        assert not classification.is_fillable
        assert "never answers this" in classification.reason

    def test_sensitive_wins_even_when_the_profile_could_answer(self, profile):
        """
        The profile has a work_authorization value, but a citizenship question
        is still the user's to answer — availability of a value must not
        override the sensitivity rule.
        """
        classification = FieldClassifier(profile).classify(_field("Citizenship status"))

        assert classification.category == FieldCategory.SENSITIVE
        assert classification.value is None

    def test_sensitive_is_deferred_when_nothing_was_saved(self, profile):
        """With no saved answer, a demographic question always goes to the user."""
        classification = FieldClassifier(profile).classify(_field("Gender"))

        assert classification.category == FieldCategory.SENSITIVE
        assert classification.value is None

    def test_the_users_own_saved_answer_is_reused(self, profile):
        """
        Reusing an answer the user deliberately saved is not the agent
        deciding — it is the agent not asking the same question on every
        application. A saved sensitive answer only exists when the user
        explicitly asked for it to be kept (see the review API), so the rule
        that the agent never invents one still holds.
        """
        remembered = {FieldClassifier.remember_key("Gender"): "Male"}

        classification = FieldClassifier(profile, remembered).classify(_field("Gender"))

        assert classification.category == FieldCategory.REMEMBERED
        assert classification.value == "Male"
        assert "saved" in classification.reason

    def test_ordinary_questions_are_not_flagged_sensitive(self, profile):
        for label in ["Full name", "Email address", "Phone number", "LinkedIn profile",
                      "Why do you want to work here?", "Notice period"]:
            classification = FieldClassifier(profile).classify(_field(label))
            assert classification.category != FieldCategory.SENSITIVE, label

    def test_is_sensitive_helper(self):
        assert FieldClassifier.is_sensitive("Protected veteran status")
        assert FieldClassifier.is_sensitive("What is your current salary?")
        assert not FieldClassifier.is_sensitive("What is your GitHub profile?")


class TestKnownFieldMapping:
    """Fields the agent may fill from the profile."""

    @pytest.mark.parametrize("label,expected", [
        ("Full name", "Alex Rivera"),
        ("Email address", "alex.rivera@example.com"),
        ("Phone number", "+1 555 0100"),
        ("Location (city)", "San Francisco, CA"),
        ("LinkedIn profile", "https://linkedin.com/in/alexrivera"),
        ("GitHub profile", "https://github.com/alexrivera"),
        ("Notice period", "4 weeks"),
    ])
    def test_profile_fields_are_mapped(self, label, expected, profile):
        classification = FieldClassifier(profile).classify(_field(label))

        assert classification.category == FieldCategory.KNOWN
        assert classification.value == expected
        assert classification.is_fillable

    def test_split_name_fields(self, profile):
        classifier = FieldClassifier(profile)

        assert classifier.classify(_field("First name")).value == "Alex"
        assert classifier.classify(_field("Last name")).value == "Rivera"

    def test_documents_are_recognized(self, profile):
        classifier = FieldClassifier(profile)

        assert classifier.classify(_field("Resume / CV")).profile_key == "resume"
        assert classifier.classify(_field("Upload your CV")).profile_key == "resume"
        assert classifier.classify(_field("Cover letter")).profile_key == "cover_letter"

    def test_a_machine_named_upload_is_still_recognized(self, profile):
        # Greenhouse labels both uploads "Attach" and tells them apart by id.
        # Matching `cover letter` against `cover_letter` failed, so the letter
        # was never attached and the field went out blank.
        classifier = FieldClassifier(profile)

        assert classifier.classify(
            _field("Attach", name="cover_letter", field_type="file")
        ).profile_key == "cover_letter"

    def test_a_camel_cased_attribute_is_still_recognized(self, profile):
        classifier = FieldClassifier(profile)

        assert classifier.classify(
            _field("", name="workAuthorization")
        ).profile_key == "work_authorization"

    def test_booleans_render_as_yes_no(self, session):
        record = CandidateProfile(
            full_name="A B", email="a@b.com", willing_to_relocate=True,
            requires_sponsorship=False,
        )
        classifier = FieldClassifier(record)

        assert classifier.classify(_field("Are you willing to relocate?")).value == "Yes"
        assert classifier.classify(_field("Do you require sponsorship?")).value == "No"

    def test_missing_profile_value_becomes_a_question(self, session):
        """An empty profile field is asked about, not filled blank."""
        record = CandidateProfile(full_name="A B", email="a@b.com")  # no phone

        classification = FieldClassifier(record).classify(_field("Phone number"))

        assert classification.category == FieldCategory.UNKNOWN
        assert not classification.is_fillable
        assert "no 'phone'" in classification.reason

    def test_unmappable_field_is_unknown(self, profile):
        classification = FieldClassifier(profile).classify(
            _field("Describe a time you disagreed with a colleague"))

        assert classification.category == FieldCategory.UNKNOWN
        assert not classification.is_fillable


class TestRememberedAnswers:
    """Questions the user has answered before are reused."""

    def test_remembered_answer_is_used(self, profile):
        question = "Why do you want to work here?"
        remembered = {FieldClassifier.remember_key(question): "I admire the product."}

        classification = FieldClassifier(profile, remembered).classify(_field(question))

        assert classification.category == FieldCategory.REMEMBERED
        assert classification.value == "I admire the product."
        assert classification.is_fillable

    def test_remember_key_normalizes_variants(self):
        assert (
            FieldClassifier.remember_key("Why do you want to work here?")
            == FieldClassifier.remember_key("why do you want to work here")
        )


# ============================================================================
# Reading and filling a real form in a real browser
# ============================================================================

@pytest.mark.asyncio
class TestFormReading:
    """The reader must see what a human sees."""

    async def test_reads_every_fillable_field(self, form_page):
        fields = await FormReader.read_fields(form_page)

        labels = [f.question for f in fields]

        assert "Full name" in labels
        assert "Email address" in labels
        assert "Resume / CV" in labels
        assert "Gender" in labels
        # The submit button is not a fillable field
        assert not any("Submit Application" == label for label in labels)

    async def test_captures_labels_not_just_names(self, form_page):
        """Sensitivity is decided from the label a human reads."""
        fields = {f.name: f for f in await FormReader.read_fields(form_page)}

        assert fields["veteran_status"].label == "Protected veteran status"
        assert fields["race_ethnicity"].label == "Race / ethnicity"

    async def test_captures_required_flags_and_options(self, form_page):
        fields = {f.name: f for f in await FormReader.read_fields(form_page)}

        assert fields["full_name"].required is True
        assert fields["phone"].required is False
        assert "LinkedIn" in fields["heard_about"].options

    async def test_a_custom_dropdown_still_offers_its_choices(self, form_page):
        """
        Forms that replace <select> with a text input keep the real choices in
        a hidden select. Without them the user is asked to type an answer the
        form will only accept from its own list.
        """
        await form_page.set_content("""
            <form>
              <div>
                <label for="country_input">Country</label>
                <input id="country_input" name="country" type="text"
                       role="combobox" autocomplete="off">
                <select name="country" style="display:none">
                  <option>Malaysia</option>
                  <option>Singapore</option>
                  <option>United States</option>
                </select>
              </div>
            </form>
        """)

        fields = {f.question: f for f in await FormReader.read_fields(form_page)}

        assert "Malaysia" in fields["Country"].options
        assert "Singapore" in fields["Country"].options

    async def test_a_long_list_is_kept_whole(self, form_page):
        """
        "Which country do you live in?" has around 200 answers. Capping the
        list truncated it, and rejecting it for being long dropped it
        altogether — either way the user is typing a country name into a
        control that only accepts one of its own.
        """
        countries = [f"Country {n:03d}" for n in range(197)]
        options = "".join(f"<option>{c}</option>" for c in countries)

        await form_page.set_content(f"""
            <form>
              <div>
                <label for="residence">Country of residence</label>
                <input id="residence" name="residence" type="text" role="combobox">
                <select name="residence" style="display:none">{options}</select>
              </div>
            </form>
        """)

        fields = {f.question: f for f in await FormReader.read_fields(form_page)}
        found = fields["Country of residence"].options

        assert len(found) == 197
        assert found[-1] == "Country 196"

    async def test_an_unlabelled_field_is_named_not_selectored(self, form_page):
        """
        "div > input:nth-of-type(1)" is not a question anyone can answer. The
        form's own attribute name at least says what it is asking for.
        """
        await form_page.set_content("""
            <form><div><input name="cover_letter_text" type="text" required></div></form>
        """)

        fields = await FormReader.read_fields(form_page)

        assert fields[0].question == "Cover letter text"

    async def test_a_generated_id_is_no_better_than_the_selector(self, form_page):
        """A framework handle like "input-42" is noise dressed up as a label."""
        await form_page.set_content("""
            <form><div><input id="input-42" type="text"></div></form>
        """)

        fields = await FormReader.read_fields(form_page)

        assert fields[0].question == fields[0].selector

    async def test_required_asterisk_is_stripped_from_labels(self, form_page):
        fields = {f.name: f for f in await FormReader.read_fields(form_page)}

        assert fields["full_name"].label == "Full name"  # not "Full name *"

    async def test_labels_decode_as_utf8(self, form_page):
        """
        Mojibaked labels would break sensitive-question matching, so the page
        must decode cleanly end to end.
        """
        title = await form_page.title()

        assert "—" in title
        assert "â€" not in title


class TestLabelNormalization:
    """
    Label text is the input to sensitive-question matching, so a corrupted or
    typographically unusual label must not silently stop matching.
    """

    def test_mojibake_is_repaired(self):
        """UTF-8 bytes decoded as cp1252 — what a charset-less site serves."""
        broken = "Protected veteranâ€™s status"

        assert FormReader._tidy(broken) == "Protected veteran's status"

    def test_repaired_label_still_classifies_as_sensitive(self, profile):
        broken = "Are you a protected veteranâ€™s spouse?"

        label = FormReader._tidy(broken)

        assert FieldClassifier.is_sensitive(label)
        assert "â€" not in label

    def test_correct_text_is_left_alone(self):
        """Repair must not corrupt text that was already fine."""
        assert FormReader._tidy("Café — Señor Ãngel") == "Café — Señor Ãngel"
        assert FormReader._tidy("Race / ethnicity") == "Race / ethnicity"

    def test_non_breaking_spaces_are_normalized(self):
        """A label using &nbsp; must still match plain-ASCII patterns."""
        label = FormReader._tidy("Current\xa0salary")

        assert label == "Current salary"
        assert FieldClassifier.is_sensitive(label)

    def test_curly_punctuation_is_folded(self):
        assert "…" not in FormReader._tidy("Tell us more…")
        assert FormReader._tidy("What’s your current salary?") == "What's your current salary?"

    def test_curly_apostrophe_does_not_break_matching(self):
        """A typeset apostrophe must not hide a compensation question."""
        label = FormReader._tidy("What’s your current salary?")

        assert FieldClassifier.is_sensitive(label)

    def test_required_markers_and_whitespace_are_stripped(self):
        assert FormReader._tidy("  Full name *  ") == "Full name"
        assert FormReader._tidy("Email address:\n") == "Email address"


@pytest.mark.asyncio
class TestFillingARealForm:
    """Acceptance: fill known fields, defer the rest, screenshot the result."""

    async def test_known_fields_are_filled(self, form_page, profile, session, tmp_path):
        filler = ApplicationFiller(session, screenshots_dir=tmp_path / "shots")

        await filler.fill_form(form_page, profile)

        assert await form_page.input_value("#fullName") == "Alex Rivera"
        assert await form_page.input_value("#emailAddr") == "alex.rivera@example.com"
        assert await form_page.input_value("#phoneNum") == "+1 555 0100"
        assert await form_page.input_value("#linkedinField") == "https://linkedin.com/in/alexrivera"

    async def test_sensitive_fields_are_left_blank_on_the_page(
        self, form_page, profile, session, tmp_path
    ):
        """The decisive assertion: nothing was entered into these."""
        filler = ApplicationFiller(session, screenshots_dir=tmp_path / "shots")

        await filler.fill_form(form_page, profile)

        for selector in ("#genderField", "#raceField", "#veteranField",
                         "#disabilityField", "#currentSalary", "#expectedSalary"):
            assert await form_page.input_value(selector) == "", selector

    async def test_sensitive_fields_are_deferred_with_reasons(
        self, form_page, profile, session, tmp_path
    ):
        filler = ApplicationFiller(session, screenshots_dir=tmp_path / "shots")

        outcome = await filler.fill_form(form_page, profile)

        sensitive = {
            q: d for q, d in outcome.deferred_fields.items()
            if d["category"] == FieldCategory.SENSITIVE.value
        }

        assert "Gender" in sensitive
        assert "Race / ethnicity" in sensitive
        assert "Protected veteran status" in sensitive
        assert "Expected salary" in sensitive
        assert all("never answers this" in d["reason"] for d in sensitive.values())

    async def test_unknown_question_is_deferred(self, form_page, profile, session, tmp_path):
        """Acceptance: an unknown field pauses for the user."""
        filler = ApplicationFiller(session, screenshots_dir=tmp_path / "shots")

        outcome = await filler.fill_form(form_page, profile)

        assert "Why do you want to work here?" in outcome.deferred_fields
        assert outcome.needs_user_input
        assert "Why do you want to work here?" in outcome.required_deferred

    async def test_resume_is_attached(self, form_page, profile, session, tmp_path, resume_pdf):
        filler = ApplicationFiller(session, screenshots_dir=tmp_path / "shots")

        outcome = await filler.fill_form(
            form_page, profile, documents={"resume": str(resume_pdf)})

        assert "Resume / CV" in outcome.filled_fields

        attached = await form_page.evaluate(
            "() => document.querySelector('#resumeUpload').files[0]?.name")
        assert attached == "resume.pdf"

    async def test_missing_document_is_deferred_not_skipped(
        self, form_page, profile, session, tmp_path
    ):
        filler = ApplicationFiller(session, screenshots_dir=tmp_path / "shots")

        outcome = await filler.fill_form(form_page, profile)

        assert "Resume / CV" in outcome.deferred_fields

    async def test_screenshot_is_captured(self, form_page, profile, session, tmp_path):
        """Acceptance: the screenshot shows the filled form."""
        filler = ApplicationFiller(session, screenshots_dir=tmp_path / "shots")

        outcome = await filler.fill_form(form_page, profile)

        assert outcome.screenshot_path
        shot = Path(outcome.screenshot_path)
        assert shot.exists()
        assert shot.read_bytes().startswith(b"\x89PNG")

    async def test_dropdown_is_filled_when_an_option_matches(
        self, form_page, profile, session, tmp_path
    ):
        """work_authorization="Yes" matches the Yes option."""
        filler = ApplicationFiller(session, screenshots_dir=tmp_path / "shots")

        await filler.fill_form(form_page, profile)

        assert await form_page.input_value("#authField") == "Yes"

    async def test_a_value_outside_a_dropdowns_choices_is_left_to_the_user(
        self, form_page, profile, session, tmp_path
    ):
        """
        Custom dropdowns render as text inputs, so a value that merely looked
        right for the question can be typed into one. GitLab's form asked
        "…require sponsorship … in your current location?" and the classifier
        matched "location", filling in "Malaysia" — not one of its seven
        choices. Typed in, that is either rejected at submit or recorded as an
        answer the user never gave.
        """
        await form_page.set_content("""
            <form>
              <label for="visa">Will you require sponsorship in your location?</label>
              <input id="visa" name="visa" type="text" role="combobox">
              <select name="visa" style="display:none">
                <option>Yes</option>
                <option>No</option>
              </select>
            </form>
        """)

        profile.location = "Malaysia"
        session.commit()

        filler = ApplicationFiller(session, screenshots_dir=tmp_path / "shots")
        outcome = await filler.fill_form(form_page, profile)

        question = "Will you require sponsorship in your location?"

        assert question not in outcome.filled_fields
        assert question in outcome.deferred_fields
        assert await form_page.input_value("#visa") == ""

    async def test_remembered_answer_fills_a_previously_unknown_question(
        self, form_page, profile, session, tmp_path
    ):
        question = "Why do you want to work here?"
        profile.remembered_answers = {
            FieldClassifier.remember_key(question): "I admire the product work."
        }
        session.commit()

        filler = ApplicationFiller(session, screenshots_dir=tmp_path / "shots")
        outcome = await filler.fill_form(form_page, profile)

        assert question in outcome.filled_fields
        assert await form_page.input_value("#whyUs") == "I admire the product work."


@pytest.mark.asyncio
class TestQueueingForReview:
    """Filled forms become review-queue entries, never submissions."""

    async def test_application_is_queued_not_submitted(
        self, form_page, profile, session, job, account, tmp_path
    ):
        filler = ApplicationFiller(session, screenshots_dir=tmp_path / "shots")
        outcome = await filler.fill_form(form_page, profile)

        application = filler.queue_for_review(
            job, account, profile, outcome, form_url=form_page.url)

        assert application.submission_status == ApplicationStatus.QUEUED_FOR_REVIEW
        assert application.submitted_at is None
        assert application.reviewed_by_user is False
        assert application.filled_at is not None

    async def test_queued_application_records_both_sides(
        self, form_page, profile, session, job, account, tmp_path
    ):
        filler = ApplicationFiller(session, screenshots_dir=tmp_path / "shots")
        outcome = await filler.fill_form(form_page, profile)

        application = filler.queue_for_review(
            job, account, profile, outcome, form_url=form_page.url)

        assert "Full name" in application.filled_fields
        assert "Gender" in application.deferred_fields
        assert application.screenshot_path
        assert application.form_url

    async def test_deferrals_are_audited(
        self, form_page, profile, session, job, account, tmp_path
    ):
        filler = ApplicationFiller(session, screenshots_dir=tmp_path / "shots")
        outcome = await filler.fill_form(form_page, profile)
        filler.queue_for_review(job, account, profile, outcome, form_url=form_page.url)

        deferrals = session.query(AuditLog).filter(
            AuditLog.action == AuditAction.FIELD_DEFERRED).all()

        assert len(deferrals) == len(outcome.deferred_fields)
        assert all(entry.result == "paused" for entry in deferrals)

    async def test_queue_event_is_audited_as_paused(
        self, form_page, profile, session, job, account, tmp_path
    ):
        filler = ApplicationFiller(session, screenshots_dir=tmp_path / "shots")
        outcome = await filler.fill_form(form_page, profile)
        filler.queue_for_review(job, account, profile, outcome, form_url=form_page.url)

        queued = session.query(AuditLog).filter(
            AuditLog.action == AuditAction.APPLICATION_QUEUED).one()

        assert queued.result == "paused"
        assert "awaiting your review" in queued.detail


@pytest.mark.asyncio
class TestConnectorIntegration:
    """The connector path used by the rest of the system."""

    async def test_fill_application_populates_the_session(
        self, form_page, profile, session, tmp_path
    ):
        from job_agent.connectors.base import ApplicationSession, JobPosting
        from job_agent.connectors.generic_ats import GenericATSConnector

        connector = GenericATSConnector()
        connector.set_page(form_page)

        posting = JobPosting(
            platform="generic_ats", external_id="1", title="Senior Backend Engineer",
            company="Acme Robotics", location="Remote",
        )
        app_session = ApplicationSession(job=posting, platform_account_id=1)

        result = await connector.fill_application(
            app_session, profile,
            {"db_session": session, "screenshots_dir": tmp_path / "shots"},
        )

        assert "Full name" in result.filled_fields
        assert "Gender" in result.deferred_fields
        assert result.screenshot_path
        assert result.form_state["needs_user_input"] is True

    async def test_filling_never_submits(self, form_page, profile, session, tmp_path):
        """
        Filling and submitting are separate acts.

        Phase 6 gave the connector a working submit_application(), so this no
        longer asserts that submission is impossible — it asserts that filling
        alone never triggers it. The form must be left sitting there, filled
        and unsent, until something explicitly submits it.
        """
        from job_agent.connectors.base import ApplicationSession, JobPosting
        from job_agent.connectors.generic_ats import GenericATSConnector

        connector = GenericATSConnector()
        connector.set_page(form_page)

        posting = JobPosting(
            platform="generic_ats", external_id="1", title="T", company="C", location="L")

        await connector.fill_application(
            ApplicationSession(job=posting, platform_account_id=1),
            profile,
            {"db_session": session, "screenshots_dir": tmp_path / "shots"},
        )

        # The page's submit handler appends #submitted; it must not be there
        assert await form_page.locator("#submitted").count() == 0
        assert await form_page.input_value("#fullName") == "Alex Rivera"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
