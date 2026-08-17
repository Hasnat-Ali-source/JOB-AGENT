#!/usr/bin/env python3
"""
Tests for user-added stations and real sign-in detection.

Two things had to change together for a station the user adds by pasting a URL
to be usable:

- **Sign-in detection had to become real.** It used to assume every platform
  was connected, so a station could be recorded as on line when nobody had
  signed in — and every run then searched a logged-out page and reported no
  jobs found.
- **The generic connector had to read boards it has never seen.** It only
  found listings inside elements whose class contained "job", and it discarded
  any posting whose company name wasn't in a class of its own. Most boards
  satisfy neither.
"""

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from job_agent.connectors import create_connector_for_account
from job_agent.dashboard.deps import get_session
from job_agent.dashboard.main import app
from job_agent.connectors.generic_ats import GenericATSConnector
from job_agent.models.database import (
    AutomationMode,
    ConnectionStatus,
    PlatformAccount,
)


def make_account(**overrides) -> PlatformAccount:
    """A station record, custom unless told otherwise."""
    defaults = dict(
        platform="acme_careers",
        connector_kind="generic_ats",
        requires_signin=False,
        search_url="https://acme.test/careers",
        status=ConnectionStatus.CONNECTED,
        profile_dir="/tmp/unused",
        automation_mode=AutomationMode.SEARCH_AND_ANALYZE,
    )
    defaults.update(overrides)
    return PlatformAccount(**defaults)


# ============================================================================
# Resolving a station to a connector
# ============================================================================

class TestConnectorForAccount:
    """A station names the connector that drives it."""

    def test_custom_station_borrows_the_generic_connector(self):
        connector = create_connector_for_account(make_account())

        assert isinstance(connector, GenericATSConnector)

    def test_custom_station_keeps_its_own_name(self):
        """Jobs and audit entries are filed under the station, not 'generic_ats'."""
        connector = create_connector_for_account(make_account())

        assert connector.platform_name == "acme_careers"

    def test_the_station_supplies_its_search_url(self):
        connector = create_connector_for_account(
            make_account(search_url="https://acme.test/openings")
        )

        assert connector.search_url == "https://acme.test/openings"

    def test_a_public_station_needs_no_signin(self):
        connector = create_connector_for_account(make_account(requires_signin=False))

        assert connector.capabilities.requires_manual_signin is False

    def test_a_private_station_does(self):
        connector = create_connector_for_account(make_account(requires_signin=True))

        assert connector.capabilities.requires_manual_signin is True

    def test_built_in_platforms_are_unaffected(self):
        """A normal account still resolves by its own name."""
        connector = create_connector_for_account(
            PlatformAccount(
                platform="greenhouse",
                profile_dir="/tmp/unused",
            )
        )

        assert connector is not None
        assert connector.platform_name == "greenhouse"


# ============================================================================
# Sign-in detection
# ============================================================================

SIGNED_OUT_PAGE = """
<html><body>
  <h1>Sign in to continue</h1>
  <form action="/login" method="post">
    <input type="email" name="email">
    <input type="password" name="password">
    <button type="submit">Sign in</button>
  </form>
</body></html>
"""

SIGNED_IN_PAGE = """
<html><body>
  <nav><a href="/account">Your profile</a><a href="/logout">Sign out</a></nav>
  <h1>Recommended jobs</h1>
</body></html>
"""

AMBIGUOUS_PAGE = """
<html><body><h1>Careers at Acme</h1><p>We are hiring.</p></body></html>
"""


@pytest_asyncio.fixture
async def page_factory():
    """Serves HTML in a real browser, or skips if none is installed."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch()
        except Exception as e:
            pytest.skip(f"No browser available (run `playwright install chromium`): {e}")

        async def build(html: str):
            page = await browser.new_page()
            await page.set_content(html)
            return page

        yield build
        await browser.close()


@pytest.mark.asyncio
class TestSessionDetection:
    """What the agent is willing to call 'connected'."""

    def _connector(self, requires_signin: bool = True) -> GenericATSConnector:
        connector = GenericATSConnector("acme")
        connector.capabilities.requires_manual_signin = requires_signin
        return connector

    async def test_a_way_to_sign_out_proves_a_session(self, page_factory):
        connector = self._connector()
        connector.set_page(await page_factory(SIGNED_IN_PAGE))

        assert await connector.check_session() == "connected"

    async def test_a_login_form_means_signed_out(self, page_factory):
        connector = self._connector()
        connector.set_page(await page_factory(SIGNED_OUT_PAGE))

        assert await connector.check_session() == "needs_signin"

    async def test_an_unprovable_session_is_not_claimed(self, page_factory):
        """
        The old placeholder returned 'connected' for anything. A page showing
        neither state is not evidence of a session, and reporting one produces
        runs that search a logged-out page and come back empty.
        """
        connector = self._connector()
        connector.set_page(await page_factory(AMBIGUOUS_PAGE))

        assert await connector.check_session() == "needs_signin"

    async def test_public_boards_have_nothing_to_sign_into(self, page_factory):
        """A station the user marked public is usable without any sign-in."""
        connector = self._connector(requires_signin=False)
        connector.set_page(await page_factory(AMBIGUOUS_PAGE))

        assert await connector.check_session() == "connected"

    async def test_no_page_is_not_a_session(self):
        assert await self._connector().check_session() == "needs_signin"


# ============================================================================
# Reading a board the connector has never seen
# ============================================================================

BOARD_WITHOUT_JOB_CLASSES = """
<html><body>
  <nav><a href="/about">About</a><a href="/blog">Blog</a></nav>
  <ul class="listings">
    <li><a href="/careers/senior-frontend-engineer">Senior Frontend Engineer</a></li>
    <li><a href="/careers/backend-engineer">Backend Engineer</a></li>
    <li><a href="/careers/data-analyst">Data Analyst</a></li>
  </ul>
  <a href="/careers/find-your-plan?utm_source=nav">Post a job</a>
  <a href="https://twitter.com/acme">Twitter</a>
</body></html>
"""

BOARD_WITH_ONE_LINK = """
<html><body>
  <a href="/careers/the-only-job">The only job</a>
  <a href="/about">About</a>
</body></html>
"""

POSTING_WITHOUT_A_COMPANY_CLASS = """
<html><head>
  <title>Job Application for Staff Engineer at Acme</title>
</head><body>
  <h1>Staff Engineer</h1>
  <div class="location">Remote, US</div>
  <main>We are looking for a staff engineer to join the platform team and own
  the reliability of our public API across every region we operate in.</main>
</body></html>
"""


@pytest.mark.asyncio
class TestReadingAnUnknownBoard:
    """Boards that don't label their markup the way the selectors expect."""

    async def test_listings_are_found_by_url_shape(self, page_factory):
        connector = GenericATSConnector("acme")
        page = await page_factory(BOARD_WITHOUT_JOB_CLASSES)
        await page.goto("data:text/html," + BOARD_WITHOUT_JOB_CLASSES)
        connector.set_page(page)

        links = await connector.collect_job_links()

        assert len(links) == 3
        assert all("/careers/" in link for link in links)

    async def test_offsite_and_tracking_links_are_left_out(self, page_factory):
        connector = GenericATSConnector("acme")
        page = await page_factory(BOARD_WITHOUT_JOB_CLASSES)
        await page.goto("data:text/html," + BOARD_WITHOUT_JOB_CLASSES)
        connector.set_page(page)

        links = await connector.collect_job_links()

        assert not any("twitter" in link for link in links)
        assert not any("utm_source" in link for link in links)

    async def test_a_single_link_is_not_a_board(self, page_factory):
        """One link of a shape is a menu item, not a listings page."""
        connector = GenericATSConnector("acme")
        page = await page_factory(BOARD_WITH_ONE_LINK)
        await page.goto("data:text/html," + BOARD_WITH_ONE_LINK)
        connector.set_page(page)

        assert await connector.collect_job_links() == []

    async def test_a_posting_survives_having_no_company_element(self, page_factory):
        """
        Requiring a `class*='company'` element threw away every posting on
        boards that name the company only in the page title — which is most
        of them.
        """
        connector = GenericATSConnector("acme")
        connector.set_page(await page_factory(POSTING_WITHOUT_A_COMPANY_CLASS))

        posting = await connector._extract_from_html()

        assert posting is not None
        assert posting.title == "Staff Engineer"
        assert posting.company == "Acme"
        assert posting.location == "Remote, US"

    async def test_postings_get_distinct_ids_from_their_urls(self, page_factory):
        """
        Deriving the id from parsed text gave every unparsed posting on a board
        the same one, and deduplication then collapsed the lot into a single job.
        """
        connector = GenericATSConnector("acme")
        page = await page_factory(POSTING_WITHOUT_A_COMPANY_CLASS)
        connector.set_page(page)

        # Serve the same posting markup at two URLs, offline.
        await page.route(
            "https://acme.test/**",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body=POSTING_WITHOUT_A_COMPANY_CLASS,
            ),
        )

        await page.goto("https://acme.test/careers/staff-engineer")
        first = await connector._extract_from_html()

        await page.goto("https://acme.test/careers/platform-engineer")
        second = await connector._extract_from_html()

        assert first.external_id != second.external_id


# ============================================================================
# Adding a station through the API
# ============================================================================

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


class TestAddStationEndpoint:
    """POST /api/v1/accounts/custom"""

    def test_a_public_board_is_searchable_at_once(self, client):
        response = client.post(
            "/api/v1/accounts/custom",
            json={"name": "Acme careers", "url": "https://acme.test/careers"},
        )

        assert response.status_code == 200

        body = response.json()
        assert body["platform"] == "acme_careers"
        assert body["connection_status"] == ConnectionStatus.CONNECTED.value

    def test_a_board_behind_a_login_waits_to_be_signed_into(self, client):
        response = client.post(
            "/api/v1/accounts/custom",
            json={
                "name": "Acme careers",
                "url": "https://acme.test/careers",
                "requires_signin": True,
            },
        )

        assert response.json()["connection_status"] == ConnectionStatus.NEEDS_SIGNIN.value

    def test_the_station_is_wired_to_the_generic_connector(self, client):
        client.post(
            "/api/v1/accounts/custom",
            json={"name": "Acme careers", "url": "https://acme.test/careers"},
        )

        account = (
            client.db.query(PlatformAccount)
            .filter(PlatformAccount.platform == "acme_careers")
            .one()
        )

        assert account.connector_kind == "generic_ats"
        assert account.search_url == "https://acme.test/careers"

    def test_a_url_without_a_scheme_is_refused(self, client):
        response = client.post(
            "/api/v1/accounts/custom",
            json={"name": "Acme", "url": "acme.test/careers"},
        )

        assert response.status_code == 400
        assert "http" in response.json()["detail"]

    def test_a_built_in_platform_cannot_be_shadowed(self, client):
        """Naming a station 'linkedin' would quietly displace the real connector."""
        response = client.post(
            "/api/v1/accounts/custom",
            json={"name": "LinkedIn", "url": "https://linkedin.test/jobs"},
        )

        assert response.status_code == 409

    def test_the_same_station_cannot_be_added_twice(self, client):
        payload = {"name": "Acme careers", "url": "https://acme.test/careers"}

        assert client.post("/api/v1/accounts/custom", json=payload).status_code == 200
        assert client.post("/api/v1/accounts/custom", json=payload).status_code == 409

    def test_a_name_with_no_letters_is_refused(self, client):
        response = client.post(
            "/api/v1/accounts/custom",
            json={"name": "!!!", "url": "https://acme.test/careers"},
        )

        assert response.status_code == 400


# ============================================================================
# A station with nowhere to search
# ============================================================================

class TestStationWithNoBoard:
    """
    Greenhouse and friends host one board per company. Connected without one,
    the connector had no page to open, and the run reported "0 jobs found" —
    which sends the user off rewriting a search profile that was never used.
    """

    def test_health_flags_a_platform_that_needs_a_board(self, client):
        client.db.add(
            PlatformAccount(
                platform="greenhouse",
                status=ConnectionStatus.CONNECTED,
                profile_dir="/tmp/unused",
            )
        )
        client.db.commit()

        platform = client.get("/api/v1/health").json()["platforms"][0]

        assert platform["needs_search_url"] is True

    def test_a_board_url_clears_the_flag(self, client):
        client.db.add(
            PlatformAccount(
                platform="greenhouse",
                status=ConnectionStatus.CONNECTED,
                profile_dir="/tmp/unused",
                search_url="https://job-boards.greenhouse.io/acme",
            )
        )
        client.db.commit()

        platform = client.get("/api/v1/health").json()["platforms"][0]

        assert platform["needs_search_url"] is False

    def test_platforms_carrying_their_own_search_are_not_flagged(self, client):
        """LinkedIn knows where its own search lives; it needs nothing set."""
        client.db.add(
            PlatformAccount(
                platform="linkedin",
                status=ConnectionStatus.CONNECTED,
                profile_dir="/tmp/unused",
            )
        )
        client.db.commit()

        platform = client.get("/api/v1/health").json()["platforms"][0]

        assert platform["needs_search_url"] is False

    @pytest.mark.asyncio
    async def test_a_run_skips_it_with_a_reason(self, client):
        from job_agent.core.orchestrator import RunOrchestrator
        from job_agent.models.database import SearchProfile

        client.db.add(
            PlatformAccount(
                platform="greenhouse",
                status=ConnectionStatus.CONNECTED,
                profile_dir="/tmp/unused",
            )
        )
        profile = SearchProfile(name="p", target_titles=["Backend Engineer"])
        client.db.add(profile)
        client.db.commit()
        client.db.refresh(profile)

        run = await RunOrchestrator(client.db).run(profile)

        assert run.platforms_run == []
        assert "no board to search" in run.platforms_skipped["greenhouse"]
        # The reason has to reach the summary — that is what the desk shows.
        assert "no board to search" in RunOrchestrator.summarize(run)


class TestPageWithNoListings:
    """
    A URL that loads but holds no jobs used to arrive as "0 jobs found",
    indistinguishable from a board that simply had no matches. Pointing a
    station at an account dashboard instead of a job board is the easy
    mistake, and that message gave no way to tell.
    """

    class _Connector:
        def __init__(self, url):
            self.page = type("P", (), {"url": url})()

    def _account(self, search_url):
        return PlatformAccount(
            platform="greenhouse",
            profile_dir="/tmp/unused",
            search_url=search_url,
        )

    def test_a_sign_in_page_is_named_as_such(self):
        from job_agent.core.search_pipeline import SearchPipeline

        message = SearchPipeline._nothing_to_read(
            self._Connector("https://my.greenhouse.io/users/sign_in"),
            self._account("https://my.greenhouse.io/dashboard"),
        )

        assert "sign-in page" in message
        assert "not a job board" in message

    def test_an_empty_board_says_the_page_had_no_listings(self):
        from job_agent.core.search_pipeline import SearchPipeline

        message = SearchPipeline._nothing_to_read(
            self._Connector("https://job-boards.greenhouse.io/acme"),
            self._account("https://job-boards.greenhouse.io/acme"),
        )

        assert "no job postings" in message
        assert "job-boards.greenhouse.io/acme" in message
