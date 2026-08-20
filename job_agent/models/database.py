"""
SQLModel data models for the Job Agent.

Defines all database tables and relationships:
- SearchProfile: Reusable search filters
- PlatformAccount: User's authenticated session on a job platform
- Job: A discovered job posting
- Application: User's application to a job
- AuditLog: Complete audit trail of all actions
- EmailThread: Thread tracking for email-based applications
"""

from datetime import datetime
from typing import Optional, List
from enum import Enum

from sqlalchemy import Column, JSON
from sqlmodel import SQLModel, Field, Relationship

from job_agent.utils.dates import utcnow


# ============================================================================
# Enums
# ============================================================================

class ConnectionStatus(str, Enum):
    """Status of a platform account connection."""
    CONNECTED = "connected"
    NEEDS_SIGNIN = "needs_signin"
    SESSION_EXPIRED = "session_expired"
    DISABLED = "disabled"
    ERROR = "error"


class ApplyStrategy(str, Enum):
    """
    How an application is put together on a given platform.

    Not every board wants the same thing, and forcing one shape on all of them
    is what limited the agent. Two are genuinely different:

    - TAILORED: the agent writes a resume and cover letter for the posting,
      attaches them, fills the form and holds it for review. This is where the
      product's value is, and it needs a board that accepts an upload.
    - PLATFORM_PROFILE: the platform already holds the documents and answers —
      Indeed SmartApply, LinkedIn Easy Apply — and an application is a matter
      of driving its flow. Generating a tailored PDF for one of these is wasted
      work: the board never asks for it and sends its own copy instead.
    """

    TAILORED = "tailored"
    PLATFORM_PROFILE = "platform_profile"


class AutomationMode(str, Enum):
    """Automation level for a platform account."""
    SEARCH_ONLY = "search_only"  # Read-only search
    SEARCH_AND_ANALYZE = "search_and_analyze"  # Search + fit scoring, no fill
    SEARCH_AND_PREPARE = "search_and_prepare"  # Search + prepare (fill form but no submit)
    SEARCH_FILL_SUBMIT = "search_fill_submit"  # Auto search, fill, submit (after proving clean)
    MANUAL_ONLY = "manual_only"  # User controls everything


class ApplicationStatus(str, Enum):
    """Status of an application."""
    DRAFT = "draft"  # Never submitted
    QUEUED_FOR_REVIEW = "queued_for_review"  # Awaiting user approval
    SUBMITTED = "submitted"  # Form submitted
    EMAIL_SENT = "email_sent"  # Email sent
    REJECTED = "rejected"  # User discarded or company rejected
    WITHDRAWN = "withdrawn"  # User withdrew application


class AuditAction(str, Enum):
    """Audit log action types."""
    ACCOUNT_CONNECTED = "account_connected"
    ACCOUNT_DISCONNECTED = "account_disconnected"
    SESSION_CHECKED = "session_checked"
    SEARCH_RUN = "search_run"
    JOB_COLLECTED = "job_collected"
    JOB_DEDUPED = "job_deduped"
    JOB_SCORED = "job_scored"
    APPLICATION_STARTED = "application_started"
    APPLICATION_FILLED = "application_filled"
    APPLICATION_REVIEWED = "application_reviewed"
    APPLICATION_SUBMITTED = "application_submitted"
    EMAIL_DRAFTED = "email_drafted"
    EMAIL_SENT = "email_sent"
    EMAIL_REPLY_RECEIVED = "email_reply_received"
    ERROR_OCCURRED = "error_occurred"
    SESSION_EXPIRED = "session_expired"
    CAPTCHA_DETECTED = "captcha_detected"
    MFA_REQUIRED = "mfa_required"
    # Phase 4
    DOCUMENT_UPLOADED = "document_uploaded"
    DOCUMENT_TAILORED = "document_tailored"
    DOCUMENT_FLAGGED = "document_flagged"
    # Phase 5
    FORM_READ = "form_read"
    FIELD_DEFERRED = "field_deferred"
    APPLICATION_QUEUED = "application_queued"
    APPLICATION_APPROVED = "application_approved"
    APPLICATION_DISCARDED = "application_discarded"
    # Phase 6b
    EMAIL_RECIPIENT_DETECTED = "email_recipient_detected"
    EMAIL_APPROVED = "email_approved"
    EMAIL_SEND_FAILED = "email_send_failed"
    # Phase 8
    RUN_STARTED = "run_started"
    RUN_COMPLETED = "run_completed"
    PLATFORM_SKIPPED = "platform_skipped"
    RUN_INTERRUPTED = "run_interrupted"
    # Phase 9
    SESSION_HEALTHY = "session_healthy"
    INTERRUPTION_RAISED = "interruption_raised"
    INTERRUPTION_RESOLVED = "interruption_resolved"
    PLATFORM_RECONNECTED = "platform_reconnected"
    PLATFORM_RESUMED = "platform_resumed"
    # Phase 11
    APPLICATION_ANALYSED = "application_analysed"


class RunStatus(str, Enum):
    """Outcome of an orchestrated run."""

    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"  # Some platforms failed or were interrupted
    FAILED = "failed"


class FieldCategory(str, Enum):
    """
    How a form field may be handled.

    SENSITIVE never gets an automatic answer — see FieldClassifier.
    """
    KNOWN = "known"  # Mapped to the candidate profile, safe to fill
    REMEMBERED = "remembered"  # The user answered this before; reuse their answer
    SENSITIVE = "sensitive"  # Must be answered by the user, every time
    UNKNOWN = "unknown"  # No confident mapping — ask the user


class DocumentType(str, Enum):
    """Kind of application document."""
    RESUME = "resume"
    COVER_LETTER = "cover_letter"


class DocumentFormat(str, Enum):
    """Source format of an uploaded master document."""
    DOCX = "docx"
    PDF = "pdf"
    TXT = "txt"
    MARKDOWN = "md"


class EmailThreadStatus(str, Enum):
    """Status of an email application thread."""
    SENT = "sent"
    AWAITING_REPLY = "awaiting_reply"
    REPLY_RECEIVED = "reply_received"
    CLOSED = "closed"


class EmailDraftStatus(str, Enum):
    """
    Status of an outbound application email (§6b).

    Every draft passes through APPROVED before it can be sent — there is no
    path from DRAFT to SENT.
    """
    DRAFT = "draft"  # Composed, awaiting the user
    APPROVED = "approved"  # User has read it and approved sending
    SENT = "sent"
    FAILED = "failed"
    DISCARDED = "discarded"


# ============================================================================
# Models
# ============================================================================

class SearchProfile(SQLModel, table=True):
    """
    Reusable search profile with filters.
    
    User can create multiple profiles (e.g., "Senior Backend Remote", "Staff Backend Hybrid SG")
    and select which ones to run on which platforms.
    """
    __tablename__ = "search_profiles"

    id: Optional[int] = Field(default=None, primary_key=True)
    
    # Profile metadata
    name: str = Field(index=True)  # e.g., "Senior Backend - Remote"
    description: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    
    # Job filters
    target_titles: List[str] = Field(default=[], sa_column=Column(JSON))  # e.g., ["Backend Engineer", "Senior Backend Engineer"]
    alt_titles: List[str] = Field(default=[], sa_column=Column(JSON))  # Alternative titles to match
    job_type: Optional[str] = None  # "full_time", "part_time", "contract", "freelance"
    seniority: Optional[str] = None  # "entry", "mid", "senior", "staff", "executive"
    
    # Location filters
    country: Optional[str] = None  # e.g., "US"
    region: Optional[str] = None  # e.g., "CA", "NY", "Singapore"
    remote_pref: Optional[str] = None  # "remote", "hybrid", "on_site"
    
    # Content filters
    keywords: List[str] = Field(default=[], sa_column=Column(JSON))  # Keywords to match (AND)
    exclusions: List[str] = Field(default=[], sa_column=Column(JSON))  # Keywords to exclude (NOT)
    
    # Salary & date filters
    salary_min: Optional[int] = None  # In platform's local currency
    salary_max: Optional[int] = None
    date_posted_within_days: Optional[int] = None  # e.g., 7 (last 7 days)
    
    # The master resume these terms were derived from. Set when the search is
    # synced to a resume, so the desk can say whether the search still
    # reflects the resume in use.
    derived_from_master_id: Optional[int] = None

    # Metadata
    is_active: bool = Field(default=True)


class CandidateProfile(SQLModel, table=True):
    """
    The user's own details, used to fill application forms (§5).

    Only contains what the user typed in themselves. The agent never infers a
    value for any of these — a wrong phone number or work-authorization answer
    on a real application is the user's problem to live with, not the agent's
    to guess at.

    Demographic, disability, and veteran information is deliberately absent:
    those are voluntary self-identification questions and are always routed to
    the user (see FieldClassifier.SENSITIVE_PATTERNS).
    """
    __tablename__ = "candidate_profiles"

    id: Optional[int] = Field(default=None, primary_key=True)

    # Contact details
    full_name: str
    email: str
    phone: Optional[str] = None
    location: Optional[str] = None  # "San Francisco, CA"

    # Links
    linkedin_url: Optional[str] = None
    github_url: Optional[str] = None
    portfolio_url: Optional[str] = None
    website_url: Optional[str] = None

    # Standard application questions, answered once by the user.
    # Left None means "ask me" rather than "assume no".
    work_authorization: Optional[str] = None  # e.g. "US citizen", "H-1B"
    requires_sponsorship: Optional[bool] = None
    willing_to_relocate: Optional[bool] = None
    years_experience: Optional[int] = None
    notice_period: Optional[str] = None

    # Answers the user supplied while reviewing a form, keyed by a normalized
    # question so the same question isn't asked on every future application.
    remembered_answers: dict = Field(default={}, sa_column=Column(JSON))

    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class MasterDocument(SQLModel, table=True):
    """
    A master resume or cover letter uploaded by the user (§4).

    This is the single source of truth for the user's actual experience.
    Tailored variants may reorder and rephrase this content but must never
    introduce facts that aren't here — see DocumentVersion.fabrication_flags.
    """
    __tablename__ = "master_documents"

    id: Optional[int] = Field(default=None, primary_key=True)

    doc_type: DocumentType = Field(index=True)
    name: str  # e.g. "Backend Engineer resume 2026"

    # Original upload, kept verbatim so the user can always re-download it
    source_path: str
    source_format: DocumentFormat

    # Extracted plain text and detected sections ({"experience": "...", ...})
    content_text: str
    sections: dict = Field(default={}, sa_column=Column(JSON))

    # Only one master per doc_type is active at a time
    is_active: bool = Field(default=True)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    versions: List["DocumentVersion"] = Relationship(back_populates="master_document")


class DocumentVersion(SQLModel, table=True):
    """
    A tailored variant of a master document, generated for one job (§4).

    Every variant records which master it came from, which job it targets, and
    whether verification found content that isn't supported by the master.
    """
    __tablename__ = "document_versions"

    id: Optional[int] = Field(default=None, primary_key=True)

    master_document_id: int = Field(foreign_key="master_documents.id")
    job_id: Optional[int] = Field(default=None, foreign_key="jobs.id", index=True)

    doc_type: DocumentType = Field(index=True)

    # Tailored text and the rendered PDF the user reviews / submits
    content_text: str
    pdf_path: Optional[str] = None

    # How it was produced: "llm:ollama:<model>", "llm:anthropic:<model>",
    # or "deterministic" when no LLM was available
    generator: str = Field(default="deterministic")

    # Plain-English record of what changed, for the review queue
    tailoring_notes: List[str] = Field(default=[], sa_column=Column(JSON))

    # Claims present in the variant but NOT in the master. Non-empty means the
    # variant must not be submitted without the user reading it first.
    fabrication_flags: List[str] = Field(default=[], sa_column=Column(JSON))

    created_at: datetime = Field(default_factory=utcnow)

    master_document: MasterDocument = Relationship(back_populates="versions")

    @property
    def is_verified(self) -> bool:
        """True when nothing unsupported by the master was detected."""
        return not self.fabrication_flags


class PlatformAccount(SQLModel, table=True):
    """
    User's authenticated session on a job platform.
    
    Stores only non-sensitive metadata; actual credentials/session cookies
    are managed by Playwright and stored in the profile directory or Keychain.
    """
    __tablename__ = "platform_accounts"

    id: Optional[int] = Field(default=None, primary_key=True)
    
    platform: str = Field(index=True)  # "linkedin", "indeed", "greenhouse", etc.
    status: ConnectionStatus = Field(default=ConnectionStatus.NEEDS_SIGNIN)

    # Which registered connector drives this station. None means the platform
    # name is itself a built-in connector — the usual case. A station the user
    # added by pasting a careers URL has no connector of its own, so it names
    # the generic one here and carries its own search_url below.
    connector_kind: Optional[str] = None

    # Taken out of service by the user, without losing the connection. A
    # station you have signed into is expensive to rebuild, so "stop using
    # this one for now" must not mean "disconnect it".
    paused: bool = Field(default=False)

    # How this station wants an application built. See ApplyStrategy: a board
    # that supplies its own documents should not have documents written for it.
    apply_strategy: ApplyStrategy = Field(default=ApplyStrategy.TAILORED)

    # Whether this station needs a signed-in session. None defers to what the
    # connector declares — the right answer for the built-in platforms. A
    # user-added station says so itself, because the same generic connector
    # drives both a public board and one behind a login.
    requires_signin: Optional[bool] = None
    
    # Automation settings
    automation_mode: AutomationMode = Field(default=AutomationMode.SEARCH_AND_ANALYZE)
    daily_search_limit: int = Field(default=100)
    daily_apply_limit: int = Field(default=10)
    daily_message_limit: int = Field(default=5)  # For platforms that support direct messaging
    
    # Search entry point for connectors that navigate to a search page.
    # Either a plain URL ("https://acme.com/careers") or a template with
    # {query}/{location} placeholders ("https://acme.com/careers?q={query}").
    search_url: Optional[str] = None

    # Session tracking (non-sensitive)
    profile_dir: str  # Path to persistent browser profile (e.g., ~/Library/Application Support/job_agent/profiles/linkedin/)
    last_verified_at: Optional[datetime] = None
    last_error: Optional[str] = None
    
    # Clean submissions tracking (§6)
    clean_submissions_count: int = Field(default=0)  # Incremented after each manually-reviewed + clean submission
    
    # Metadata
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    
    # Relationships
    applications: List["Application"] = Relationship(back_populates="platform_account")


class Job(SQLModel, table=True):
    """
    A discovered job posting from any platform.
    
    Includes dedup_hash to recognize the same job from multiple sources.
    """
    __tablename__ = "jobs"

    id: Optional[int] = Field(default=None, primary_key=True)
    
    # Platform metadata
    platform: str = Field(index=True)  # "linkedin", "greenhouse", etc.
    external_id: str = Field(index=True)  # Platform's job ID
    
    # Job details
    title: str = Field(index=True)
    company: str = Field(index=True)
    location: str
    job_type: Optional[str] = None  # "full_time", "part_time", "contract", "freelance"
    
    # Job content
    description: str  # Full job description
    requirements: Optional[str] = None  # Requirements section or similar
    salary: Optional[str] = None  # Salary range (often in text format, may be localized)
    
    # Posting metadata
    posted_at: Optional[datetime] = None
    posted_at_text: Optional[str] = None  # "2 days ago" format for parsing later
    
    # Application method
    apply_method: str  # "web_form", "email", "external_link"
    recruiter_contact: Optional[str] = None  # Email or name for email applications
    
    # Scoring & filtering
    fit_score: Optional[float] = None  # 0-1, from LLM scoring
    hard_filter_pass: bool = Field(default=True)  # False if salary/location/keywords exclude it
    
    # Deduplication
    dedup_hash: str = Field(index=True)  # Hash of (company + title + location), normalized
    
    # Which master resume was in use when this posting was collected. The
    # wire shows only the current resume's postings, so that changing resume
    # changes what you are looking at — a Full-Stack resume should not be
    # read against a wire full of VP-of-Data roles found for a previous one.
    matched_master_id: Optional[int] = Field(default=None, index=True)

    # Tracking
    first_seen_at: datetime = Field(default_factory=utcnow)
    
    # Status
    status: str = Field(default="new")  # "new", "reviewing", "rejected", "applied"
    
    # Raw data backup
    raw_data: dict = Field(default={}, sa_column=Column(JSON))  # Full page JSON or HTML snapshot
    
    # Relationships
    applications: List["Application"] = Relationship(back_populates="job")


class Application(SQLModel, table=True):
    """
    User's application to a job posting.
    
    Tracks filled form data, submission status, confirmation, and review state.
    """
    __tablename__ = "applications"

    id: Optional[int] = Field(default=None, primary_key=True)
    
    # Foreign keys
    job_id: int = Field(foreign_key="jobs.id")
    platform_account_id: int = Field(foreign_key="platform_accounts.id")
    
    # Document versions
    resume_version: Optional[str] = None  # File path to tailored resume PDF
    cover_letter_version: Optional[str] = None  # File path to tailored cover letter PDF

    # Links to the DocumentVersion records those paths came from (Phase 4)
    resume_version_id: Optional[int] = Field(default=None, foreign_key="document_versions.id")
    cover_letter_version_id: Optional[int] = Field(default=None, foreign_key="document_versions.id")
    
    # Form data
    filled_fields: dict = Field(default={}, sa_column=Column(JSON))  # {field_name: value}
    deferred_fields: dict = Field(default={}, sa_column=Column(JSON))  # {field_name: {reason, question, value_entered_by_user}}
    
    # Submission & confirmation
    submission_status: ApplicationStatus = Field(default=ApplicationStatus.DRAFT)
    submitted_at: Optional[datetime] = None
    confirmation_ref: Optional[str] = None  # Confirmation number, job ID returned, etc.
    confirmation_url: Optional[str] = None
    
    # Email-specific
    recipient_email: Optional[str] = None  # For email applications
    email_message_id: Optional[str] = None  # RFC 2822 message-id for reply tracking
    
    # Review
    reviewed_by_user: bool = Field(default=False)
    user_notes: Optional[str] = None  # User's notes on this application
    screenshot_path: Optional[str] = None  # Screenshot of filled form before submission

    # Form context (Phase 5)
    form_url: Optional[str] = None  # Where the form was filled

    # How a multi-step form was walked: which steps were read, what was
    # pressed to advance, and where the walk stopped. Null for a single-page
    # form. Its own column rather than a key inside filled_fields, which half
    # a dozen places iterate as form fields and would replay onto the form.
    form_walk: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    candidate_profile_id: Optional[int] = Field(
        default=None, foreign_key="candidate_profiles.id"
    )
    filled_at: Optional[datetime] = None  # When the agent filled the form
    reviewed_at: Optional[datetime] = None  # When the user approved or discarded it
    
    # Metadata
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    
    # Relationships
    job: Job = Relationship(back_populates="applications")
    platform_account: PlatformAccount = Relationship(back_populates="applications")
    email_thread: Optional["EmailThread"] = Relationship(back_populates="application")


class AuditLog(SQLModel, table=True):
    """
    Complete audit trail of all actions taken by the agent.
    
    Enables compliance review, debugging, and performance monitoring.
    """
    __tablename__ = "audit_log"

    id: Optional[int] = Field(default=None, primary_key=True)
    
    # Timestamp & action
    timestamp: datetime = Field(default_factory=utcnow, index=True)
    action: AuditAction = Field(index=True)
    
    # Context
    platform: Optional[str] = Field(default=None, index=True)
    actor: str = Field(default="agent")  # "agent", "user", "system"
    
    # Details
    detail: str  # Human-readable description
    detail_json: Optional[dict] = Field(default=None, sa_column=Column(JSON))  # Structured data
    
    # Result
    result: str  # "success", "failure", "paused", etc.
    error_message: Optional[str] = None


class PlatformInterruption(SQLModel, table=True):
    """
    Something that stopped a platform and needs the user (§9).

    A CAPTCHA, an MFA prompt, an expired session, a rate-limit block. The
    record outlives the run that hit it, because the thing it represents is a
    task for the user rather than an event in a log: the platform stays paused
    until this row is resolved.

    Resolution is never automatic on the agent's say-so. Either the user states
    they've handled it, or `check_session()` confirms the platform is usable
    again — an agent deciding on its own that a CAPTCHA "probably passed" is
    how a session gets flagged.
    """
    __tablename__ = "platform_interruptions"

    id: Optional[int] = Field(default=None, primary_key=True)

    platform: str = Field(index=True)
    kind: str = Field(index=True)  # captcha | mfa | signin_required | rate_limited | blocked

    detected_at: datetime = Field(default_factory=utcnow, index=True)
    url: Optional[str] = None
    evidence: Optional[str] = None
    guidance: str = ""
    screenshot_path: Optional[str] = None

    run_id: Optional[int] = Field(default=None, foreign_key="agent_runs.id")

    resolved_at: Optional[datetime] = None
    resolved_by: Optional[str] = None  # "user" | "session_check"
    resolution_note: Optional[str] = None

    @property
    def is_open(self) -> bool:
        """True while this still blocks the platform."""
        return self.resolved_at is None


class AgentRun(SQLModel, table=True):
    """
    One orchestrated pass over the connected platforms (§8).

    Every scheduled or manual run gets a record, so a user coming back to an
    unattended agent can see what it did — and, more importantly, what it
    couldn't do and why.
    """
    __tablename__ = "agent_runs"

    id: Optional[int] = Field(default=None, primary_key=True)

    search_profile_id: Optional[int] = Field(
        default=None, foreign_key="search_profiles.id"
    )
    trigger: str = Field(default="manual")  # "manual" | "scheduled"

    started_at: datetime = Field(default_factory=utcnow, index=True)
    finished_at: Optional[datetime] = None
    status: RunStatus = Field(default=RunStatus.RUNNING, index=True)

    # Aggregate counters across every platform in the run
    platforms_run: List[str] = Field(default=[], sa_column=Column(JSON))
    platforms_skipped: dict = Field(default={}, sa_column=Column(JSON))
    jobs_found: int = Field(default=0)
    new_jobs: int = Field(default=0)
    duplicates_skipped: int = Field(default=0)
    hard_filters_failed: int = Field(default=0)
    documents_generated: int = Field(default=0)
    applications_queued: int = Field(default=0)
    applications_submitted: int = Field(default=0)

    # Anything that stopped a platform mid-run (CAPTCHA, MFA, expired session)
    interruptions: List[dict] = Field(default=[], sa_column=Column(JSON))
    errors: List[str] = Field(default=[], sa_column=Column(JSON))

    @property
    def duration_seconds(self) -> Optional[float]:
        """How long the run took, or None while it's still going."""
        if not self.finished_at:
            return None
        return (self.finished_at - self.started_at).total_seconds()


class EmailDraft(SQLModel, table=True):
    """
    An application email awaiting the user's approval (§6b).

    Email applications always stop here, whatever the platform's automation
    mode says. A web form is submitted inside a site's own workflow; an email
    goes out from the user's personal address, in their name, to a person — and
    it cannot be unsent. The clean-submissions gate does not apply to email
    because "the agent has done this correctly three times" does not transfer
    to a different recipient reading a different message.
    """
    __tablename__ = "email_drafts"

    id: Optional[int] = Field(default=None, primary_key=True)

    application_id: Optional[int] = Field(default=None, foreign_key="applications.id")
    job_id: int = Field(foreign_key="jobs.id", index=True)

    # The message
    to_email: str = Field(index=True)
    subject: str
    body: str
    attachments: List[str] = Field(default=[], sa_column=Column(JSON))

    # How the recipient was determined, shown to the user during review
    recipient_source: Optional[str] = None

    status: EmailDraftStatus = Field(default=EmailDraftStatus.DRAFT, index=True)
    reviewed_by_user: bool = Field(default=False)
    user_notes: Optional[str] = None

    # Send outcome
    send_method: Optional[str] = None  # "mail_app" or "smtp"
    sent_message_id: Optional[str] = None
    sent_at: Optional[datetime] = None
    error_message: Optional[str] = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class EmailThread(SQLModel, table=True):
    """
    Thread tracking for email-based applications (§5a).
    
    Links email sends to applications and tracks replies.
    """
    __tablename__ = "email_threads"

    id: Optional[int] = Field(default=None, primary_key=True)
    
    # Foreign key
    application_id: int = Field(foreign_key="applications.id")
    
    # Sent message metadata
    sent_message_id: str  # RFC 2822 message-id
    recipient_email: str = Field(index=True)
    subject: str
    sent_at: datetime = Field(default_factory=utcnow)
    
    # Reply tracking
    reply_received_at: Optional[datetime] = None
    reply_snippet: Optional[str] = None  # First 200 chars of reply body
    thread_status: EmailThreadStatus = Field(default=EmailThreadStatus.SENT)
    
    # Metadata
    created_at: datetime = Field(default_factory=utcnow)
    
    # Relationships
    application: Application = Relationship(back_populates="email_thread")
