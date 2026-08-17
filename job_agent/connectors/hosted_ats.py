"""
Hosted ATS Connector Base (Phase 7).

Greenhouse, Lever, Ashby, Workable and friends all follow the same shape: a
per-company board at a predictable URL, job pages at a predictable path, and an
application form built from standard field names. The differences are the URL
patterns and a handful of selectors — not the logic — so they subclass this
rather than reimplementing search, parsing, filling and submission.

Everything inherited from GenericATSConnector still applies: JSON-LD parsing
with an HTML fallback, the field classifier that refuses to answer demographic
and compensation questions, and submission that verifies rather than assumes.

**On capability declarations.** `PlatformCapabilities` is a promise to the rest
of the system, and the GUI hides actions a connector doesn't claim. Each
subclass declares only what its code actually implements, and
`verified_against` records how far that claim has been tested — see
`VerificationLevel`.
"""

import logging
import re
from enum import Enum
from typing import Any, Dict, List, Optional, Pattern

from job_agent.connectors.base import JobPosting, PlatformCapabilities
from job_agent.connectors.generic_ats import GenericATSConnector

logger = logging.getLogger(__name__)


class VerificationLevel(str, Enum):
    """
    How far a connector's behaviour has actually been confirmed.

    Recorded so nobody has to guess how much a capability claim is worth. A
    connector verified only against fixtures may still be wrong about the live
    site — the Phase 6 clean-submissions gate is what protects the user there,
    by forcing the first submissions through human review whatever this says.
    """

    FIXTURE = "fixture"
    """Tested against local HTML replicating the platform's published markup."""

    LIVE_READ = "live_read"
    """Search and job reading confirmed against the real site."""

    LIVE_SUBMIT = "live_submit"
    """A real application was submitted successfully through this connector."""


class HostedATSConnector(GenericATSConnector):
    """
    Base for connectors targeting a hosted ATS with a known URL shape.

    Subclasses set the class attributes below; the behaviour comes from here
    and from GenericATSConnector.
    """

    PLATFORM: str = "hosted_ats"
    """Registry name."""

    BOARD_URL_TEMPLATE: Optional[str] = None
    """Board URL with a {board} placeholder, e.g. 'https://job-boards.greenhouse.io/{board}'."""

    SEARCH_QUERY_PARAM: Optional[str] = None
    """Query parameter the board's own search uses, when it has one."""

    JOB_LINK_PATTERN: Optional[Pattern] = None
    """Matches hrefs that are job postings rather than navigation."""

    APPLY_PATH_SUFFIX: Optional[str] = None
    """Appended to a job URL to reach its application form, when needed."""

    EXTRA_FIELD_ALIASES: Dict[str, str] = {}
    """Platform field names mapped to profile keys the classifier understands."""

    PAGE_QUERY_PARAM: Optional[str] = None
    """Query parameter carrying the page number, when the board paginates by URL."""

    FIRST_PAGE_INDEX: int = 1
    """Whether the platform's first page is page 1 or page 0."""

    NEXT_PAGE_SELECTORS: List[str] = [
        "a[rel='next']",
        "a[aria-label*='next' i]:not([aria-disabled='true'])",
        "button[aria-label*='next' i]:not([disabled])",
        "a.next:not(.disabled)",
        "[data-testid*='pagination-next' i]:not([disabled])",
    ]
    """Controls that advance to the next page of results."""

    LOAD_MORE_SELECTORS: List[str] = [
        "button:has-text('Load more')",
        "button:has-text('Show more')",
        "button:has-text('See more jobs')",
        "[data-testid*='load-more' i]",
    ]
    """Controls that append more results to the same page."""

    MAX_PAGES: int = 5
    """
    Pages to walk before stopping.

    A cap rather than "all pages": an unbounded walk on a board with thousands
    of postings would run for hours and read far more than any daily limit
    permits. Five pages is roughly 50–100 postings on most boards, which
    comfortably exceeds the default daily_search_limit.
    """

    CAPABILITIES: PlatformCapabilities = PlatformCapabilities()
    """What this connector actually implements."""

    VERIFIED_AGAINST: VerificationLevel = VerificationLevel.FIXTURE
    """How far the behaviour has been confirmed."""

    def __init__(
        self,
        platform_name: Optional[str] = None,
        search_url: Optional[str] = None,
        board: Optional[str] = None,
    ):
        """
        Initialize the connector.

        Args:
            platform_name: Registry name (defaults to PLATFORM)
            search_url: Explicit search URL, overriding the board template
            board: Company board token, e.g. "acme" in
                job-boards.greenhouse.io/acme
        """
        super().__init__(platform_name or self.PLATFORM)

        # Replace the generic capabilities with this platform's declaration
        self.capabilities = self.CAPABILITIES
        self.verified_against = self.VERIFIED_AGAINST
        self.board = board

        if search_url:
            self.search_url = search_url
        elif board and self.BOARD_URL_TEMPLATE:
            self.search_url = self.board_url(board)

    # ------------------------------------------------------------------
    # URLs
    # ------------------------------------------------------------------

    @classmethod
    def board_url(cls, board: str) -> str:
        """
        Build the board URL for a company.

        Args:
            board: Company board token

        Returns:
            Board URL

        Raises:
            ValueError: If this platform has no board template
        """
        if not cls.BOARD_URL_TEMPLATE:
            raise ValueError(f"{cls.__name__} has no board URL template")

        return cls.BOARD_URL_TEMPLATE.format(board=board.strip("/"))

    @classmethod
    def extract_board(cls, url: str) -> Optional[str]:
        """
        Recover the board token from a URL on this platform.

        Args:
            url: Any URL on the platform

        Returns:
            The board token, or None if the URL doesn't match
        """
        if not cls.BOARD_URL_TEMPLATE:
            return None

        pattern = re.escape(cls.BOARD_URL_TEMPLATE).replace(
            re.escape("{board}"), r"([^/?#]+)"
        )
        match = re.match(pattern, url)

        return match.group(1) if match else None

    def _build_search_url(self, query: str, location: str) -> str:
        """
        Resolve the search URL, appending the board's query parameter.

        Boards mostly filter client-side or via one parameter, so a templated
        `{query}` (handled by the parent) takes precedence and this only adds
        the platform's own parameter when there is one.

        Args:
            query: Search text
            location: Location text

        Returns:
            URL to open
        """
        url = super()._build_search_url(query, location)

        if not query or not self.SEARCH_QUERY_PARAM or self._is_template(self.search_url or ""):
            return url

        from urllib.parse import quote_plus

        separator = "&" if "?" in url else "?"

        return f"{url}{separator}{self.SEARCH_QUERY_PARAM}={quote_plus(query)}"

    # ------------------------------------------------------------------
    # Job links
    # ------------------------------------------------------------------

    async def collect_job_links(self) -> List[str]:
        """
        Collect job posting URLs across the board's pages.

        Uses the platform's link pattern, which is far more reliable than the
        generic connector's guess at listing containers: on a hosted board
        every posting URL has the same recognizable shape.

        Walks up to MAX_PAGES, trying in order: the platform's page query
        parameter, a "next page" control, then a "load more" button. Stops
        early when a page yields no new links, which also covers a pagination
        control that silently does nothing.

        Returns:
            Job URLs, in page order and de-duplicated across pages
        """
        if not self.JOB_LINK_PATTERN:
            return await super().collect_job_links()

        if not self._page:
            logger.warning(f"{self.platform_name}: no page attached")
            return []

        links: List[str] = []
        seen = set()

        for page_number in range(self.MAX_PAGES):
            found = await self._links_on_page()
            new = [href for href in found if href not in seen]

            for href in new:
                seen.add(href)
                links.append(href)

            if page_number == 0:
                logger.info(f"{self.platform_name}: {len(new)} job link(s) on page 1")

            # A page that adds nothing means we've reached the end, or the
            # control we clicked didn't actually advance
            if page_number > 0 and not new:
                logger.info(
                    f"{self.platform_name}: page {page_number + 1} added no new links; "
                    f"stopping"
                )
                break

            if page_number == self.MAX_PAGES - 1:
                break

            if not await self._go_to_next_page(page_number):
                break

            logger.info(
                f"{self.platform_name}: advanced to page {page_number + 2} "
                f"({len(links)} link(s) so far)"
            )

        logger.info(f"{self.platform_name}: found {len(links)} job link(s) in total")

        return links

    async def _links_on_page(self) -> List[str]:
        """
        Extract matching job links from the current page.

        Returns:
            Job URLs in document order
        """
        try:
            hrefs = await self._page.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.href)"
            )
        except Exception as e:
            logger.warning(f"{self.platform_name}: could not read links: {e}")
            return []

        return [
            href for href in hrefs
            if href and self.JOB_LINK_PATTERN.search(href)
        ]

    async def _go_to_next_page(self, current_index: int) -> bool:
        """
        Advance to the next page of results.

        Args:
            current_index: Zero-based index of the page just read

        Returns:
            True if navigation happened
        """
        # 1. A page parameter is the most reliable, when the platform has one
        if self.PAGE_QUERY_PARAM and self.search_url:
            next_page = self.FIRST_PAGE_INDEX + current_index + 1
            url = self._url_with_page(self._page.url, next_page)

            try:
                await self._page.goto(url)
                await self._settle()
                return True
            except Exception as e:
                logger.debug(f"{self.platform_name}: page URL {url} failed: {e}")
                return False

        # 2. A next-page control
        if await self._click_first(self.NEXT_PAGE_SELECTORS):
            await self._settle()
            return True

        # 3. A load-more button, which appends rather than navigating
        if await self._click_first(self.LOAD_MORE_SELECTORS):
            await self._settle()
            return True

        return False

    def _url_with_page(self, url: str, page: int) -> str:
        """
        Set the page parameter on a URL, replacing any existing value.

        Args:
            url: Current URL
            page: Page number to request

        Returns:
            URL for that page
        """
        from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

        parsed = urlparse(url)
        params = dict(parse_qsl(parsed.query))
        params[self.PAGE_QUERY_PARAM] = str(page)

        return urlunparse(parsed._replace(query=urlencode(params)))

    def apply_url_for(self, job_url: str) -> str:
        """
        Build the application URL for a job.

        Args:
            job_url: The job posting URL

        Returns:
            URL of the application form
        """
        if not self.APPLY_PATH_SUFFIX:
            return job_url

        base = job_url.split("?")[0].rstrip("/")

        if base.endswith(self.APPLY_PATH_SUFFIX.rstrip("/")):
            return job_url

        return f"{base}{self.APPLY_PATH_SUFFIX}"

    async def begin_application(self, job: JobPosting) -> Any:
        """
        Open the application form for a job.

        Navigates straight to the platform's apply path when it has one,
        instead of hunting for an apply button.

        Args:
            job: The posting to apply for

        Returns:
            ApplicationSession positioned on the form
        """
        if self.APPLY_PATH_SUFFIX and job.apply_url:
            job = JobPosting(**{**job.__dict__, "apply_url": self.apply_url_for(job.apply_url)})

        return await super().begin_application(job)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def describe(self) -> dict:
        """
        Describe what this connector can do and how far it's been verified.

        Returns:
            Capability and verification detail for the dashboard
        """
        capabilities = self.capabilities

        return {
            "platform": self.platform_name,
            "board": self.board,
            "search_url": self.search_url,
            "verified_against": self.verified_against.value,
            "capabilities": {
                "search": capabilities.can_search,
                "filter": capabilities.can_filter,
                "read_details": capabilities.can_read_details,
                "start_application": capabilities.can_start_application,
                "fill_standard_fields": capabilities.can_fill_standard_fields,
                "upload_documents": capabilities.can_upload_documents,
                "process_custom_questions": capabilities.can_process_custom_questions,
                "submit_automatically": capabilities.can_submit_automatically,
            },
            "requires_manual_signin": capabilities.requires_manual_signin,
            "requires_manual_review_first_n": capabilities.requires_manual_review_first_n,
            "tos_risk_note": capabilities.tos_risk_note,
        }
