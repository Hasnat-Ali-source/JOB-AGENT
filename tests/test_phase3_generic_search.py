#!/usr/bin/env python3
"""
Tests for GenericATSConnector search navigation (Phase 3).

Two layers:

- Strategy tests use a stub page object, so the URL building and the
  template-vs-plain-URL-vs-on-page decision are verified without a browser.
- Browser tests drive real HTML through Playwright to prove the CSS selectors
  actually match a typical career-site search form. They skip automatically
  when no browser is installed (`playwright install chromium`).
"""

from typing import List, Optional
from urllib.parse import parse_qs, urlparse

import pytest
import pytest_asyncio

from job_agent.connectors.generic_ats import GenericATSConnector
from job_agent.models.database import SearchProfile


# ============================================================================
# Stub page (no browser)
# ============================================================================

class StubLocator:
    """Minimal Playwright-locator stand-in."""

    def __init__(self, visible: bool = False, options: Optional[List[str]] = None):
        self._visible = visible
        self._options = options or []
        self.filled: Optional[str] = None
        self.pressed: Optional[str] = None
        self.clicked = False
        self.checked = False
        self.selected: Optional[str] = None

    @property
    def first(self):
        return self

    async def is_visible(self) -> bool:
        return self._visible

    async def fill(self, value: str) -> None:
        self.filled = value

    async def press(self, key: str) -> None:
        self.pressed = key

    async def click(self) -> None:
        self.clicked = True

    async def check(self) -> None:
        self.checked = True

    async def select_option(self, label: str) -> None:
        self.selected = label

    async def all_text_contents(self) -> List[str]:
        return self._options

    def locator(self, selector: str):
        return StubLocator(visible=True, options=self._options)


class StubPage:
    """
    Page stand-in that resolves only the selectors it is told about.

    Anything else reports not-visible, which is how a real page behaves for
    markup a site doesn't have.
    """

    def __init__(self, url: str = "https://acme.test/careers", elements: Optional[dict] = None):
        self.url = url
        self.elements = elements or {}
        self.goto_calls: List[str] = []
        self.load_state_waits = 0

    async def goto(self, url: str) -> None:
        self.goto_calls.append(url)
        self.url = url

    def locator(self, selector: str) -> StubLocator:
        return self.elements.get(selector, StubLocator(visible=False))

    async def wait_for_load_state(self, state: str, timeout: int = 0) -> None:
        self.load_state_waits += 1


def make_profile(**overrides) -> SearchProfile:
    """SearchProfile with sensible search defaults."""
    defaults = dict(
        name="Backend - Remote",
        target_titles=["Backend Engineer"],
        keywords=["python"],
        remote_pref="remote",
    )
    defaults.update(overrides)
    return SearchProfile(**defaults)


# ============================================================================
# Query / URL construction
# ============================================================================

class TestSearchTermBuilding:
    """Turning a SearchProfile into search-box terms."""

    def test_query_uses_first_target_title(self):
        profile = make_profile(target_titles=["Backend Engineer", "Platform Engineer"])
        assert GenericATSConnector._build_query(profile) == "Backend Engineer"

    def test_query_falls_back_to_keywords(self):
        profile = make_profile(target_titles=[], keywords=["python", "aws", "postgres", "k8s"])
        # Capped at three terms — search boxes narrow fast with more
        assert GenericATSConnector._build_query(profile) == "python aws postgres"

    def test_query_empty_when_nothing_specified(self):
        profile = make_profile(target_titles=[], keywords=[])
        assert GenericATSConnector._build_query(profile) == ""

    def test_location_prefers_region_then_country(self):
        assert GenericATSConnector._build_location(
            make_profile(region="California", country="US")) == "California"
        assert GenericATSConnector._build_location(
            make_profile(country="US", remote_pref=None)) == "US"

    def test_location_falls_back_to_remote(self):
        profile = make_profile(remote_pref="remote")
        assert GenericATSConnector._build_location(profile) == "Remote"

    def test_location_empty_for_unconstrained_profile(self):
        profile = make_profile(remote_pref=None)
        assert GenericATSConnector._build_location(profile) == ""


class TestSearchUrlBuilding:
    """Template substitution."""

    def test_template_is_url_encoded(self):
        connector = GenericATSConnector(
            search_url="https://acme.test/jobs?q={query}&loc={location}")

        url = connector._build_search_url("Backend Engineer", "San Francisco, CA")
        params = parse_qs(urlparse(url).query)

        assert params["q"] == ["Backend Engineer"]
        assert params["loc"] == ["San Francisco, CA"]
        assert " " not in url  # encoded, not raw

    def test_plain_url_passes_through(self):
        connector = GenericATSConnector(search_url="https://acme.test/careers")
        assert connector._build_search_url("anything", "anywhere") == "https://acme.test/careers"

    def test_template_detection(self):
        assert GenericATSConnector._is_template("https://x.test?q={query}")
        assert GenericATSConnector._is_template("https://x.test?l={location}")
        assert not GenericATSConnector._is_template("https://x.test/careers")


# ============================================================================
# open_search strategy selection
# ============================================================================

@pytest.mark.asyncio
class TestOpenSearchStrategies:
    """Which of the three navigation strategies runs, and when."""

    async def test_requires_a_page(self):
        connector = GenericATSConnector()

        with pytest.raises(RuntimeError, match="No page attached"):
            await connector.open_search(make_profile())

    async def test_template_url_navigates_without_touching_the_form(self):
        page = StubPage()
        connector = GenericATSConnector(
            search_url="https://acme.test/jobs?q={query}&loc={location}")
        connector.set_page(page)

        await connector.open_search(make_profile())

        assert len(page.goto_calls) == 1
        assert "Backend+Engineer" in page.goto_calls[0]
        assert "Remote" in page.goto_calls[0]
        assert connector.last_search_url == page.goto_calls[0]

    async def test_plain_url_navigates_then_drives_the_form(self):
        search_box = StubLocator(visible=True)
        page = StubPage(elements={"input[type='search']": search_box})
        connector = GenericATSConnector(search_url="https://acme.test/careers")
        connector.set_page(page)

        await connector.open_search(make_profile())

        assert page.goto_calls == ["https://acme.test/careers"]
        assert search_box.filled == "Backend Engineer"
        assert search_box.pressed == "Enter"  # no submit button in this page

    async def test_no_url_searches_the_current_page(self):
        search_box = StubLocator(visible=True)
        page = StubPage(elements={"input[name*='keyword' i]": search_box})
        connector = GenericATSConnector()
        connector.set_page(page)

        await connector.open_search(make_profile())

        assert page.goto_calls == []  # never navigated away
        assert search_box.filled == "Backend Engineer"
        assert connector.last_search_url == page.url

    async def test_submit_button_preferred_over_enter(self):
        search_box = StubLocator(visible=True)
        submit = StubLocator(visible=True)
        page = StubPage(elements={
            "input[type='search']": search_box,
            "button[type='submit']": submit,
        })
        connector = GenericATSConnector()
        connector.set_page(page)

        await connector.open_search(make_profile())

        assert submit.clicked is True
        assert search_box.pressed is None

    async def test_location_field_filled_when_present(self):
        search_box = StubLocator(visible=True)
        location_box = StubLocator(visible=True)
        page = StubPage(elements={
            "input[type='search']": search_box,
            "input[name*='location' i]": location_box,
        })
        connector = GenericATSConnector()
        connector.set_page(page)

        await connector.open_search(make_profile(region="Singapore"))

        assert location_box.filled == "Singapore"

    async def test_no_form_and_no_url_leaves_page_alone(self):
        """A page with no search form is read as-is rather than erroring."""
        page = StubPage()
        connector = GenericATSConnector()
        connector.set_page(page)

        await connector.open_search(make_profile())

        assert page.goto_calls == []
        assert connector.last_search_url is None  # honest: no search was run

    async def test_set_search_url_configures_navigation(self):
        """The pipeline configures the URL after construction."""
        page = StubPage()
        connector = GenericATSConnector()
        connector.set_page(page)
        connector.set_search_url("https://acme.test/jobs?q={query}")

        await connector.open_search(make_profile())

        assert page.goto_calls[0].startswith("https://acme.test/jobs?q=Backend")


@pytest.mark.asyncio
class TestApplySearchFilters:
    """Best-effort filter application."""

    async def test_date_posted_select(self):
        dropdown = StubLocator(visible=True, options=["Any time", "Past 7 days", "Past 30 days"])
        page = StubPage(elements={"select[name*='date' i]": dropdown})
        connector = GenericATSConnector()
        connector.set_page(page)

        await connector.apply_search_filters(make_profile(date_posted_within_days=7))

        assert dropdown.selected == "Past 7 days"
        assert "date_posted<=7d" in connector.applied_filters

    async def test_job_type_select_matches_spaced_label(self):
        """Profile stores "full_time"; sites label it "Full time"."""
        dropdown = StubLocator(visible=True, options=["All", "Full time", "Contract"])
        page = StubPage(elements={"select[name*='type' i]": dropdown})
        connector = GenericATSConnector()
        connector.set_page(page)

        await connector.apply_search_filters(make_profile(job_type="full_time"))

        assert dropdown.selected == "Full time"

    async def test_remote_checkbox(self):
        checkbox = StubLocator(visible=True)
        page = StubPage(elements={"input[type='checkbox'][name*='remote' i]": checkbox})
        connector = GenericATSConnector()
        connector.set_page(page)

        await connector.apply_search_filters(make_profile(remote_pref="remote"))

        assert checkbox.checked is True
        assert "remote=true" in connector.applied_filters

    async def test_missing_filters_are_skipped_silently(self):
        page = StubPage()
        connector = GenericATSConnector()
        connector.set_page(page)

        await connector.apply_search_filters(
            make_profile(date_posted_within_days=7, job_type="full_time"))

        assert connector.applied_filters == []  # nothing claimed that didn't happen

    async def test_no_page_is_a_no_op(self):
        connector = GenericATSConnector()
        await connector.apply_search_filters(make_profile())  # must not raise


# ============================================================================
# Browser-backed selector tests
# ============================================================================

CAREER_PAGE = """
<!DOCTYPE html>
<html><body>
  <form id="searchForm" onsubmit="runSearch(); return false;">
    <input type="search" name="keywords" placeholder="Search jobs">
    <input type="text" name="location" placeholder="Location">
    <select name="date_posted">
      <option>Any time</option>
      <option>Past 7 days</option>
      <option>Past 30 days</option>
    </select>
    <select name="employment_type">
      <option>All types</option>
      <option>Full time</option>
      <option>Contract</option>
    </select>
    <label><input type="checkbox" name="remote_only"> Remote only</label>
    <button type="submit">Search</button>
  </form>
  <div id="results"></div>
  <script>
    function runSearch() {
      const q = document.querySelector("input[name=keywords]").value;
      const loc = document.querySelector("input[name=location]").value;
      document.getElementById('results').innerHTML =
        '<div class="job-card"><a href="/job/1">' + q + ' in ' + loc + '</a></div>';
    }
  </script>
</body></html>
"""


@pytest_asyncio.fixture
async def browser_page():
    """A real Playwright page, or skip if no browser is installed."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch()
        except Exception as e:
            pytest.skip(f"No browser available (run `playwright install chromium`): {e}")

        page = await browser.new_page()
        await page.set_content(CAREER_PAGE)
        yield page
        await browser.close()


@pytest.mark.asyncio
class TestAgainstRealMarkup:
    """The selectors have to match markup a real career site would serve."""

    async def test_search_form_is_found_and_submitted(self, browser_page):
        connector = GenericATSConnector()
        connector.set_page(browser_page)

        await connector.open_search(make_profile(region="Singapore"))

        results = await browser_page.locator("#results").inner_text()
        assert "Backend Engineer" in results
        assert "Singapore" in results

    async def test_filters_apply_to_real_selects(self, browser_page):
        connector = GenericATSConnector()
        connector.set_page(browser_page)

        await connector.apply_search_filters(
            make_profile(date_posted_within_days=7, job_type="full_time", remote_pref="remote"))

        assert await browser_page.locator("select[name=date_posted]").input_value() == "Past 7 days"
        assert await browser_page.locator("select[name=employment_type]").input_value() == "Full time"
        assert await browser_page.locator("input[name=remote_only]").is_checked()
        assert len(connector.applied_filters) == 3

    async def test_collect_job_links_reads_results(self, browser_page):
        connector = GenericATSConnector()
        connector.set_page(browser_page)

        await connector.open_search(make_profile())
        links = await connector.collect_job_links()

        assert any(link.endswith("/job/1") for link in links)


# ============================================================================
# JSON-LD parsing (regressions found against live pages)
# ============================================================================

class TestJsonLdShapes:
    """Real postings vary the shape of every field."""

    def test_finds_bare_job_posting(self):
        data = {"@type": "JobPosting", "title": "Backend Engineer"}
        assert GenericATSConnector._find_job_posting(data) == data

    def test_finds_posting_in_list(self):
        posting = {"@type": "JobPosting", "title": "X"}
        assert GenericATSConnector._find_job_posting(
            [{"@type": "BreadcrumbList"}, posting]) == posting

    def test_finds_posting_in_graph(self):
        posting = {"@type": "JobPosting", "title": "X"}
        assert GenericATSConnector._find_job_posting(
            {"@graph": [{"@type": "Organization"}, posting]}) == posting

    def test_finds_posting_in_item_list(self):
        posting = {"@type": "JobPosting", "title": "X"}
        assert GenericATSConnector._find_job_posting(
            {"@type": "ItemList", "itemListElement": [{"item": posting}]}) == posting

    def test_handles_type_as_list(self):
        posting = {"@type": ["JobPosting", "Thing"], "title": "X"}
        assert GenericATSConnector._find_job_posting(posting) == posting

    def test_returns_none_without_a_posting(self):
        assert GenericATSConnector._find_job_posting({"@type": "Organization"}) is None
        assert GenericATSConnector._find_job_posting("not json-ld") is None

    def test_scalar_accepts_string_or_object(self):
        """`identifier` is a bare string on some sites, an object on others."""
        assert GenericATSConnector._scalar("R-1234", "value") == "R-1234"
        assert GenericATSConnector._scalar(
            {"@type": "PropertyValue", "value": "R-1234"}, "value") == "R-1234"

    def test_scalar_reads_named_key(self):
        assert GenericATSConnector._scalar({"name": "TechCorp"}, "name") == "TechCorp"

    def test_scalar_falls_back_across_common_keys(self):
        assert GenericATSConnector._scalar({"name": "TechCorp"}) == "TechCorp"
        assert GenericATSConnector._scalar({"@value": "2026-08-12"}) == "2026-08-12"

    def test_scalar_picks_first_of_a_list(self):
        assert GenericATSConnector._scalar([{"name": "A"}, {"name": "B"}], "name") == "A"

    def test_scalar_handles_missing_and_empty(self):
        assert GenericATSConnector._scalar(None) is None
        assert GenericATSConnector._scalar("   ") is None
        assert GenericATSConnector._scalar({}) is None


class TestSalaryShapes:
    """schema.org nests salary figures a level down; both shapes must work."""

    def test_nested_quantitative_value(self):
        connector = GenericATSConnector()
        result = connector._extract_salary({
            "@type": "MonetaryAmount", "currency": "USD",
            "value": {"@type": "QuantitativeValue",
                      "minValue": 150000, "maxValue": 170000, "unitText": "YEAR"},
        })
        assert result == "USD 150,000 - 170,000"

    def test_flat_shape_still_works(self):
        connector = GenericATSConnector()
        assert connector._extract_salary(
            {"minValue": 100000, "maxValue": 150000, "currency": "USD"}
        ) == "USD 100,000 - 150,000"

    def test_minimum_only(self):
        connector = GenericATSConnector()
        assert connector._extract_salary({"currency": "EUR", "value": {"minValue": 90000}}) == "EUR 90,000+"

    def test_scalar_value(self):
        connector = GenericATSConnector()
        assert connector._extract_salary({"currency": "GBP", "value": 75000}) == "GBP 75,000"

    def test_unparseable_returns_none(self):
        connector = GenericATSConnector()
        assert connector._extract_salary({}) is None
        assert connector._extract_salary("not a dict") is None


# ============================================================================
# Full pipeline against a live career site
# ============================================================================

LIVE_JOBS = {
    "1": ("Senior Backend Engineer", "TechCorp", "Remote", 150000),
    "2": ("Backend Engineer", "Globex", "Remote", 130000),
    "3": ("Junior Backend Engineer", "CheapCo", "Remote", 80000),
    # Same posting as #1 with different wording — must deduplicate
    "4": ("Sr. Backend Engineer", "TechCorp Inc", "Remote", 150000),
}


def _make_handler():
    """Build a request handler serving a search page and JSON-LD job pages."""
    from http.server import BaseHTTPRequestHandler

    class CareerSiteHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # keep pytest output clean

        def do_GET(self):
            path = urlparse(self.path).path

            if path == "/careers":
                query = parse_qs(urlparse(self.path).query).get("q", [""])[0]
                cards = "".join(
                    f'<div class="job-card"><a href="/job/{jid}">{title}</a></div>'
                    for jid, (title, *_rest) in LIVE_JOBS.items()
                )
                body = f"<html><body><h1>Results for {query}</h1>{cards}</body></html>"

            elif path.startswith("/job/"):
                jid = path.rsplit("/", 1)[-1]
                title, company, location, salary = LIVE_JOBS[jid]
                # Spec-shaped JSON-LD: string identifier, nested baseSalary
                body = (
                    '<html><head><script type="application/ld+json">'
                    '{"@context":"https://schema.org","@type":"JobPosting",'
                    f'"identifier":"REQ-{jid}","title":"{title}",'
                    f'"hiringOrganization":{{"@type":"Organization","name":"{company}"}},'
                    f'"jobLocation":{{"@type":"Place","address":'
                    f'{{"@type":"PostalAddress","addressLocality":"{location}"}}}},'
                    '"description":"Python services on AWS.","employmentType":"FULL_TIME",'
                    '"datePosted":"2026-08-12",'
                    '"baseSalary":{"@type":"MonetaryAmount","currency":"USD","value":'
                    f'{{"@type":"QuantitativeValue","minValue":{salary},'
                    f'"maxValue":{salary + 20000},"unitText":"YEAR"}}}}}}'
                    f'</script></head><body><h1>{title}</h1></body></html>'
                )
            else:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            raw = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return CareerSiteHandler


@pytest.fixture
def career_site():
    """Serve a small career site on a free port; yields its base URL."""
    import threading
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://127.0.0.1:{server.server_port}"

    server.shutdown()
    thread.join(timeout=5)


@pytest.mark.asyncio
class TestLivePipeline:
    """
    The Phase 3 acceptance criteria, exercised through a browser against a
    real HTTP career site rather than a stubbed connector.
    """

    async def test_full_search_to_storage(self, career_site, browser_page):
        from sqlalchemy.pool import StaticPool
        from sqlmodel import Session, SQLModel, create_engine

        from job_agent.core.search_pipeline import SearchPipeline
        from job_agent.models.database import Job, PlatformAccount

        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        SQLModel.metadata.create_all(engine)
        session = Session(engine)

        account = PlatformAccount(
            platform="generic_ats",
            profile_dir="/tmp/job-agent-test/generic",
            daily_search_limit=10,
            search_url=f"{career_site}/careers?q={{query}}",
        )
        profile = make_profile(salary_min=100000)
        session.add(account)
        session.add(profile)
        session.commit()
        session.refresh(account)
        session.refresh(profile)

        connector = GenericATSConnector()
        connector.set_page(browser_page)
        connector.set_search_url(account.search_url)

        result = await SearchPipeline(session).search(account, profile, connector=connector)

        # Navigation actually happened, using the templated URL
        assert connector.last_search_url.endswith("/careers?q=Backend+Engineer")

        # 4 postings, one of which duplicates another
        assert result.jobs_found == 4
        assert result.new_jobs == 3
        assert result.duplicates_skipped == 1
        assert result.errors == []

        # Salary parsed out of nested JSON-LD, so the hard filter can act
        stored = {j.company: j for j in session.query(Job).all()}
        assert stored["TechCorp"].salary == "USD 150,000 - 170,000"
        assert stored["TechCorp"].hard_filter_pass is True

        # The $80k posting fails the $100k minimum but is kept for review
        assert result.hard_filters_failed == 1
        assert stored["CheapCo"].hard_filter_pass is False
        assert stored["CheapCo"].status == "filtered_out"

    async def test_unparseable_page_returns_quickly(self, career_site, browser_page):
        """
        A page with no JSON-LD and no recognizable markup must fail fast.

        Every speculative selector previously waited Playwright's 30s default,
        so one such posting stalled a run for ~35 seconds.
        """
        import time

        await browser_page.goto(f"{career_site}/nothing-here")
        await browser_page.set_content("<html><body><p>no job data</p></body></html>")

        connector = GenericATSConnector()
        connector.set_page(browser_page)

        started = time.monotonic()
        posting = await connector.read_job_details("")
        elapsed = time.monotonic() - started

        assert posting.title == "Job Posting"  # the honest "couldn't parse" result
        assert elapsed < 5  # was ~35s before existence checks replaced auto-waits


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
