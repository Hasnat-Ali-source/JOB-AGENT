"""
Hosted ATS Connectors (Phase 7a–7e).

Greenhouse, Lever, Ashby, Workday, SmartRecruiters and Workable.

These are the platforms companies host their own careers pages on, so applying
through them is applying directly to the employer — there is no third-party
terms-of-service problem with automating a form the company published for
applicants to fill in. That is why they come first in the build order, ahead of
the consumer job boards.

**Verification status.** Every connector here is built from each platform's
published URL structure and public form markup, and is tested against local
fixtures replicating that markup. None has been run against a live tenant from
this machine — doing so would mean submitting real applications to real
employers. They are therefore marked `VerificationLevel.FIXTURE`, and the
Phase 6 clean-submissions gate keeps the first three applications on any
platform under human review regardless of what the capabilities claim.
"""

import logging
import re
from typing import List, Optional

from job_agent.connectors.base import JobPosting, PlatformCapabilities
from job_agent.connectors.hosted_ats import HostedATSConnector, VerificationLevel

logger = logging.getLogger(__name__)


# ============================================================================
# 7a. Greenhouse
# ============================================================================

class GreenhouseConnector(HostedATSConnector):
    """
    Greenhouse job boards.

    Boards live at job-boards.greenhouse.io/{company} (and the older
    boards.greenhouse.io), with postings at /{company}/jobs/{id}. The
    application form uses stable field names — first_name, last_name, email,
    phone, resume — plus custom questions the classifier routes to the user.

    Companies also embed the board in their own careers page through a
    #grnhse_app iframe; `read_job_details` follows into it when present.
    """

    PLATFORM = "greenhouse"
    BOARD_URL_TEMPLATE = "https://job-boards.greenhouse.io/{board}"
    JOB_LINK_PATTERN = re.compile(r"greenhouse\.io/[^/]+/jobs/\d+", re.I)

    CAPABILITIES = PlatformCapabilities(
        can_search=True,
        can_filter=False,  # Boards have no filter controls; hard filters cover it
        can_read_details=True,
        can_start_application=True,
        can_fill_standard_fields=True,
        can_upload_documents=True,
        can_process_custom_questions=True,  # Detected and deferred, not answered
        can_submit_automatically=True,
        requires_manual_signin=False,  # Public boards need no account
        requires_manual_review_first_n=3,
    )
    VERIFIED_AGAINST = VerificationLevel.FIXTURE

    EMBED_SELECTOR = "#grnhse_app iframe, iframe[src*='greenhouse.io']"

    async def read_job_details(self, job_url: str) -> JobPosting:
        """
        Read a posting, following an embedded board iframe when present.

        Args:
            job_url: Job posting URL

        Returns:
            JobPosting
        """
        posting = await super().read_job_details(job_url)

        if posting.title not in ("Job Posting", "Unable to read", "Error"):
            return posting

        # The page may be a company careers page wrapping the real board
        frame_url = await self._embedded_board_url()

        if frame_url:
            logger.info(f"{self.platform_name}: following the embedded board to {frame_url}")
            return await super().read_job_details(frame_url)

        return posting

    async def _embedded_board_url(self) -> Optional[str]:
        """Return the src of an embedded Greenhouse iframe, if there is one."""
        if not self._page:
            return None

        try:
            locator = self._page.locator(self.EMBED_SELECTOR).first

            if await locator.count() == 0:
                return None

            return await locator.get_attribute("src", timeout=2000)
        except Exception as e:
            logger.debug(f"No embedded Greenhouse board: {e}")
            return None


# ============================================================================
# 7b. Lever
# ============================================================================

class LeverConnector(HostedATSConnector):
    """
    Lever job boards.

    Boards live at jobs.lever.co/{company}, postings at
    jobs.lever.co/{company}/{uuid}, and the application form is the same URL
    with /apply appended — so there is no need to hunt for an apply button.

    Lever names its link fields urls[LinkedIn], urls[GitHub] and so on; those
    are mapped so the classifier recognizes them.
    """

    PLATFORM = "lever"
    BOARD_URL_TEMPLATE = "https://jobs.lever.co/{board}"
    JOB_LINK_PATTERN = re.compile(
        r"jobs\.lever\.co/[^/]+/[0-9a-f]{8}-[0-9a-f]{4}", re.I
    )
    APPLY_PATH_SUFFIX = "/apply"

    EXTRA_FIELD_ALIASES = {
        "urls[LinkedIn]": "linkedin_url",
        "urls[GitHub]": "github_url",
        "urls[Portfolio]": "portfolio_url",
        "org": "current_employer",
    }

    CAPABILITIES = PlatformCapabilities(
        can_search=True,
        can_filter=False,
        can_read_details=True,
        can_start_application=True,
        can_fill_standard_fields=True,
        can_upload_documents=True,
        can_process_custom_questions=True,
        can_submit_automatically=True,
        requires_manual_signin=False,
        requires_manual_review_first_n=3,
    )
    VERIFIED_AGAINST = VerificationLevel.FIXTURE


# ============================================================================
# 7c. Ashby
# ============================================================================

class AshbyConnector(HostedATSConnector):
    """
    Ashby job boards.

    Boards live at jobs.ashbyhq.com/{company}. Ashby renders client-side, so
    pages need to settle before anything is read — the inherited networkidle
    wait handles that.

    Custom questions are common here and are detected and deferred rather than
    answered, which is why `can_process_custom_questions` is true while
    `can_submit_automatically` is not: a posting whose required custom question
    the agent can't answer must not be submitted half-complete. Submission stays
    with the user until the flow is confirmed against a live board.
    """

    PLATFORM = "ashby"
    BOARD_URL_TEMPLATE = "https://jobs.ashbyhq.com/{board}"
    JOB_LINK_PATTERN = re.compile(
        r"jobs\.ashbyhq\.com/[^/]+/[0-9a-f]{8}-[0-9a-f]{4}", re.I
    )
    APPLY_PATH_SUFFIX = "/application"

    CAPABILITIES = PlatformCapabilities(
        can_search=True,
        can_filter=False,
        can_read_details=True,
        can_start_application=True,
        can_fill_standard_fields=True,
        can_upload_documents=True,
        can_process_custom_questions=True,
        can_submit_automatically=False,  # Custom-question heavy; user submits
        requires_manual_signin=False,
        requires_manual_review_first_n=3,
        tos_risk_note=(
            "Ashby boards render client-side and lean on custom questions. "
            "Review each application before submitting."
        ),
    )
    VERIFIED_AGAINST = VerificationLevel.FIXTURE


# ============================================================================
# 7d. Workday
# ============================================================================

class WorkdayConnector(HostedATSConnector):
    """
    Workday tenants.

    Tenants live at {tenant}.wd{N}.myworkdayjobs.com/{site}, and that is where
    the predictability ends. Workday is configured per customer: the
    application is a multi-step wizard whose steps, field names and required
    questions differ between tenants, and most require creating an account
    before applying.

    So this connector honestly claims only what holds everywhere: finding and
    reading postings, and opening the application. It does not claim to fill or
    submit, because a connector that fills two of five wizard steps and stops
    is worse than one that doesn't start.
    """

    PLATFORM = "workday"
    BOARD_URL_TEMPLATE = "https://{board}.myworkdayjobs.com"
    JOB_LINK_PATTERN = re.compile(r"myworkdayjobs\.com/.+/job/", re.I)
    SEARCH_QUERY_PARAM = "q"
    # Greenhouse/Lever/Ashby boards list everything on one page; Workday pages
    PAGE_QUERY_PARAM = "page"

    CAPABILITIES = PlatformCapabilities(
        can_search=True,
        can_filter=False,
        can_read_details=True,
        can_start_application=True,
        can_fill_standard_fields=False,  # Wizard varies per tenant
        can_upload_documents=False,
        can_process_custom_questions=False,
        can_submit_automatically=False,
        requires_manual_signin=True,  # Most tenants require an account
        requires_manual_review_first_n=5,
        tos_risk_note=(
            "Workday is configured per employer: the application is a multi-step "
            "wizard that differs between tenants and usually requires an account. "
            "This connector finds and reads postings and opens the application; "
            "you complete it yourself."
        ),
    )
    VERIFIED_AGAINST = VerificationLevel.FIXTURE

    @classmethod
    def board_url(cls, board: str) -> str:
        """
        Build a tenant URL.

        Accepts either a full host ("acme.wd5.myworkdayjobs.com") or a bare
        tenant name, since tenants embed a data-centre number that can't be
        guessed.

        Args:
            board: Tenant host or name

        Returns:
            Tenant URL
        """
        board = board.strip("/")

        if board.startswith("http"):
            return board

        if "myworkdayjobs.com" in board:
            return f"https://{board}"

        return f"https://{board}.myworkdayjobs.com"


# ============================================================================
# 7e. SmartRecruiters and Workable
# ============================================================================

class SmartRecruitersConnector(HostedATSConnector):
    """
    SmartRecruiters job boards.

    Boards live at jobs.smartrecruiters.com/{company}, with postings under
    /{company}/{id}-{slug}. Standard fields; custom questions are deferred.
    """

    PLATFORM = "smartrecruiters"
    BOARD_URL_TEMPLATE = "https://jobs.smartrecruiters.com/{board}"
    JOB_LINK_PATTERN = re.compile(r"jobs\.smartrecruiters\.com/[^/]+/\d+", re.I)

    CAPABILITIES = PlatformCapabilities(
        can_search=True,
        can_filter=False,
        can_read_details=True,
        can_start_application=True,
        can_fill_standard_fields=True,
        can_upload_documents=True,
        can_process_custom_questions=True,
        can_submit_automatically=True,
        requires_manual_signin=False,
        requires_manual_review_first_n=3,
    )
    VERIFIED_AGAINST = VerificationLevel.FIXTURE


class WorkableConnector(HostedATSConnector):
    """
    Workable job boards.

    Boards live at apply.workable.com/{company}, with postings at /j/{id}.
    """

    PLATFORM = "workable"
    BOARD_URL_TEMPLATE = "https://apply.workable.com/{board}"
    JOB_LINK_PATTERN = re.compile(r"apply\.workable\.com/[^/]+/j/[A-Z0-9]+", re.I)
    APPLY_PATH_SUFFIX = "/apply"

    CAPABILITIES = PlatformCapabilities(
        can_search=True,
        can_filter=False,
        can_read_details=True,
        can_start_application=True,
        can_fill_standard_fields=True,
        can_upload_documents=True,
        can_process_custom_questions=True,
        can_submit_automatically=True,
        requires_manual_signin=False,
        requires_manual_review_first_n=3,
    )
    VERIFIED_AGAINST = VerificationLevel.FIXTURE


ATS_CONNECTORS: List[type] = [
    GreenhouseConnector,
    LeverConnector,
    AshbyConnector,
    WorkdayConnector,
    SmartRecruitersConnector,
    WorkableConnector,
]
