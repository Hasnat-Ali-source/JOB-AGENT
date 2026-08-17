#!/usr/bin/env python3
"""
Tests for Phase 7: Additional Connectors.

Covers the per-platform acceptance criteria, and the property that matters
across all of them: **capability declarations must be honest**. The GUI hides
actions a connector doesn't claim and the Phase 6 gate consults
`can_submit_automatically`, so an over-claiming connector would route around
the safety machinery rather than fail loudly.

Connectors are exercised against local fixtures replicating each platform's
published markup. None is tested against a live tenant — that would mean
submitting real applications to real employers — which is exactly what
`VerificationLevel.FIXTURE` records.
"""

import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import pytest_asyncio

from job_agent.connectors import create_connector, list_connectors
from job_agent.connectors.ats_connectors import (
    ATS_CONNECTORS,
    AshbyConnector,
    GreenhouseConnector,
    LeverConnector,
    SmartRecruitersConnector,
    WorkableConnector,
    WorkdayConnector,
)
from job_agent.connectors.hosted_ats import HostedATSConnector, VerificationLevel
from job_agent.connectors.job_boards import (
    BOARD_CONNECTORS,
    IndeedConnector,
    LinkedInConnector,
)
from job_agent.models.database import SearchProfile

ALL_CONNECTORS = [*ATS_CONNECTORS, *BOARD_CONNECTORS]


# ============================================================================
# Fixtures replicating each platform's markup
# ============================================================================

GREENHOUSE_BOARD = """
<!DOCTYPE html><html><head><meta charset="utf-8"><title>Acme Jobs</title></head><body>
<div class="opening"><a href="https://job-boards.greenhouse.io/acme/jobs/4001">Senior Backend Engineer</a></div>
<div class="opening"><a href="https://job-boards.greenhouse.io/acme/jobs/4002">Platform Engineer</a></div>
<a href="https://job-boards.greenhouse.io/acme">All openings</a>
<a href="https://www.acme.test/about">About us</a>
</body></html>
"""

GREENHOUSE_JOB = """
<!DOCTYPE html><html><head><meta charset="utf-8">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"JobPosting","identifier":"4001",
"title":"Senior Backend Engineer","hiringOrganization":{"@type":"Organization","name":"Acme"},
"jobLocation":{"@type":"Place","address":{"@type":"PostalAddress","addressLocality":"Remote"}},
"description":"Python and PostgreSQL.","employmentType":"FULL_TIME","datePosted":"2026-08-12",
"baseSalary":{"@type":"MonetaryAmount","currency":"USD",
"value":{"@type":"QuantitativeValue","minValue":170000,"maxValue":200000}}}
</script></head><body>
<h1>Senior Backend Engineer</h1>
<form id="application_form">
  <label for="first_name">First Name *</label><input id="first_name" name="job_application[first_name]" required>
  <label for="last_name">Last Name *</label><input id="last_name" name="job_application[last_name]" required>
  <label for="email">Email *</label><input id="email" type="email" name="job_application[email]" required>
  <label for="phone">Phone</label><input id="phone" type="tel" name="job_application[phone]">
  <label for="resume">Resume *</label><input id="resume" type="file" name="job_application[resume]" required>
  <label for="question_9001">Why do you want to work at Acme? *</label>
  <textarea id="question_9001" name="job_application[answers][9001]" required></textarea>
  <label for="gender">Gender</label>
  <select id="gender" name="job_application[demographic][gender]">
    <option value="">Decline to self identify</option><option>Female</option><option>Male</option>
  </select>
  <button type="submit">Submit Application</button>
</form>
</body></html>
"""

LEVER_BOARD = """
<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<a class="posting-title" href="https://jobs.lever.co/acme/a1b2c3d4-e5f6-7890-abcd-ef1234567890">Backend Engineer</a>
<a class="posting-title" href="https://jobs.lever.co/acme/b2c3d4e5-f6a7-8901-bcde-f12345678901">Data Engineer</a>
<a href="https://jobs.lever.co/acme">Back to jobs</a>
</body></html>
"""

WORKABLE_BOARD = """
<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<a href="https://apply.workable.com/acme/j/A1B2C3D4E5">Senior Backend Engineer</a>
<a href="https://apply.workable.com/acme/j/F6G7H8I9J0">Frontend Engineer</a>
</body></html>
"""

LINKEDIN_RESULTS = """
<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<a href="https://www.linkedin.com/jobs/view/3901234567">Senior Backend Engineer</a>
<a href="https://www.linkedin.com/jobs/view/3901234568">Staff Engineer</a>
<a href="https://www.linkedin.com/feed/">Home</a>
</body></html>
"""


@pytest.fixture
def fixture_server():
    """Serve the platform fixtures over HTTP."""
    pages = {
        "/greenhouse/board": GREENHOUSE_BOARD,
        "/greenhouse/job": GREENHOUSE_JOB,
        "/lever/board": LEVER_BOARD,
        "/workable/board": WORKABLE_BOARD,
        "/linkedin/results": LINKEDIN_RESULTS,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = self.path.split("?")[0]
            body = pages.get(path, "<html><body>not found</body></html>").encode("utf-8")
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


@pytest.fixture
def profile() -> SearchProfile:
    """A search profile."""
    return SearchProfile(
        name="Backend", target_titles=["Backend Engineer"], remote_pref="remote")


# ============================================================================
# Honest capability declarations
# ============================================================================

class TestCapabilityHonesty:
    """
    The property that protects the user across every connector.

    A connector claiming more than it implements would route around the review
    gate rather than fail loudly, so these are checked for all of them at once.
    """

    @pytest.mark.parametrize("connector_class", ALL_CONNECTORS,
                             ids=lambda c: c.PLATFORM)
    def test_every_connector_registers(self, connector_class):
        assert connector_class.PLATFORM in list_connectors()
        assert create_connector(connector_class.PLATFORM) is not None

    @pytest.mark.parametrize("connector_class", ALL_CONNECTORS,
                             ids=lambda c: c.PLATFORM)
    def test_submission_requires_filling(self, connector_class):
        """Claiming to submit without claiming to fill is incoherent."""
        capabilities = connector_class.CAPABILITIES

        if capabilities.can_submit_automatically:
            assert capabilities.can_fill_standard_fields, (
                f"{connector_class.PLATFORM} claims it can submit but not fill"
            )

    @pytest.mark.parametrize("connector_class", ALL_CONNECTORS,
                             ids=lambda c: c.PLATFORM)
    def test_filling_requires_reading(self, connector_class):
        capabilities = connector_class.CAPABILITIES

        if capabilities.can_fill_standard_fields:
            assert capabilities.can_read_details

    @pytest.mark.parametrize("connector_class", ALL_CONNECTORS,
                             ids=lambda c: c.PLATFORM)
    def test_verification_level_is_declared(self, connector_class):
        assert isinstance(connector_class.VERIFIED_AGAINST, VerificationLevel)

    @pytest.mark.parametrize("connector_class", ALL_CONNECTORS,
                             ids=lambda c: c.PLATFORM)
    def test_nothing_claims_live_verification_yet(self, connector_class):
        """
        Nothing here has been run against a live tenant. If that changes, the
        connector's level moves up and this test is what forces the claim to be
        deliberate rather than accidental.
        """
        assert connector_class.VERIFIED_AGAINST == VerificationLevel.FIXTURE

    @pytest.mark.parametrize("connector_class", BOARD_CONNECTORS,
                             ids=lambda c: c.PLATFORM)
    def test_consumer_boards_never_submit(self, connector_class):
        """
        Third-party boards act against the user's own account. Read-only is the
        default and there is no setting that changes it.
        """
        capabilities = connector_class.CAPABILITIES

        assert capabilities.can_submit_automatically is False
        assert capabilities.can_fill_standard_fields is False

    @pytest.mark.parametrize("connector_class", BOARD_CONNECTORS,
                             ids=lambda c: c.PLATFORM)
    def test_consumer_boards_carry_a_risk_note(self, connector_class):
        note = connector_class.CAPABILITIES.tos_risk_note

        assert note, f"{connector_class.PLATFORM} has no ToS risk note"
        assert "never submits" in note.lower() or "submit" in note.lower()

    @pytest.mark.parametrize("connector_class", ALL_CONNECTORS,
                             ids=lambda c: c.PLATFORM)
    def test_review_threshold_is_positive(self, connector_class):
        """Every platform must earn unattended submission through review."""
        assert connector_class.CAPABILITIES.requires_manual_review_first_n >= 3

    def test_describe_reports_capabilities(self):
        described = GreenhouseConnector(board="acme").describe()

        assert described["platform"] == "greenhouse"
        assert described["board"] == "acme"
        assert described["verified_against"] == "fixture"
        assert described["capabilities"]["submit_automatically"] is True


# ============================================================================
# URL handling
# ============================================================================

class TestBoardUrls:
    """Each platform's URL shape."""

    @pytest.mark.parametrize("connector_class,board,expected", [
        (GreenhouseConnector, "acme", "https://job-boards.greenhouse.io/acme"),
        (LeverConnector, "acme", "https://jobs.lever.co/acme"),
        (AshbyConnector, "acme", "https://jobs.ashbyhq.com/acme"),
        (SmartRecruitersConnector, "acme", "https://jobs.smartrecruiters.com/acme"),
        (WorkableConnector, "acme", "https://apply.workable.com/acme"),
    ])
    def test_board_url_is_built(self, connector_class, board, expected):
        assert connector_class.board_url(board) == expected

    def test_board_token_round_trips(self):
        url = GreenhouseConnector.board_url("acme")

        assert GreenhouseConnector.extract_board(url) == "acme"

    def test_board_is_used_as_the_search_url(self):
        connector = LeverConnector(board="acme")

        assert connector.search_url == "https://jobs.lever.co/acme"

    def test_explicit_search_url_wins(self):
        connector = LeverConnector(
            board="acme", search_url="https://jobs.lever.co/other")

        assert connector.search_url == "https://jobs.lever.co/other"

    def test_workday_accepts_a_full_host(self):
        """Tenants embed a data-centre number that can't be guessed."""
        assert WorkdayConnector.board_url("acme.wd5.myworkdayjobs.com") == (
            "https://acme.wd5.myworkdayjobs.com"
        )

    def test_workday_accepts_a_bare_tenant(self):
        assert WorkdayConnector.board_url("acme") == "https://acme.myworkdayjobs.com"

    @pytest.mark.parametrize("connector_class,job_url,expected_suffix", [
        (LeverConnector, "https://jobs.lever.co/acme/abc-123", "/apply"),
        (AshbyConnector, "https://jobs.ashbyhq.com/acme/abc-123", "/application"),
        (WorkableConnector, "https://apply.workable.com/acme/j/ABC", "/apply"),
    ])
    def test_apply_url_is_derived(self, connector_class, job_url, expected_suffix):
        assert connector_class().apply_url_for(job_url).endswith(expected_suffix)

    def test_apply_url_is_not_doubled(self):
        connector = LeverConnector()
        once = connector.apply_url_for("https://jobs.lever.co/acme/abc")

        assert connector.apply_url_for(once) == once

    def test_greenhouse_has_no_apply_suffix(self):
        """Greenhouse serves the form on the posting page itself."""
        url = "https://job-boards.greenhouse.io/acme/jobs/4001"

        assert GreenhouseConnector().apply_url_for(url) == url

    @pytest.mark.parametrize("connector_class,query,expected", [
        (LinkedInConnector, "Backend Engineer", "keywords=Backend+Engineer"),
        (IndeedConnector, "Backend Engineer", "q=Backend+Engineer"),
    ])
    def test_consumer_board_search_urls(self, connector_class, query, expected):
        connector = connector_class()
        url = connector._build_search_url(query, "Remote")

        assert expected in url
        assert "Remote" in url or "location=" in url


class TestJobLinkPatterns:
    """Link patterns must accept postings and reject navigation."""

    @pytest.mark.parametrize("connector_class,url,matches", [
        (GreenhouseConnector, "https://job-boards.greenhouse.io/acme/jobs/4001", True),
        (GreenhouseConnector, "https://job-boards.greenhouse.io/acme", False),
        (LeverConnector,
         "https://jobs.lever.co/acme/a1b2c3d4-e5f6-7890-abcd-ef1234567890", True),
        (LeverConnector, "https://jobs.lever.co/acme", False),
        (WorkableConnector, "https://apply.workable.com/acme/j/A1B2C3D4E5", True),
        (WorkableConnector, "https://apply.workable.com/acme", False),
        (LinkedInConnector, "https://www.linkedin.com/jobs/view/3901234567", True),
        (LinkedInConnector, "https://www.linkedin.com/feed/", False),
        (IndeedConnector, "https://www.indeed.com/viewjob?jk=abc123", True),
        (IndeedConnector, "https://www.indeed.com/companies", False),
        (WorkdayConnector,
         "https://acme.wd5.myworkdayjobs.com/careers/job/Remote/Engineer_R-1", True),
    ])
    def test_pattern(self, connector_class, url, matches):
        pattern = connector_class.JOB_LINK_PATTERN

        assert bool(pattern.search(url)) is matches


# ============================================================================
# Behaviour against fixtures
# ============================================================================

@pytest.mark.asyncio
class TestAgainstFixtures:
    """Real browser, fixture markup replicating each platform."""

    async def test_greenhouse_collects_only_job_links(
        self, browser, fixture_server, profile
    ):
        """Acceptance (7a): can open search and collect postings."""
        page = await browser.new_page()
        await page.goto(f"{fixture_server}/greenhouse/board")

        connector = GreenhouseConnector(board="acme")
        connector.set_page(page)

        links = await connector.collect_job_links()

        assert len(links) == 2
        assert all("/jobs/" in link for link in links)
        assert not any(link.endswith("/about") for link in links)

    async def test_greenhouse_reads_job_details(self, browser, fixture_server):
        """Acceptance (7a): can read job details."""
        page = await browser.new_page()
        await page.goto(f"{fixture_server}/greenhouse/job")

        connector = GreenhouseConnector(board="acme")
        connector.set_page(page)

        posting = await connector.read_job_details(f"{fixture_server}/greenhouse/job")

        assert posting.title == "Senior Backend Engineer"
        assert posting.company == "Acme"
        assert posting.salary == "USD 170,000 - 200,000"
        assert posting.platform == "greenhouse"

    async def test_greenhouse_fills_standard_fields(
        self, browser, fixture_server, tmp_path
    ):
        """Acceptance (7a): can fill standard fields."""
        from sqlalchemy.pool import StaticPool
        from sqlmodel import Session, SQLModel, create_engine

        from job_agent.models.database import CandidateProfile
        from job_agent.services.application_filler import ApplicationFiller

        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(engine)
        session = Session(engine)

        candidate = CandidateProfile(
            full_name="Alex Rivera", email="alex@example.com", phone="+1 555 0100")
        session.add(candidate)
        session.commit()
        session.refresh(candidate)

        page = await browser.new_page()
        await page.goto(f"{fixture_server}/greenhouse/job")

        outcome = await ApplicationFiller(
            session, screenshots_dir=tmp_path).fill_form(page, candidate)

        assert await page.input_value("#first_name") == "Alex"
        assert await page.input_value("#last_name") == "Rivera"
        assert await page.input_value("#email") == "alex@example.com"
        assert await page.input_value("#phone") == "+1 555 0100"

        # The custom question and the demographic question are both deferred
        assert "Why do you want to work at Acme?" in outcome.deferred_fields
        assert "Gender" in outcome.deferred_fields
        assert (
            outcome.deferred_fields["Gender"]["category"] == "sensitive"
        )
        assert await page.input_value("#gender") == ""

    async def test_lever_collects_uuid_postings(self, browser, fixture_server):
        """Acceptance (7b): can open search and read postings."""
        page = await browser.new_page()
        await page.goto(f"{fixture_server}/lever/board")

        connector = LeverConnector(board="acme")
        connector.set_page(page)

        links = await connector.collect_job_links()

        assert len(links) == 2
        assert all(re.search(r"[0-9a-f]{8}-", link) for link in links)

    async def test_workable_collects_postings(self, browser, fixture_server):
        """Acceptance (7e): can open the application flow."""
        page = await browser.new_page()
        await page.goto(f"{fixture_server}/workable/board")

        connector = WorkableConnector(board="acme")
        connector.set_page(page)

        links = await connector.collect_job_links()

        assert len(links) == 2
        assert all("/j/" in link for link in links)

    async def test_linkedin_collects_job_views_only(self, browser, fixture_server):
        """Acceptance (7f): can search and read; navigation links ignored."""
        page = await browser.new_page()
        await page.goto(f"{fixture_server}/linkedin/results")

        connector = LinkedInConnector()
        connector.set_page(page)

        links = await connector.collect_job_links()

        assert len(links) == 2
        assert all("/jobs/view/" in link for link in links)

    async def test_read_only_board_refuses_to_submit(self, browser, fixture_server):
        """
        A read-only connector must not submit even when asked directly — the
        capability flag is what the gate consults, and the two must agree.
        """
        from job_agent.connectors.base import ApplicationSession, JobPosting
        from job_agent.services.submission_gate import SubmissionGate

        connector = LinkedInConnector()

        assert connector.capabilities.can_submit_automatically is False

        # And the gate refuses on that basis
        from sqlalchemy.pool import StaticPool
        from sqlmodel import Session, SQLModel, create_engine

        from job_agent.models.database import (
            Application, ApplicationStatus, AutomationMode, Job, PlatformAccount,
        )

        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(engine)
        session = Session(engine)

        job = Job(platform="linkedin", external_id="1", title="T", company="C",
                  location="L", description="d", apply_method="web_form", dedup_hash="h")
        account = PlatformAccount(
            platform="linkedin", profile_dir="/tmp/x",
            automation_mode=AutomationMode.SEARCH_FILL_SUBMIT,
            clean_submissions_count=99, daily_apply_limit=10,
        )
        session.add_all([job, account])
        session.commit()
        session.refresh(job)
        session.refresh(account)

        application = Application(
            job_id=job.id, platform_account_id=account.id,
            submission_status=ApplicationStatus.QUEUED_FOR_REVIEW,
            reviewed_by_user=True, deferred_fields={},
        )
        session.add(application)
        session.commit()
        session.refresh(application)

        decision = SubmissionGate(session).check_auto(application, account, connector)

        assert decision.allowed is False
        assert any("does not support automatic submission" in b for b in decision.blockers)

    async def test_greenhouse_opens_search_by_url(self, browser, fixture_server, profile):
        connector = GreenhouseConnector(search_url=f"{fixture_server}/greenhouse/board")
        page = await browser.new_page()
        connector.set_page(page)

        await connector.open_search(profile)

        assert connector.last_search_url
        assert "/greenhouse/board" in page.url


class TestInheritance:
    """Platform connectors reuse the generic machinery rather than forking it."""

    @pytest.mark.parametrize("connector_class", ALL_CONNECTORS,
                             ids=lambda c: c.PLATFORM)
    def test_all_extend_the_hosted_base(self, connector_class):
        assert issubclass(connector_class, HostedATSConnector)

    @pytest.mark.parametrize("connector_class", ALL_CONNECTORS,
                             ids=lambda c: c.PLATFORM)
    def test_platform_name_matches_registration(self, connector_class):
        assert connector_class().platform_name == connector_class.PLATFORM

    def test_generic_parsing_is_inherited(self):
        """JSON-LD handling is shared, not reimplemented per platform."""
        connector = GreenhouseConnector()

        assert connector._extract_salary(
            {"currency": "USD", "value": {"minValue": 1000}}) == "USD 1,000+"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
