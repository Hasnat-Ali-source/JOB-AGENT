"""
Submission Gate (Phase 6).

Decides whether an application may be submitted *without a human present*.

This is the point where the agent stops preparing work and starts acting on the
world: a submitted application reaches a real employer under the user's name and
cannot be recalled. So the gate is deliberately conservative and every refusal
is explained.

Two different questions are being answered here, and they have different bars:

- **User-directed submission** (`check_user_directed`) — the user is looking at
  the filled form and pressed submit. Only correctness matters: required
  questions answered, documents sound.
- **Unattended auto-submission** (`check_auto`) — nobody is watching. Everything
  above, *plus* explicit opt-in, a proven track record on this platform, and
  the daily cap.

The track record is `clean_submissions_count`: the number of applications on
this platform that a human reviewed and that submitted cleanly. Until that
reaches the threshold (default 3), every application on the platform goes to
review, whatever the automation mode says. A connector that fills forms subtly
wrong should be caught by a human on its first attempts, not on its fiftieth.
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy.orm import Session

from job_agent.config import settings
from job_agent.models.database import (
    Application,
    ApplicationStatus,
    AuditAction,
    AutomationMode,
    DocumentVersion,
    Job,
    PlatformAccount,
)
from job_agent.utils.dates import utcnow

logger = logging.getLogger(__name__)


@dataclass
class SubmissionDecision:
    """Whether a submission may proceed, and why."""

    allowed: bool
    blockers: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    checks_passed: List[str] = field(default_factory=list)
    # The analyst's full report, when it ran. Kept structured alongside the
    # flattened blockers so the tray can show each finding with its own fix
    # instead of a wall of sentences.
    analysis: Optional[dict] = None

    def to_dict(self) -> dict:
        """Serialize for API responses and audit entries."""
        return {
            "allowed": self.allowed,
            "blockers": self.blockers,
            "warnings": self.warnings,
            "checks_passed": self.checks_passed,
            "analysis": self.analysis,
        }


class SubmissionGate:
    """Evaluates whether an application may be submitted."""

    def __init__(self, db_session: Session):
        """
        Initialize the gate.

        Args:
            db_session: Database session
        """
        self.db_session = db_session
        self.threshold = settings.clean_submissions_threshold
        # describe() asks for both decisions, and both read the application.
        # The analysis opens and parses a PDF, so it is done once per gate.
        self._analyses: dict = {}

    # ------------------------------------------------------------------
    # Public checks
    # ------------------------------------------------------------------

    def check_user_directed(
        self,
        application: Application,
        accept_weak_fit: bool = False,
    ) -> SubmissionDecision:
        """
        Check a submission the user has explicitly asked for.

        The user is present and has seen the form, so the automation mode and
        clean-submission count are irrelevant. What still matters is that the
        application is actually complete and honest.

        Args:
            application: The application to submit
            accept_weak_fit: The user has read the analyst's judgement that
                this posting is a poor match and is sending anyway. Waives
                only findings the analyst marks overridable; a defect stays a
                defect however the user feels about it.

        Returns:
            SubmissionDecision
        """
        decision = SubmissionDecision(allowed=True)

        self._check_not_already_submitted(application, decision)
        self._check_required_answers(application, decision)
        self._check_documents_verified(application, decision)
        self._check_analyst_passed(application, decision, accept_weak_fit)

        decision.allowed = not decision.blockers

        return decision

    def check_auto(
        self,
        application: Application,
        platform_account: PlatformAccount,
        connector: Optional[object] = None,
    ) -> SubmissionDecision:
        """
        Check whether an application may be submitted unattended.

        Args:
            application: The application to submit
            platform_account: The platform it belongs to
            connector: Connector whose capabilities are consulted, if available

        Returns:
            SubmissionDecision
        """
        decision = SubmissionDecision(allowed=True)

        # Everything the user-directed path requires
        self._check_not_already_submitted(application, decision)
        self._check_required_answers(application, decision)
        self._check_documents_verified(application, decision)
        self._check_analyst_passed(application, decision)

        # Plus the unattended-only requirements
        self._check_automation_mode(platform_account, decision)
        self._check_connector_capability(connector, decision)
        self._check_clean_submissions(platform_account, decision)
        self._check_daily_limit(platform_account, decision)
        self._check_no_unanswered_sensitive(application, decision)

        decision.allowed = not decision.blockers

        if decision.allowed:
            logger.info(
                f"Auto-submit permitted for application #{application.id} on "
                f"{platform_account.platform}"
            )
        else:
            logger.info(
                f"Auto-submit refused for application #{application.id}: "
                f"{'; '.join(decision.blockers)}"
            )

        return decision

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _unconfirmed_attempts(self, application: Application) -> int:
        """
        How many times submit has been clicked on this application already.

        Args:
            application: The application

        Returns:
            Count of prior submit attempts recorded in the audit log
        """
        from job_agent.models.database import AuditLog

        try:
            return (
                self.db_session.query(AuditLog)
                .filter(
                    AuditLog.action == AuditAction.APPLICATION_SUBMITTED,
                    AuditLog.detail_json["application_id"].as_integer()
                    == application.id,
                )
                .count()
            )
        except Exception:
            return 0

    def _check_not_already_submitted(
        self, application: Application, decision: SubmissionDecision
    ) -> None:
        """Refuse to submit the same application twice."""
        if application.submission_status in (
            ApplicationStatus.SUBMITTED,
            ApplicationStatus.EMAIL_SENT,
        ):
            decision.blockers.append(
                f"Already submitted at "
                f"{application.submitted_at.isoformat() if application.submitted_at else 'unknown time'}"
            )
            return

        # An attempt that produced no confirmation is retryable — the send may
        # never have happened. But it may also have landed and only failed to
        # confirm, so the retry is warned about rather than waved through: two
        # applications to the same employer is not a harmless outcome.
        attempts = self._unconfirmed_attempts(application)

        if attempts:
            decision.warnings.append(
                f"Submit was already clicked {attempts} time(s) on this "
                f"application without a confirmation. If the employer did "
                f"receive it, sending again files a duplicate — check for an "
                f"acknowledgement email before releasing."
            )

        if application.submission_status == ApplicationStatus.REJECTED:
            decision.blockers.append("This application was discarded")
            return

        decision.checks_passed.append("not previously submitted")

    @staticmethod
    def _check_required_answers(
        application: Application, decision: SubmissionDecision
    ) -> None:
        """Every required question must have an answer."""
        deferred = application.deferred_fields or {}

        unanswered = [
            question for question, detail in deferred.items()
            if detail.get("required")
            and detail.get("value_entered_by_user") in (None, "")
        ]

        if unanswered:
            decision.blockers.append(
                f"{len(unanswered)} required question(s) unanswered: "
                f"{', '.join(unanswered)}"
            )
            return

        decision.checks_passed.append("all required questions answered")

    def _check_documents_verified(
        self, application: Application, decision: SubmissionDecision
    ) -> None:
        """
        Never submit a document containing claims its master doesn't support.

        Phase 4 flags tailored documents whose content isn't backed by the
        user's master resume. Sending one to an employer would put a fabricated
        claim in front of a hiring manager under the user's name.
        """
        version_ids = [
            vid for vid in (
                application.resume_version_id,
                application.cover_letter_version_id,
            ) if vid
        ]

        if not version_ids:
            decision.warnings.append("No tailored documents are attached")
            return

        versions = self.db_session.query(DocumentVersion).filter(
            DocumentVersion.id.in_(version_ids)
        ).all()

        for version in versions:
            if version.fabrication_flags:
                decision.blockers.append(
                    f"{version.doc_type.value} version #{version.id} contains "
                    f"{len(version.fabrication_flags)} claim(s) not supported by your "
                    f"master document — review it before sending"
                )

        if not decision.blockers:
            decision.checks_passed.append("attached documents verified against the master")

    def _check_analyst_passed(
        self,
        application: Application,
        decision: SubmissionDecision,
        accept_weak_fit: bool = False,
    ) -> None:
        """
        Read the finished application before letting it go to an employer.

        Every other check here asks whether the *process* was followed: was it
        approved, was it already sent, were the required boxes filled. None of
        them opens the resume. The analyst does, and its blockers are this
        gate's blockers — a resume with no email on it fails no procedural
        check and is not an application worth sending.

        The report is attached to the decision either way, so the tray can show
        what was noticed even when nothing was wrong.
        """
        if not settings.analyst_enabled:
            decision.warnings.append(
                "Pre-submission analysis is turned off — nothing read this "
                "application before sending"
            )
            return

        from job_agent.services.application_analyst import ApplicationAnalyst

        if application.id not in self._analyses:
            self._analyses[application.id] = ApplicationAnalyst(
                self.db_session
            ).analyse(application)

        report = self._analyses[application.id]
        decision.analysis = report.to_dict()

        for finding in report.blockers:
            if accept_weak_fit and finding.overridable:
                decision.warnings.append(
                    f"Sending despite the analyst's judgement: {finding.detail}"
                )
                continue

            decision.blockers.append(f"{finding.detail} {finding.fix}".strip())

        for finding in report.warnings:
            decision.warnings.append(f"{finding.detail} {finding.fix}".strip())

        if report.ready:
            decision.checks_passed.append(
                f"read end to end before sending ({len(report.checks_passed)} checks)"
            )

    @staticmethod
    def _check_automation_mode(
        platform_account: PlatformAccount, decision: SubmissionDecision
    ) -> None:
        """Auto-submit requires the user to have turned it on for this platform."""
        if platform_account.automation_mode != AutomationMode.SEARCH_FILL_SUBMIT:
            decision.blockers.append(
                f"Automation mode for {platform_account.platform} is "
                f"'{platform_account.automation_mode.value}', not 'search_fill_submit'"
            )
            return

        decision.checks_passed.append("automation mode allows submission")

    @staticmethod
    def _check_connector_capability(
        connector: Optional[object], decision: SubmissionDecision
    ) -> None:
        """The connector must declare that it can submit on this platform."""
        if connector is None:
            decision.warnings.append("No connector supplied; capability not verified")
            return

        capabilities = getattr(connector, "capabilities", None)

        if not capabilities or not getattr(capabilities, "can_submit_automatically", False):
            decision.blockers.append(
                f"The {getattr(connector, 'platform_name', 'platform')} connector does "
                f"not support automatic submission"
            )
            return

        if getattr(capabilities, "tos_risk_note", None):
            decision.warnings.append(capabilities.tos_risk_note)

        decision.checks_passed.append("connector supports submission")

    def _check_clean_submissions(
        self, platform_account: PlatformAccount, decision: SubmissionDecision
    ) -> None:
        """
        Require a proven track record on this platform first.

        The count only rises on applications a human reviewed and that
        submitted cleanly — see SubmissionRecorder.record_success.
        """
        count = platform_account.clean_submissions_count or 0

        if count < self.threshold:
            decision.blockers.append(
                f"Only {count} of {self.threshold} reviewed submissions on "
                f"{platform_account.platform} so far — the first {self.threshold} "
                f"always go to review"
            )
            return

        decision.checks_passed.append(
            f"{count} reviewed submissions on this platform (threshold {self.threshold})"
        )

    def _check_daily_limit(
        self, platform_account: PlatformAccount, decision: SubmissionDecision
    ) -> None:
        """Stop at the platform's daily application cap."""
        submitted_today = self.count_submitted_today(platform_account.platform)
        limit = platform_account.daily_apply_limit or 0

        if submitted_today >= limit:
            decision.blockers.append(
                f"Daily apply limit reached for {platform_account.platform} "
                f"({submitted_today}/{limit})"
            )
            return

        decision.checks_passed.append(
            f"within the daily limit ({submitted_today}/{limit} used)"
        )

    @staticmethod
    def _check_no_unanswered_sensitive(
        application: Application, decision: SubmissionDecision
    ) -> None:
        """
        Note unanswered sensitive questions, without blocking on them.

        Optional demographic and veteran questions are legitimately left blank —
        declining to answer is a valid choice and the common default. But the
        user should see that an unattended submission left them blank.
        """
        deferred = application.deferred_fields or {}

        blank_sensitive = [
            question for question, detail in deferred.items()
            if detail.get("category") == "sensitive"
            and detail.get("value_entered_by_user") in (None, "")
        ]

        if blank_sensitive:
            decision.warnings.append(
                f"{len(blank_sensitive)} optional sensitive question(s) will be "
                f"submitted blank: {', '.join(blank_sensitive)}"
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def count_submitted_today(self, platform: str) -> int:
        """
        Count applications submitted on a platform since midnight UTC.

        Args:
            platform: Platform name

        Returns:
            Number submitted today
        """
        midnight = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

        return (
            self.db_session.query(Application)
            .join(PlatformAccount, Application.platform_account_id == PlatformAccount.id)
            .filter(
                PlatformAccount.platform == platform,
                Application.submission_status == ApplicationStatus.SUBMITTED,
                Application.submitted_at >= midnight,
            )
            .count()
        )

    def describe(
        self,
        application: Application,
        platform_account: PlatformAccount,
        connector: Optional[object] = None,
    ) -> dict:
        """
        Explain the current submission position for an application.

        Args:
            application: The application
            platform_account: Its platform
            connector: Optional connector for capability checks

        Returns:
            Both decisions plus the gate's counters
        """
        job = self.db_session.query(Job).filter(Job.id == application.job_id).first()

        return {
            "application_id": application.id,
            "job_title": job.title if job else None,
            "company": job.company if job else None,
            "platform": platform_account.platform,
            "automation_mode": platform_account.automation_mode.value,
            "clean_submissions_count": platform_account.clean_submissions_count,
            "clean_submissions_threshold": self.threshold,
            "submitted_today": self.count_submitted_today(platform_account.platform),
            "daily_apply_limit": platform_account.daily_apply_limit,
            "user_directed": self.check_user_directed(application).to_dict(),
            "auto": self.check_auto(application, platform_account, connector).to_dict(),
        }
