"""
Base connector class and capability definitions (Phase 2).

All platform-specific connectors (LinkedIn, Greenhouse, Indeed, etc.) inherit from
ConnectedPlatformConnector and implement the required methods.

Each connector declares its PlatformCapabilities to ensure the GUI only exposes
actions that connector actually supports.
"""

from dataclasses import dataclass
from enum import Enum
from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any, TYPE_CHECKING
import logging

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from job_agent.models.database import SearchProfile



class ConnectionStatus(str, Enum):
    """Current session status for a connector."""
    CONNECTED = "connected"
    NOT_CONNECTED = "not_connected"
    NEEDS_SIGNIN = "needs_signin"
    SESSION_EXPIRED = "session_expired"
    ERROR = "error"


@dataclass
class PlatformCapabilities:
    """
    Declares what this platform connector can do.
    
    Used by the GUI to show/hide actions. Be honest — don't claim a capability
    unless it's fully implemented and tested.
    
    Example usage:
        LinkedIn connector:
            can_search=True, can_submit_automatically=False (default for consumer boards)
        Greenhouse connector:
            can_search=False, can_submit_automatically=True (default for ATS-hosted)
    """
    
    can_search: bool = False
    """Can find jobs via search on this platform"""
    
    can_filter: bool = False
    """Can apply search filters (job type, location, salary, etc.)"""
    
    can_read_details: bool = False
    """Can fetch full job details (title, description, requirements, etc.)"""
    
    can_start_application: bool = False
    """Can open an application form or email draft"""
    
    can_fill_standard_fields: bool = False
    """Can fill common fields (name, email, phone, resume, cover letter)"""
    
    can_upload_documents: bool = False
    """Can attach resume/cover letter as files"""
    
    can_process_custom_questions: bool = False
    """Can detect and attempt to answer custom questions (pauses on unknowns)"""
    
    can_submit_automatically: bool = False
    """Can submit application without user review (requires clean_submissions_count gate first)"""
    
    requires_manual_signin: bool = True
    """User must sign in manually (no credential handling by agent)"""
    
    requires_manual_review_first_n: int = 3
    """Number of submissions that must be manually reviewed before auto-submit is eligible"""
    
    tos_risk_note: Optional[str] = None
    """Risk warning shown in GUI when auto-submit is enabled (especially for consumer boards)"""


@dataclass
class JobPosting:
    """Standardized job posting data returned by read_job_details()."""
    
    platform: str  # "linkedin", "greenhouse", etc.
    external_id: str  # Platform's job ID
    title: str
    company: str
    # Defaulted deliberately. Plenty of postings state no location at all, and
    # requiring one meant every fallback path that omitted it raised
    # TypeError — including the *exception handler*, so a failure to read one
    # SimplyHired posting surfaced as "missing 1 required positional argument:
    # 'location'" and the real cause was never logged.
    location: str = "Not specified"
    job_type: Optional[str] = None  # "full_time", "part_time", "contract"
    seniority: Optional[str] = None  # "entry", "mid", "senior", "staff"
    description: str = ""  # Full job description
    requirements: Optional[str] = None
    salary: Optional[str] = None
    posted_at: Optional[str] = None  # ISO format or "2 days ago"
    apply_method: str = "web_form"  # "web_form", "email", "external_link"
    recruiter_contact: Optional[str] = None  # Email address for email applications
    apply_url: Optional[str] = None  # Direct link to application
    raw_data: Dict[str, Any] = None  # Full page data (JSON, HTML, etc.)


@dataclass
class ApplicationSession:
    """
    Represents an in-progress application form or email draft.
    
    Tracks filled fields, form state, and screenshot for review.
    """
    
    job: JobPosting
    platform_account_id: int
    form_url: Optional[str] = None  # URL of the application form
    filled_fields: Dict[str, str] = None  # {field_name: value}
    deferred_fields: Dict[str, Dict[str, Any]] = None  # {field_name: {reason, question, ...}}
    form_state: Dict[str, Any] = None  # Page state, field IDs, etc. (implementation-specific)
    screenshot_path: Optional[str] = None  # Path to screenshot of filled form
    
    def __post_init__(self):
        if self.filled_fields is None:
            self.filled_fields = {}
        if self.deferred_fields is None:
            self.deferred_fields = {}
        if self.form_state is None:
            self.form_state = {}


@dataclass
class SubmissionResult:
    """Result of application submission."""
    
    success: bool
    confirmation_ref: Optional[str] = None  # Confirmation number, job ID, etc.
    confirmation_url: Optional[str] = None  # URL to confirm application was submitted
    confirmation_message: Optional[str] = None  # "Application submitted successfully"
    error_message: Optional[str] = None  # Error details if success=False


class ConnectedPlatformConnector(ABC):
    """
    Base class for all platform-specific connectors.
    
    Subclasses implement platform-specific logic for:
    - Signing in (handled via persistent browser context, §3)
    - Searching for jobs
    - Reading job details
    - Opening and filling application forms
    - Submitting applications
    
    Each connector declares its capabilities via the PlatformCapabilities struct
    so the GUI knows what actions are supported.
    """
    
    platform_name: str
    """Short name: "linkedin", "greenhouse", "indeed", etc."""
    
    capabilities: PlatformCapabilities
    """What this connector can do."""
    
    def __init__(self, platform_name: str, capabilities: PlatformCapabilities):
        """
        Initialize the connector.

        Args:
            platform_name: Short identifier (e.g., "linkedin")
            capabilities: PlatformCapabilities describing what we can do
        """
        self.platform_name = platform_name
        self.capabilities = capabilities
        self._page: Optional[Any] = None
        self.search_url: Optional[str] = None

    def set_search_url(self, search_url: Optional[str]) -> None:
        """
        Set the platform's search entry point.

        Connectors that navigate to a search page (see GenericATSConnector)
        read this; connectors with a hard-coded search flow ignore it.

        Args:
            search_url: Search page URL, optionally templated with
                `{query}` / `{location}` placeholders
        """
        self.search_url = search_url

    def set_page(self, page: Any) -> None:
        """
        Attach an authenticated Playwright page to this connector.

        Called by the search pipeline (or SessionManager) before any navigation
        method runs. Connectors read the page via `self.page`.

        Args:
            page: Playwright Page object
        """
        self._page = page

    @property
    def page(self) -> Optional[Any]:
        """The attached Playwright page, or None if not set."""
        return self._page

    @abstractmethod
    async def check_session(self) -> ConnectionStatus:
        """
        Check if the persistent browser session is still authenticated.
        
        Must not read or touch credential fields — only check for logged-in indicators
        (e.g., presence of a user menu, absence of login form).
        
        Returns:
            ConnectionStatus.CONNECTED if authenticated and ready
            ConnectionStatus.SESSION_EXPIRED if session is stale
            ConnectionStatus.NOT_CONNECTED if not signed in
            ConnectionStatus.ERROR if something went wrong
        """
        pass
    
    @abstractmethod
    async def open_search(self, search_profile: 'SearchProfile') -> None:
        """
        Navigate to the job search page and prepare for filtering.
        
        Args:
            search_profile: User's search filters (titles, location, salary, etc.)
        
        Raises:
            Exception: If unable to reach search page or apply filters.
        """
        pass
    
    @abstractmethod
    async def apply_search_filters(self, search_profile: 'SearchProfile') -> None:
        """
        Apply search profile filters on the job search page.
        
        This includes setting job title, location, job type, salary range,
        keywords, date posted, etc. — whatever the platform supports.
        
        Args:
            search_profile: User's filters
        
        Raises:
            Exception: If unable to set filters or search.
        """
        pass
    
    @abstractmethod
    async def collect_job_links(self) -> List[str]:
        """
        Collect links to all job postings from current search results.
        
        Must respect the daily_search_limit set on the platform account.
        
        Returns:
            List of job URLs/links to pass to read_job_details().
        """
        pass
    
    @abstractmethod
    async def read_job_details(self, job_url: str) -> JobPosting:
        """
        Read full job details from a posting.
        
        Args:
            job_url: URL or link to the job posting
        
        Returns:
            JobPosting object with all extracted details
        
        Raises:
            Exception: If unable to fetch or parse the posting.
        """
        pass
    
    @abstractmethod
    async def begin_application(self, job: JobPosting) -> ApplicationSession:
        """
        Start an application for a job.
        
        Opens the application form or email draft view.
        
        Args:
            job: JobPosting to apply for
        
        Returns:
            ApplicationSession tracking the in-progress application
        
        Raises:
            Exception: If unable to open application flow.
        """
        pass
    
    @abstractmethod
    async def fill_application(
        self,
        session: ApplicationSession,
        candidate_profile: Dict[str, Any],
        application_package: Dict[str, Any]
    ) -> ApplicationSession:
        """
        Fill in known fields of the application form.
        
        Must:
        - Fill common fields (name, email, phone, resume, cover letter) automatically
        - For unknown or sensitive fields (compensation, demographics, etc.):
          - Mark as deferred with reason and question text
          - Do NOT guess or auto-fill
        - Capture a screenshot of the filled form
        - Return the updated ApplicationSession
        
        Args:
            session: ApplicationSession to fill
            candidate_profile: User's resume/profile data
            application_package: Tailored resume + cover letter for this job
        
        Returns:
            Updated ApplicationSession with filled_fields and deferred_fields
        """
        pass
    
    @abstractmethod
    async def submit_application(self, session: ApplicationSession) -> SubmissionResult:
        """
        Submit a filled application.
        
        Called only after user reviews and approves the filled form (from Review Queue).
        
        Args:
            session: ApplicationSession ready to submit
        
        Returns:
            SubmissionResult with success status and confirmation details
        """
        pass
