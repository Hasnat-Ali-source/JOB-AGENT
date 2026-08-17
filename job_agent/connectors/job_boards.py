"""
Consumer Job Board Connectors (Phase 7f–7j).

LinkedIn, Indeed, Glassdoor, ZipRecruiter, Wellfound, Dice and JobStreet.

These differ from the hosted ATS connectors in a way that matters. Applying
through Greenhouse means filling in a form the employer published for
applicants. These are third-party platforms whose terms of service restrict
automated access, and whose anti-automation systems act on the *user's own
account* — a suspended LinkedIn account costs far more than the applications it
saved.

So every connector here is **read-only by default**:

- `can_submit_automatically=False` on all of them, with a risk note explaining why
- `can_fill_standard_fields=False` — the agent opens the application and hands
  the browser to the user
- Search and reading are supported, which is where most of the value is anyway:
  finding jobs and pulling their details into the pipeline, then applying
  through the employer's own ATS wherever the posting links to one

Turning submission on is a deliberate code change to a capability declaration,
not a settings toggle — the Phase 6 gate consults `can_submit_automatically`,
so leaving it false makes unattended submission unreachable by construction.

The agent also always works through the user's own signed-in browser session
(Phase 1), at human pace, within the configured daily limits. It never handles
credentials.
"""

import logging
import re
from typing import List
from urllib.parse import quote_plus

from job_agent.connectors.base import PlatformCapabilities
from job_agent.connectors.hosted_ats import HostedATSConnector, VerificationLevel

logger = logging.getLogger(__name__)

# Applied to every connector in this module unless overridden
READ_ONLY_CAPABILITIES = dict(
    can_search=True,
    can_filter=True,
    can_read_details=True,
    can_start_application=True,  # Opens the flow; the user takes it from there
    can_fill_standard_fields=False,
    can_upload_documents=False,
    can_process_custom_questions=False,
    can_submit_automatically=False,
    requires_manual_signin=True,
    requires_manual_review_first_n=5,
)


class ConsumerBoardConnector(HostedATSConnector):
    """
    Base for third-party job boards.

    Adds a search URL built from query parameters, since these boards search
    server-side rather than filtering a per-company list.
    """

    SEARCH_URL_TEMPLATE: str = ""
    """Full search URL with {query} and {location} placeholders."""

    def __init__(self, platform_name=None, search_url=None, board=None):
        super().__init__(platform_name, search_url, board)

        if not self.search_url and self.SEARCH_URL_TEMPLATE:
            self.search_url = self.SEARCH_URL_TEMPLATE

    def _build_search_url(self, query: str, location: str) -> str:
        """
        Build the board's search URL.

        Args:
            query: Search text
            location: Location text

        Returns:
            Search URL
        """
        if self.search_url and self._is_template(self.search_url):
            return (
                self.search_url
                .replace("{query}", quote_plus(query))
                .replace("{location}", quote_plus(location))
            )

        return super()._build_search_url(query, location)


# ============================================================================
# 7f. LinkedIn
# ============================================================================

class LinkedInConnector(ConsumerBoardConnector):
    """
    LinkedIn Jobs — search and read only.

    LinkedIn's User Agreement prohibits automated access, and its automated
    detection acts against the account itself. This connector therefore reads
    postings from the user's own signed-in session and stops there: it will
    open an application flow so the user can complete it, but it does not fill
    or submit, and no setting makes it do so.
    """

    PLATFORM = "linkedin"
    SEARCH_URL_TEMPLATE = (
        "https://www.linkedin.com/jobs/search/?keywords={query}&location={location}"
    )
    JOB_LINK_PATTERN = re.compile(r"linkedin\.com/jobs/view/\d+", re.I)
    PAGE_QUERY_PARAM = "start"  # LinkedIn pages by result offset, 25 at a time
    FIRST_PAGE_INDEX = 0

    CAPABILITIES = PlatformCapabilities(
        **READ_ONLY_CAPABILITIES,
        tos_risk_note=(
            "LinkedIn's User Agreement prohibits automated access, and enforcement "
            "targets your account, not the tool. This connector searches and reads "
            "postings through your own signed-in session and never submits. Where a "
            "posting links to the employer's own ATS, apply there instead."
        ),
    )
    VERIFIED_AGAINST = VerificationLevel.FIXTURE


# ============================================================================
# 7g. Indeed
# ============================================================================

class IndeedConnector(ConsumerBoardConnector):
    """
    Indeed — search and read only.

    Indeed runs aggressive bot detection and its terms restrict scraping.
    Read-only, through the user's own session, at human pace.
    """

    PLATFORM = "indeed"
    SEARCH_URL_TEMPLATE = "https://www.indeed.com/jobs?q={query}&l={location}"
    JOB_LINK_PATTERN = re.compile(r"indeed\.com/(viewjob\?jk=|rc/clk\?jk=)", re.I)
    PAGE_QUERY_PARAM = "start"  # Indeed pages by result offset, 10 at a time
    FIRST_PAGE_INDEX = 0

    CAPABILITIES = PlatformCapabilities(
        **READ_ONLY_CAPABILITIES,
        tos_risk_note=(
            "Indeed's terms restrict automated access and it runs active bot "
            "detection; challenges may interrupt a run. This connector searches and "
            "reads postings only, and never submits."
        ),
    )
    VERIFIED_AGAINST = VerificationLevel.FIXTURE


# ============================================================================
# 7h–7j. Remaining boards
# ============================================================================

class GlassdoorConnector(ConsumerBoardConnector):
    """Glassdoor — search and read only."""

    PLATFORM = "glassdoor"
    SEARCH_URL_TEMPLATE = (
        "https://www.glassdoor.com/Job/jobs.htm?sc.keyword={query}&locT=&locId="
    )
    JOB_LINK_PATTERN = re.compile(r"glassdoor\.[a-z.]+/job-listing/", re.I)

    CAPABILITIES = PlatformCapabilities(
        **READ_ONLY_CAPABILITIES,
        tos_risk_note=(
            "Glassdoor's terms restrict automated access and it gates content behind "
            "sign-in walls. Search and read only; never submits."
        ),
    )
    VERIFIED_AGAINST = VerificationLevel.FIXTURE


class ZipRecruiterConnector(ConsumerBoardConnector):
    """ZipRecruiter — search and read only."""

    PLATFORM = "ziprecruiter"
    SEARCH_URL_TEMPLATE = (
        "https://www.ziprecruiter.com/jobs-search?search={query}&location={location}"
    )
    PAGE_QUERY_PARAM = "page"
    JOB_LINK_PATTERN = re.compile(r"ziprecruiter\.com/(c|jobs)/", re.I)

    CAPABILITIES = PlatformCapabilities(
        **READ_ONLY_CAPABILITIES,
        tos_risk_note=(
            "ZipRecruiter's terms restrict automated access. Search and read only; "
            "never submits."
        ),
    )
    VERIFIED_AGAINST = VerificationLevel.FIXTURE


class WellfoundConnector(ConsumerBoardConnector):
    """Wellfound (formerly AngelList Talent) — search and read only."""

    PLATFORM = "wellfound"
    SEARCH_URL_TEMPLATE = "https://wellfound.com/jobs?query={query}"
    JOB_LINK_PATTERN = re.compile(r"wellfound\.com/jobs/\d+", re.I)

    CAPABILITIES = PlatformCapabilities(
        **READ_ONLY_CAPABILITIES,
        tos_risk_note=(
            "Wellfound's terms restrict automated access and applications there are "
            "tied to your profile. Search and read only; never submits."
        ),
    )
    VERIFIED_AGAINST = VerificationLevel.FIXTURE


class DiceConnector(ConsumerBoardConnector):
    """Dice — search and read only."""

    PLATFORM = "dice"
    SEARCH_URL_TEMPLATE = "https://www.dice.com/jobs?q={query}&location={location}"
    PAGE_QUERY_PARAM = "page"
    JOB_LINK_PATTERN = re.compile(r"dice\.com/job-detail/", re.I)

    CAPABILITIES = PlatformCapabilities(
        **READ_ONLY_CAPABILITIES,
        tos_risk_note=(
            "Dice's terms restrict automated access. Search and read only; never "
            "submits."
        ),
    )
    VERIFIED_AGAINST = VerificationLevel.FIXTURE


class JobStreetConnector(ConsumerBoardConnector):
    """JobStreet (SEEK, Asia-Pacific) — search and read only."""

    PLATFORM = "jobstreet"
    SEARCH_URL_TEMPLATE = "https://www.jobstreet.com/{query}-jobs"
    JOB_LINK_PATTERN = re.compile(r"jobstreet\.com(\.[a-z]{2})?/job/\d+", re.I)

    CAPABILITIES = PlatformCapabilities(
        **READ_ONLY_CAPABILITIES,
        tos_risk_note=(
            "JobStreet's terms restrict automated access. Search and read only; "
            "never submits."
        ),
    )
    VERIFIED_AGAINST = VerificationLevel.FIXTURE


BOARD_CONNECTORS: List[type] = [
    LinkedInConnector,
    IndeedConnector,
    GlassdoorConnector,
    ZipRecruiterConnector,
    WellfoundConnector,
    DiceConnector,
    JobStreetConnector,
]
