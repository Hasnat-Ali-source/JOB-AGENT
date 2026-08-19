"""
Generic ATS (Applicant Tracking System) Connector (Phase 2).

Works with any job site that uses standard HTML patterns for job listings.
Implements the ConnectedPlatformConnector interface for generic career sites.

Typical sites using ATS patterns:
- Company career pages (most use similar HTML patterns)
- Job boards with consistent markup
- Greenhouse, Lever, and similar ATS providers

The generic connector uses:
- Common CSS selectors for job listing elements
- JSON-LD structured data for job details
- Standard form patterns for applications
- Email detection for jobs that only accept email applications
"""

import html
import logging
import re
from typing import List, Optional, Dict, Any, TYPE_CHECKING
from urllib.parse import quote_plus, urljoin, urlparse

from job_agent.connectors.base import (
    ConnectedPlatformConnector,
    PlatformCapabilities,
    JobPosting,
    ApplicationSession,
    SubmissionResult,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from job_agent.models.database import SearchProfile

# Speculative selector probes get a short leash. Playwright's 30s default
# applies per call, and most of these selectors are expected to miss.
EXTRACT_TIMEOUT_MS = 2000

# Evidence that a session exists. A way to sign *out* is the only thing on a
# page that can only be there for someone already signed in — "Sign in" links,
# avatars and account menus all appear on signed-out pages too.
SIGNED_IN_SELECTORS = [
    "a[href*='logout' i]",
    "a[href*='log-out' i]",
    "a[href*='signout' i]",
    "a[href*='sign-out' i]",
    "button[name*='logout' i]",
    "form[action*='logout' i]",
    "[data-testid*='logout' i]",
    "[aria-label*='sign out' i]",
    "[aria-label*='log out' i]",
]

# Evidence that there is no session: somewhere to type a password, or an
# invitation to start one.
SIGNED_OUT_SELECTORS = [
    "input[type='password']",
    "form[action*='login' i]",
    "form[action*='signin' i]",
    "a[href*='/login' i]",
    "a[href*='/signin' i]",
    "a[href*='/sign-in' i]",
    "[data-testid*='signin' i]",
    "[data-testid*='login' i]",
]

# Pages that show nothing about anything. Probing these for a sign-out control
# would report every platform as signed out.
BLANK_URLS = {"", "about:blank", "chrome://newtab/"}

# Path segments that mark a link as a posting rather than navigation. Used by
# the fallback that reads a board by the shape of its URLs when its markup
# doesn't use the class names the selectors above look for.
JOB_PATH_HINTS = (
    "/job",
    "/jobs",
    "/remote-job",
    "/career",
    "/careers",
    "/vacanc",
    "/position",
    "/opening",
    "/listing",
    "/role",
)

# One link of a shape proves nothing; a board repeats its posting URL shape.
MIN_LINKS_FOR_A_BOARD = 3



class GenericATSConnector(ConnectedPlatformConnector):
    """
    Generic connector for ATS-based job sites.
    
    Uses common HTML patterns and JSON-LD structured data to:
    - Find job listings
    - Extract job details (title, company, location, description, etc.)
    - Identify apply methods (web form or email)
    - Open application forms
    
    Designed to work with any site that follows standard web patterns.
    More specific connectors (LinkedIn, Greenhouse, etc.) will override
    this generic implementation with site-specific logic.
    
    Examples:
    - Company career pages (most use similar HTML)
    - Greenhouse-hosted jobs
    - Lever-hosted jobs
    - Indeed postings
    """
    
    def __init__(
        self,
        platform_name: str = "generic_ats",
        search_url: Optional[str] = None,
    ):
        """
        Initialize generic ATS connector.

        Args:
            platform_name: Name of the platform (used for logging)
            search_url: Career-site search page. Two forms are accepted:
                - A template with `{query}` and/or `{location}` placeholders,
                  e.g. "https://acme.com/careers?q={query}&loc={location}" —
                  values are URL-encoded and substituted, no form interaction
                  needed.
                - A plain URL, e.g. "https://acme.com/careers" — the connector
                  navigates there and then drives the page's own search form.
        """
        capabilities = PlatformCapabilities(
            can_search=True,
            can_filter=True,
            can_read_details=True,
            can_start_application=True,
            can_fill_standard_fields=False,  # Phase 3+
            can_upload_documents=False,  # Phase 3+
            can_process_custom_questions=False,  # Phase 3+
            can_submit_automatically=False,  # Phase 3+
            requires_manual_signin=True,
            requires_manual_review_first_n=5,
            tos_risk_note="Generic connector works on public job boards only. Verify site's ToS before using.",
        )
        
        super().__init__(platform_name, capabilities)

        self.search_url = search_url

        # Records what the last open_search()/apply_search_filters() actually
        # managed to do, so the caller can tell a real search from a page that
        # was simply left as-is.
        self.last_search_url: Optional[str] = None
        self.applied_filters: List[str] = []

        # Common CSS selectors for ATS job listings
        self.selectors = {
            "job_listing": [
                "div[class*='job']",
                "article[class*='job']",
                "li[class*='job']",
                "div[data-job-id]",
                "article[data-job-id]",
            ],
            "job_title": [
                "h2, h3, h4",
                "[class*='title']",
                "[class*='position']",
                "a[class*='job']",
            ],
            "job_company": [
                "[class*='company']",
                "[class*='employer']",
                "span[class*='company']",
            ],
            "job_location": [
                "[class*='location']",
                "[class*='place']",
                "span[class*='location']",
            ],
            "apply_button": [
                "a[href*='apply']",
                "button[class*='apply']",
                "a[class*='apply']",
                "input[value*='Apply']",
            ],
            # Search form patterns, ordered most- to least-specific
            "search_input": [
                "input[type='search']",
                "input[name*='keyword' i]",
                "input[name='q']",
                "input[name*='search' i]",
                "input[id*='keyword' i]",
                "input[aria-label*='search' i]",
                "input[placeholder*='job title' i]",
                "input[placeholder*='search' i]",
            ],
            "location_input": [
                "input[name*='location' i]",
                "input[name*='where' i]",
                "input[id*='location' i]",
                "input[aria-label*='location' i]",
                "input[placeholder*='location' i]",
                "input[placeholder*='city' i]",
            ],
            "search_submit": [
                "button[type='submit']",
                "input[type='submit']",
                "button[class*='search' i]",
            ],
            "date_posted_select": [
                "select[name*='date' i]",
                "select[name*='posted' i]",
                "select[name*='age' i]",
            ],
            "job_type_select": [
                "select[name*='type' i]",
                "select[name*='employment' i]",
                "select[name*='commitment' i]",
            ],
            "remote_toggle": [
                "input[type='checkbox'][name*='remote' i]",
                "input[type='checkbox'][id*='remote' i]",
            ],
        }
    
    async def _any_selector_present(self, selectors: List[str]) -> bool:
        """
        Whether any of these selectors matches something on the current page.

        Args:
            selectors: CSS selectors to probe

        Returns:
            True on the first match
        """
        for selector in selectors:
            try:
                if await self._page.query_selector(selector):
                    return True
            except Exception:
                # A malformed or unsupported selector should not decide the
                # question of whether someone is signed in.
                continue

        return False

    async def _ensure_page_shows_platform(self) -> None:
        """
        Put the platform's own page in front of the session check.

        A context reopened for a scheduled run starts on a blank tab, and a
        blank tab has no sign-out control — checking it would report every
        platform as signed out.
        """
        try:
            current = (self._page.url or "").strip()
        except Exception:
            current = ""

        if current not in BLANK_URLS:
            return

        landing = self.search_url or getattr(self, "BOARD_URL_TEMPLATE", None)

        if not landing or "{" in landing:
            # Nothing safe to navigate to; judge whatever is on screen.
            return

        try:
            await self._page.goto(landing)
        except Exception as e:
            logger.warning(f"{self.platform_name}: could not open {landing}: {e}")

    async def check_session(self) -> str:
        """
        Check whether this platform still has a usable session.

        Read-only platforms have nothing to be signed in to: a page that loads
        is the whole requirement. Where sign-in *is* required, this looks for
        evidence rather than assuming — a way to sign out means a session
        exists; a password field or a sign-in link means it does not.

        When neither appears, the answer is "not signed in". A platform that
        cannot be shown to be connected must not be reported as connected: the
        cost of being wrong is a run that quietly returns nothing, having
        searched a logged-out page.

        Returns:
            'connected', 'needs_signin', or 'error'
        """
        if not self._page:
            return "needs_signin"

        if not self.capabilities.requires_manual_signin:
            # Nothing to sign in to. Reachability is the only question, and
            # having a page at all answers it.
            return "connected"

        try:
            await self._ensure_page_shows_platform()

            if await self._any_selector_present(SIGNED_IN_SELECTORS):
                return "connected"

            if await self._any_selector_present(SIGNED_OUT_SELECTORS):
                logger.info(f"{self.platform_name}: page shows a signed-out state")
                return "needs_signin"

            logger.info(
                f"{self.platform_name}: found no sign-out control, so the session "
                f"cannot be confirmed"
            )
            return "needs_signin"
        except Exception as e:
            logger.error(f"{self.platform_name}: session check failed: {e}")
            return "error"
    
    async def open_search(self, search_profile: 'SearchProfile') -> None:
        """
        Navigate to the job search page and run the query.

        Three strategies, in order:

        1. `search_url` is a template containing `{query}`/`{location}` — the
           profile's terms are URL-encoded into it and the page is opened
           directly. This is the most reliable path and the one to configure
           for a known career site.
        2. `search_url` is a plain URL — navigate there, then drive the page's
           own search form.
        3. No `search_url` — drive the search form on whatever page is already
           open (e.g. a career site the user navigated to during connect).

        If none of these can run, the current page is left untouched and a
        warning is logged; `collect_job_links()` will then simply read whatever
        listings the page already shows.

        Args:
            search_profile: Search filters (titles, location, etc.)

        Raises:
            RuntimeError: If no page has been attached via set_page()
        """
        if not self._page:
            raise RuntimeError(
                f"No page attached to {self.platform_name} connector — "
                f"call set_page() before open_search()"
            )

        self.last_search_url = None
        self.applied_filters = []

        query = self._build_query(search_profile)
        location = self._build_location(search_profile)

        logger.info(
            f"Opening search on {self.platform_name} "
            f"(query={query!r}, location={location!r})"
        )

        # Strategy 1 & 2: a configured search URL
        if self.search_url:
            url = self._build_search_url(query, location)
            response = await self._page.goto(url)
            self._note_response(response)
            await self._settle()
            self.last_search_url = self._page.url

            # A template URL already carries the query; a plain URL still needs
            # the on-page form driven.
            if not self._is_template(self.search_url):
                await self._search_on_page(query, location)

            return

        # Strategy 3: search the page we're already on
        searched = await self._search_on_page(query, location)

        if searched:
            self.last_search_url = self._page.url
        else:
            logger.warning(
                f"{self.platform_name}: no search_url configured and no search "
                f"form found on {self._page.url} — reading the current page as-is"
            )

    async def apply_search_filters(self, search_profile: 'SearchProfile') -> None:
        """
        Apply search filters to the current results page, best-effort.

        Career sites have no common filter markup, so this tries the widespread
        patterns (a date-posted select, a job-type select, a remote checkbox)
        and skips anything it cannot find. Whatever succeeded is recorded in
        `self.applied_filters`; filters that don't apply are re-checked in the
        hard-filter pass anyway, so a miss costs recall, not correctness.

        Args:
            search_profile: Search filters
        """
        if not self._page or not search_profile:
            return

        logger.info(f"Applying filters on {self.platform_name}")

        # Date posted — match the option whose text mentions the window
        if search_profile.date_posted_within_days:
            days = search_profile.date_posted_within_days
            if await self._select_option_matching(
                self.selectors["date_posted_select"],
                [f"{days} day", f"past {days}", f"last {days}"],
            ):
                self.applied_filters.append(f"date_posted<={days}d")

        # Job type — "full_time" also has to match "Full time" / "Full-time"
        if search_profile.job_type:
            variants = [
                search_profile.job_type.replace("_", " "),
                search_profile.job_type.replace("_", "-"),
                search_profile.job_type,
            ]
            if await self._select_option_matching(
                self.selectors["job_type_select"], variants
            ):
                self.applied_filters.append(f"job_type={search_profile.job_type}")

        # Remote toggle
        if search_profile.remote_pref == "remote":
            if await self._check_first(self.selectors["remote_toggle"]):
                self.applied_filters.append("remote=true")

        if self.applied_filters:
            await self._settle()
            logger.info(
                f"{self.platform_name}: applied filters {self.applied_filters}"
            )
        else:
            logger.info(f"{self.platform_name}: no site filters matched; "
                        f"relying on the hard-filter pass")

    # ------------------------------------------------------------------
    # Search helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_template(url: str) -> bool:
        """True if the URL carries {query}/{location} placeholders."""
        return "{query}" in url or "{location}" in url

    @staticmethod
    def _build_query(search_profile: 'SearchProfile') -> str:
        """
        Build the search text from a profile.

        Uses the first target title (career-site search boxes take one phrase,
        not a boolean expression); falls back to keywords when no title is set.
        """
        if not search_profile:
            return ""

        titles = search_profile.target_titles or []
        if titles:
            return titles[0]

        keywords = search_profile.keywords or []
        return " ".join(keywords[:3])

    @staticmethod
    def _build_location(search_profile: 'SearchProfile') -> str:
        """Build the location term: explicit region/country, or "Remote"."""
        if not search_profile:
            return ""

        if search_profile.region:
            return search_profile.region
        if search_profile.country:
            return search_profile.country
        if search_profile.remote_pref == "remote":
            return "Remote"

        return ""

    def _build_search_url(self, query: str, location: str) -> str:
        """
        Resolve `search_url` into a concrete URL.

        Args:
            query: Search text
            location: Location text

        Returns:
            URL with placeholders substituted (URL-encoded), or the plain URL
        """
        url = self.search_url

        if self._is_template(url):
            url = url.replace("{query}", quote_plus(query))
            url = url.replace("{location}", quote_plus(location))

        return url

    async def _search_on_page(self, query: str, location: str) -> bool:
        """
        Drive the search form on the current page.

        Args:
            query: Search text
            location: Location text

        Returns:
            True if a search input was found and submitted
        """
        search_box = await self._first_visible(self.selectors["search_input"])

        if not search_box:
            return False

        try:
            if query:
                await search_box.fill(query)

            if location:
                location_box = await self._first_visible(self.selectors["location_input"])
                if location_box:
                    await location_box.fill(location)

            submitted = await self._click_first(self.selectors["search_submit"])
            if not submitted:
                await search_box.press("Enter")

            await self._settle()
            logger.info(f"{self.platform_name}: ran on-page search for {query!r}")
            return True

        except Exception as e:
            logger.warning(f"{self.platform_name}: on-page search failed: {e}")
            return False

    async def _first_visible(self, selectors: List[str]):
        """
        Return the first visible element matching any selector, or None.

        Args:
            selectors: CSS selectors to try in order
        """
        for selector in selectors:
            try:
                element = self._page.locator(selector).first
                if await element.is_visible():
                    return element
            except Exception as e:
                logger.debug(f"Selector {selector} not usable: {e}")
                continue

        return None

    async def _click_first(self, selectors: List[str]) -> bool:
        """Click the first visible element matching any selector."""
        element = await self._first_visible(selectors)

        if not element:
            return False

        try:
            await element.click()
            return True
        except Exception as e:
            logger.debug(f"Click failed: {e}")
            return False

    async def _check_first(self, selectors: List[str]) -> bool:
        """Tick the first visible checkbox matching any selector."""
        element = await self._first_visible(selectors)

        if not element:
            return False

        try:
            await element.check()
            return True
        except Exception as e:
            logger.debug(f"Check failed: {e}")
            return False

    async def _select_option_matching(
        self,
        selectors: List[str],
        wanted: List[str],
    ) -> bool:
        """
        Select the first <option> whose label contains one of `wanted`.

        Args:
            selectors: Candidate <select> selectors
            wanted: Case-insensitive substrings to look for in option labels

        Returns:
            True if an option was selected
        """
        dropdown = await self._first_visible(selectors)

        if not dropdown:
            return False

        try:
            labels = await dropdown.locator("option").all_text_contents()

            for label in labels:
                lowered = label.lower()
                if any(w.lower() in lowered for w in wanted):
                    await dropdown.select_option(label=label)
                    return True
        except Exception as e:
            logger.debug(f"Option select failed: {e}")

        return False

    async def _settle(self) -> None:
        """Wait for the page to stop loading, tolerating sites that never idle."""
        try:
            await self._page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            logger.debug("networkidle wait timed out; continuing")
    
    async def collect_job_links(self) -> List[str]:
        """
        Collect links to all visible job listings on the current page.
        
        Uses common CSS patterns for job listing elements.
        
        Returns:
            List of job URLs
        """
        logger.info(f"Collecting job links from {self.platform_name}")
        
        if not hasattr(self, '_page') or not self._page:
            logger.warning("No active page context")
            return []
        
        job_links = []
        
        # Try each selector pattern
        for selector in self.selectors["job_listing"]:
            try:
                listings = await self._page.locator(selector).all()
                
                for listing in listings:
                    # Look for a link within the listing. Containers without an
                    # anchor are common, and get_attribute() auto-waits — so
                    # check existence first rather than paying the timeout.
                    try:
                        anchors = listing.locator("a")

                        if await anchors.count() == 0:
                            continue

                        link = await anchors.first.get_attribute(
                            "href", timeout=EXTRACT_TIMEOUT_MS
                        )
                    except Exception:
                        continue

                    if link:
                        # Convert relative URLs to absolute
                        full_url = urljoin(self._page.url, link)
                        if full_url not in job_links:
                            job_links.append(full_url)
                
                if job_links:
                    logger.info(f"Found {len(job_links)} jobs using selector: {selector}")
                    break
            except Exception as e:
                logger.debug(f"Selector {selector} failed: {e}")
                continue

        if not job_links:
            # The selectors above all assume the markup labels its listings
            # "job" somewhere in a class name. Plenty of boards don't, and
            # returning nothing for them reads as "this board has no jobs".
            job_links = await self._collect_links_by_url_shape()
        else:
            # A container selector can match a decorated subset — a "featured
            # roles" strip — and stopping there silently drops the rest of the
            # board. Both strategies read the same page; the fuller reading of
            # it wins.
            by_shape = await self._collect_links_by_url_shape()

            if len(by_shape) > len(job_links):
                logger.info(
                    f"{self.platform_name}: URL shape found {len(by_shape)} postings "
                    f"against the selectors' {len(job_links)} — using the fuller set"
                )
                job_links = by_shape

        if not job_links:
            status = getattr(self, "last_status", None)

            if status and status >= 400:
                # An empty page the site refused to serve is not an empty
                # board. Reported as zero jobs it looks like the search was
                # too narrow, and the user goes off widening a working filter.
                raise RuntimeError(
                    f"{self.platform_name} answered {status} for "
                    f"{self._page.url} — the site refused the request rather "
                    f"than returning no results. If this is a board that blocks "
                    f"automated search, point the station at a listings page "
                    f"instead of a search URL."
                )

        return job_links

    def set_page(self, page: Any) -> None:
        """
        Attach a page and start watching how the site answers navigations.

        Args:
            page: Playwright page
        """
        super().set_page(page)

        self.last_status = None

        # Submitting a search form navigates without going through goto(), and
        # that is exactly the navigation a board blocks. Listening covers both.
        if page is not None and not getattr(page, "_job_agent_watched", False):
            try:
                page.on("response", self._note_document_response)
                page._job_agent_watched = True
            except Exception as e:
                logger.debug(f"Could not watch responses on the page: {e}")

    def _note_document_response(self, response: Any) -> None:
        """
        Record the status of a top-level page load.

        Args:
            response: The Playwright response
        """
        try:
            request = response.request

            if request.resource_type == "document" and request.is_navigation_request():
                self.last_status = response.status
        except Exception:
            pass

    def _note_response(self, response: Any) -> None:
        """
        Remember how the site answered a navigation we drove ourselves.

        Args:
            response: The Playwright response, or None
        """
        try:
            if response is not None:
                self.last_status = response.status
        except Exception:
            pass

    async def _collect_links_by_url_shape(self) -> List[str]:
        """
        Find postings by the shape of the page's links rather than its markup.

        A job board repeats one URL shape for its postings — `/remote-jobs/…`,
        `/careers/…` — while its navigation does not. Grouping every link by
        the path it hangs off and taking the largest job-ish group finds the
        listings on a site whose class names this connector has never seen.

        Returns:
            Absolute job URLs, in the order they appear on the page
        """
        try:
            hrefs = await self._page.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.getAttribute('href'))"
            )
        except Exception as e:
            logger.debug(f"Could not read links from the page: {e}")
            return []

        page_url = self._page.url
        page_host = urlparse(page_url).netloc

        groups: Dict[str, List[str]] = {}

        for href in hrefs:
            if not href or href.startswith("#"):
                continue

            absolute = urljoin(page_url, href)
            parsed = urlparse(absolute)

            # An off-site link is an advert or a social profile, not a posting.
            if parsed.netloc != page_host or not parsed.path:
                continue

            parent, _, last = parsed.path.rstrip("/").rpartition("/")

            # A bare section page ("/jobs") is the index, not an entry in it.
            if not parent or not last:
                continue

            if not any(hint in parent.lower() for hint in JOB_PATH_HINTS):
                continue

            groups.setdefault(parent, []).append(absolute)

        if not groups:
            return []

        parent, links = max(groups.items(), key=lambda item: len(item[1]))

        if len(links) < MIN_LINKS_FOR_A_BOARD:
            return []

        # Postings are plain paths; the tracking-tagged links alongside them are
        # promotions for the board itself.
        clean = [link for link in links if "?" not in link]

        chosen = clean or links

        deduped = list(dict.fromkeys(chosen))

        logger.info(
            f"Found {len(deduped)} jobs on {self.platform_name} by URL shape ({parent}/)"
        )

        return deduped
    
    async def read_job_details(self, job_url: str) -> JobPosting:
        """
        Read job details from a job listing page.
        
        Extracts: title, company, location, description, requirements,
        salary, posted_at, apply_method
        
        Uses JSON-LD structured data if available, falls back to
        CSS selectors for parsing.
        
        Args:
            job_url: URL to job posting
        
        Returns:
            JobPosting with parsed details
        """
        logger.info(f"Reading job details from {job_url}")
        
        if not hasattr(self, '_page') or not self._page:
            logger.error("No active page context")
            return JobPosting(
                platform=self.platform_name,
                external_id="unknown",
                title="Unable to read",
                company="Unknown",
                location="Not specified",
                description="Page not accessible",
            )
        
        try:
            # Navigate to the posting if we're not already on it
            if job_url and self._page.url != job_url:
                await self._page.goto(job_url)

            # Try to get JSON-LD structured data first (most reliable)
            job_posting = await self._extract_json_ld()
            if job_posting:
                job_posting.platform = self.platform_name
                job_posting.apply_url = job_url
                return job_posting
            
            # Fall back to CSS selector parsing
            job_posting = await self._extract_from_html()
            if job_posting:
                job_posting.platform = self.platform_name
                job_posting.apply_url = job_url
                return job_posting
            
            # Return minimal posting if extraction failed
            logger.warning(f"Could not parse job details from {job_url}")
            return JobPosting(
                platform=self.platform_name,
                external_id="unknown",
                title="Job Posting",
                company="Unknown",
                location="Not specified",
                description="Could not parse job details",
                apply_url=job_url,
            )
        
        except Exception as e:
            logger.error(
                f"Error reading job details from {job_url}: "
                f"{type(e).__name__}: {e}",
                exc_info=True,
            )
            return JobPosting(
                platform=self.platform_name,
                external_id="unknown",
                title="Error",
                company="Unknown",
                location="Not specified",
                description=f"Error reading job: {type(e).__name__}: {e}",
                apply_url=job_url,
            )
    
    async def _extract_json_ld(self) -> Optional[JobPosting]:
        """
        Extract job details from JSON-LD structured data.
        
        JSON-LD is the most reliable way to parse job postings.
        Format: <script type="application/ld+json">
        
        Returns:
            JobPosting if JSON-LD found, None otherwise
        """
        try:
            import json

            # A page may carry several JSON-LD blocks (breadcrumbs, org info,
            # the posting); scan them all for the JobPosting.
            blocks = await self._page.locator(
                'script[type="application/ld+json"]'
            ).all_text_contents()

            job_data = None
            for block in blocks:
                if not block or not block.strip():
                    continue

                try:
                    data = json.loads(block)
                except json.JSONDecodeError:
                    continue

                job_data = self._find_job_posting(data)
                if job_data:
                    break

            if not job_data:
                return None

            # Extract fields from schema.org JobPosting
            return JobPosting(
                platform=self.platform_name,
                external_id=self._scalar(job_data.get("identifier"), "value") or "unknown",
                title=self._scalar(job_data.get("title")) or "Job",
                company=self._scalar(job_data.get("hiringOrganization"), "name") or "Unknown",
                location=self._extract_location(job_data.get("jobLocation", {})),
                job_type=self._normalize_job_type(job_data.get("employmentType", "full_time")),
                description=_as_plain_text(self._scalar(job_data.get("description"))),
                requirements=self._scalar(job_data.get("applicantLocationRequirements")) or "",
                salary=self._extract_salary(job_data.get("baseSalary", {})),
                posted_at=self._scalar(job_data.get("datePosted")) or "",
                apply_method=self._detect_apply_method(job_data),
                apply_url="",  # Set by caller
            )

        except Exception as e:
            logger.debug(f"JSON-LD extraction failed: {e}")
            return None

    @staticmethod
    def _find_job_posting(data: Any) -> Optional[Dict[str, Any]]:
        """
        Locate the JobPosting object inside a JSON-LD document.

        Real pages nest it in several shapes: the bare object, a list of
        objects, an `@graph` array, or an `ItemList` of postings.

        Args:
            data: Parsed JSON-LD

        Returns:
            The JobPosting dict, or None
        """
        if isinstance(data, list):
            for item in data:
                found = GenericATSConnector._find_job_posting(item)
                if found:
                    return found
            return None

        if not isinstance(data, dict):
            return None

        # @type may itself be a list, e.g. ["JobPosting", "Thing"]
        types = data.get("@type", "")
        types = types if isinstance(types, list) else [types]

        if "JobPosting" in types:
            return data

        for key in ("@graph", "itemListElement"):
            nested = data.get(key)
            if nested:
                found = GenericATSConnector._find_job_posting(nested)
                if found:
                    return found

        # ItemList entries wrap the real object in `item`
        if "item" in data:
            return GenericATSConnector._find_job_posting(data["item"])

        return None

    @staticmethod
    def _plain_text(value: Optional[str]) -> str:
        """Exposed for tests; see `_as_plain_text`."""
        return _as_plain_text(value)

    @staticmethod
    def _scalar(value: Any, key: Optional[str] = None) -> Optional[str]:
        """
        Coerce a schema.org field to a string.

        The same field is a plain string on one site and an object on another
        (`identifier: "R-1234"` vs `identifier: {"@type": "PropertyValue",
        "value": "R-1234"}`), and lists show up for repeated values. Assuming
        one shape is why the previous parser raised
        `'str' object has no attribute 'get'` on valid postings.

        Args:
            value: Raw field value
            key: Property to read when the value is an object

        Returns:
            String value, or None if there isn't one
        """
        if value is None:
            return None

        if isinstance(value, str):
            return value.strip() or None

        if isinstance(value, (int, float)):
            return str(value)

        if isinstance(value, dict):
            if key and key in value:
                return GenericATSConnector._scalar(value[key])
            # Common fallbacks for object-wrapped scalars
            for fallback in ("value", "name", "@value"):
                if fallback in value:
                    return GenericATSConnector._scalar(value[fallback])
            return None

        if isinstance(value, list):
            for item in value:
                found = GenericATSConnector._scalar(item, key)
                if found:
                    return found

        return None


    async def _extract_from_html(self) -> Optional[JobPosting]:
        """
        Extract job details from HTML using common CSS patterns.
        
        Fallback method when JSON-LD is not available.
        
        Returns:
            JobPosting if extraction successful, None otherwise
        """
        try:
            title = await self._text_from(["h1", "h2", "[class*='title']"])
            company = await self._company_name()
            location = await self._text_from(["[class*='location']", "[class*='place']"])
            description = await self._text_from(
                ["[class*='description']", "main", "article"], min_length=50
            )

            # A posting with no readable title isn't a posting. The company is
            # a different matter: plenty of boards name it once in the page
            # furniture and never in a class the selectors above would find,
            # and throwing the job away over that loses the whole board.
            if not title:
                return None

            return JobPosting(
                platform=self.platform_name,
                # The URL is the only identifier guaranteed to differ between
                # two postings; deriving it from the text collapses every job
                # on a board whose titles didn't parse into a single duplicate.
                external_id=self._external_id_from_url()
                or f"{company or 'unknown'}-{title}",
                title=title.strip(),
                company=(company or "Unknown").strip(),
                location=location.strip() if location else "Not specified",
                description=description.strip() if description else "",
                apply_url="",
            )

        except Exception as e:
            logger.debug(f"HTML extraction failed: {e}")
            return None

    def _external_id_from_url(self) -> Optional[str]:
        """
        A stable per-posting identifier taken from its URL.

        Returns:
            The URL's last path segment, or None if there isn't one
        """
        try:
            path = urlparse(self._page.url).path.rstrip("/")
        except Exception:
            return None

        segment = path.rpartition("/")[2]

        return segment or None

    async def _company_name(self) -> Optional[str]:
        """
        Work out whose job this is.

        Tries the markup first, then the page furniture that almost every
        board fills in: the Open Graph site name, and the "<role> at <company>"
        pattern that job pages put in their title tag.

        Returns:
            The company name, or None if nothing yields one
        """
        company = await self._text_from(
            ["[class*='company']", "[class*='employer']"]
        )

        if company:
            return company

        for selector in (
            "meta[property='og:site_name']",
            "meta[name='application-name']",
        ):
            try:
                element = self._page.locator(selector)

                if await element.count():
                    content = await element.first.get_attribute(
                        "content", timeout=EXTRACT_TIMEOUT_MS
                    )

                    if content and content.strip():
                        return content.strip()
            except Exception:
                continue

        try:
            page_title = await self._page.title()
        except Exception:
            return None

        if page_title and " at " in page_title:
            # "Job Application for Vice President, Data & Insights at GitLab"
            return page_title.rpartition(" at ")[2].strip() or None

        return None

    async def _text_from(
        self,
        selectors: List[str],
        min_length: int = 1,
    ) -> Optional[str]:
        """
        Return text from the first selector that matches, or None.

        Existence is checked with count(), which resolves immediately, before
        any text is fetched. text_content() auto-waits instead — at
        Playwright's 30s default, ten speculative probes against a page
        without the expected markup stalled a run for ~35 seconds per posting.

        Args:
            selectors: CSS selectors to try in order
            min_length: Ignore matches shorter than this

        Returns:
            Stripped text, or None if nothing matched
        """
        for selector in selectors:
            try:
                locator = self._page.locator(selector)

                if await locator.count() == 0:
                    continue  # nothing to wait for

                text = await locator.first.text_content(timeout=EXTRACT_TIMEOUT_MS)

                if text and len(text.strip()) >= min_length:
                    return text
            except Exception as e:
                logger.debug(f"Selector {selector} yielded nothing: {e}")
                continue

        return None


    def _extract_location(self, location_data: Dict[str, Any]) -> str:
        """Extract location from schema.org jobLocation object."""
        if isinstance(location_data, dict):
            if "@type" in location_data and location_data["@type"] == "Place":
                address = location_data.get("address", {})
                if isinstance(address, dict):
                    parts = [
                        address.get("addressLocality"),
                        address.get("addressRegion"),
                        address.get("addressCountry"),
                    ]
                    return ", ".join(p for p in parts if p)
        return "Not specified"
    
    def _normalize_job_type(self, job_type: str) -> str:
        """Normalize job type to standard values."""
        if isinstance(job_type, str):
            job_type = job_type.lower()
            if "full" in job_type:
                return "full_time"
            elif "part" in job_type:
                return "part_time"
            elif "contract" in job_type:
                return "contract"
            elif "temp" in job_type:
                return "temporary"
        return "full_time"
    
    def _extract_salary(self, salary_data: Dict[str, Any]) -> Optional[str]:
        """
        Extract salary from a schema.org baseSalary object.

        Spec-compliant markup nests the numbers one level down in a
        QuantitativeValue:

            "baseSalary": {"@type": "MonetaryAmount", "currency": "USD",
                           "value": {"@type": "QuantitativeValue",
                                     "minValue": 150000, "maxValue": 170000}}

        Reading minValue/maxValue only at the top level (as this did) returns
        None for those postings, which silently disables the salary hard
        filter. Both shapes are handled, plus a scalar `value`.

        Args:
            salary_data: The baseSalary object

        Returns:
            Human-readable salary string, or None
        """
        try:
            if not isinstance(salary_data, dict):
                return None

            currency = (
                salary_data.get("currency")
                or salary_data.get("salaryCurrency")
                or "USD"
            )

            # Prefer the nested QuantitativeValue when present
            value = salary_data.get("value")
            source = value if isinstance(value, dict) else salary_data

            min_sal = source.get("minValue")
            max_sal = source.get("maxValue")

            # A scalar value means a single figure, not a range
            if min_sal is None and max_sal is None and isinstance(value, (int, float)):
                return f"{currency} {value:,}"

            if min_sal is None and max_sal is None:
                single = source.get("value")
                if isinstance(single, (int, float)):
                    return f"{currency} {single:,}"

            if min_sal and max_sal:
                return f"{currency} {min_sal:,} - {max_sal:,}"
            elif min_sal:
                return f"{currency} {min_sal:,}+"
            elif max_sal:
                return f"{currency} up to {max_sal:,}"
        except Exception as e:
            logger.debug(f"Salary extraction failed: {e}")
        
        return None
    
    def _detect_apply_method(self, job_data: Dict[str, Any]) -> str:
        """Detect if job application is via web form or email."""
        # Check for applicationContact email
        contact = job_data.get("applicationContact", {})
        if isinstance(contact, dict) and contact.get("contactType") == "Customer Service":
            if "email" in contact:
                return "email"
        
        return "web_form"
    
    async def begin_application(self, job: JobPosting) -> ApplicationSession:
        """
        Begin an application for a job.

        Navigates to the apply URL, then follows an "Apply" control if the page
        is still the job posting rather than the form itself.

        Args:
            job: JobPosting to apply for

        Returns:
            ApplicationSession positioned on the application form
        """
        logger.info(f"Beginning application for {job.title}")

        if not self._page:
            raise RuntimeError(
                f"No page attached to {self.platform_name} connector — "
                f"call set_page() before begin_application()"
            )

        if job.apply_url and self._page.url != job.apply_url:
            await self._page.goto(job.apply_url)
            await self._settle()

        # If there's no form here yet, look for the apply control
        if not await self._has_form_fields():
            if await self._click_first(self.selectors["apply_button"]):
                await self._settle()
                logger.info(f"Followed the apply control to {self._page.url}")

        return ApplicationSession(
            job=job,
            platform_account_id=0,  # Set by the caller
            form_url=self._page.url,
        )

    async def _has_form_fields(self) -> bool:
        """True if the current page shows any fillable input."""
        try:
            return await self._page.locator(
                "input:not([type=hidden]):not([type=submit]), select, textarea"
            ).count() > 0
        except Exception:
            return False

    async def fill_application(
        self,
        session: ApplicationSession,
        candidate_profile: Any,
        application_package: Dict[str, Any],
    ) -> ApplicationSession:
        """
        Fill the application form, deferring anything not confidently known.

        Sensitive questions (demographics, disability, veteran status,
        compensation history) are never answered here — they are recorded for
        the user with the reason, and left blank on the page.

        Args:
            session: ApplicationSession positioned on the form
            candidate_profile: CandidateProfile supplying values
            application_package: {"resume": path, "cover_letter": path,
                "db_session": Session, "screenshots_dir": Path}

        Returns:
            The session, with filled_fields, deferred_fields and screenshot_path

        Raises:
            RuntimeError: If no page is attached
        """
        if not self._page:
            raise RuntimeError(f"No page attached to {self.platform_name} connector")

        from job_agent.services.application_filler import ApplicationFiller

        package = dict(application_package or {})

        filler = ApplicationFiller(
            db_session=package.get("db_session"),
            screenshots_dir=package.get("screenshots_dir"),
        )

        # The whole application, not just the screen in front of it. Most
        # boards outside Greenhouse are wizards, and filling only step one
        # reported an application as ready when four screens had never been
        # read.
        outcome = await filler.fill_application(
            self._page,
            candidate_profile,
            documents={
                key: package[key]
                for key in ("resume", "cover_letter")
                if package.get(key)
            },
            # Supplied so open written questions arrive with a draft answer
            # rather than an empty box.
            job=package.get("job"),
            master_text=package.get("master_text", ""),
        )

        session.filled_fields = outcome.filled_fields
        session.deferred_fields = outcome.deferred_fields
        session.screenshot_path = outcome.screenshot_path
        session.form_state = {
            "errors": outcome.errors,
            "needs_user_input": outcome.needs_user_input,
            "required_unanswered": outcome.required_deferred,
            # How the form was walked: the steps read, what was pressed to
            # advance, and where it stopped. The user needs this to know
            # whether "filled" means one screen or five.
            "walk": outcome.walk,
            "form_url": self._page.url,
        }

        return session

    async def submit_application(self, session: ApplicationSession) -> SubmissionResult:
        """
        Submit the filled application form.

        Callers must clear SubmissionGate first — this method performs the
        submission, it does not decide whether it is allowed. It clicks the
        submit control, waits, and then verifies the result rather than
        assuming the click worked.

        Args:
            session: ApplicationSession on the filled form

        Returns:
            SubmissionResult; `success` is True only when confirmation was found

        Raises:
            RuntimeError: If no page is attached
        """
        if not self._page:
            raise RuntimeError(f"No page attached to {self.platform_name} connector")

        from job_agent.services.submitter import ApplicationSubmitter

        screenshots_dir = (session.form_state or {}).get("screenshots_dir")

        outcome = await ApplicationSubmitter(screenshots_dir=screenshots_dir).submit(self._page)

        session.form_state = {**(session.form_state or {}), "submission": outcome.to_dict()}

        if not outcome.submitted:
            return SubmissionResult(
                success=False,
                error_message=outcome.error_message or "The form was not submitted",
            )

        if outcome.validation_errors:
            return SubmissionResult(
                success=False,
                confirmation_url=outcome.confirmation_url,
                error_message=(
                    f"The form rejected the submission: "
                    f"{'; '.join(outcome.validation_errors[:2])}"
                ),
            )

        return SubmissionResult(
            success=outcome.confirmed,
            confirmation_ref=outcome.confirmation_ref,
            confirmation_url=outcome.confirmation_url,
            confirmation_message=outcome.confirmation_message,
            error_message=(
                None if outcome.confirmed
                else "Submitted, but no confirmation could be found on the page"
            ),
        )


def _as_plain_text(value: Optional[str]) -> str:
    """
    Turn a schema.org description into readable text.

    JSON-LD carries the description as an HTML fragment, and it was being
    stored verbatim: "<div><p>Career paths start between $14.50…". That HTML
    then reached three places it had no business being — the fit analyser,
    which scored `<b>` and `<br>` as content words; the tailoring prompt, where
    it wasted a small model's context on markup; and the posting the user reads
    in the tray.

    Block-level tags become line breaks so the structure of a posting — its
    headings, its bullet list of requirements — survives as plain text, which
    is what the requirement extractor needs to find them.

    Args:
        value: A description, possibly HTML

    Returns:
        Plain text, empty when there was nothing
    """
    if not value:
        return ""

    text = value

    if "<" not in text:
        return text.strip()

    # Block boundaries first, so paragraphs and list items do not run together
    # into one unreadable line.
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", text)
    text = re.sub(r"(?i)</\s*(p|div|li|tr|h[1-6]|ul|ol|table|section)\s*>", "\n", text)
    text = re.sub(r"(?i)<\s*li[^>]*>", "• ", text)

    # Everything else is presentational.
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)

    # Collapse the whitespace the tags left behind, keeping paragraph breaks.
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()
