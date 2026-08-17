"""
Submission Recorder (Phase 6).

Writes the consequences of a submission attempt: the application's status, the
confirmation reference, the audit trail, and — only when it is earned — the
platform's clean-submission count.

The counter is the gate's memory, so what increments it is the whole design:

- The application must have been **reviewed by a human**. The gate exists to
  prove the connector fills forms correctly under human eyes; letting
  auto-submitted applications raise the count would let it certify itself.
- The submission must have been **confirmed**. An unverified submission isn't a
  clean one.
- It must have had **no validation errors**.
"""

import logging
from typing import Optional

from sqlalchemy.orm import Session

from job_agent.models.database import (
    Application,
    ApplicationStatus,
    AuditAction,
    AuditLog,
    Job,
    PlatformAccount,
)
from job_agent.services.submitter import SubmissionOutcome
from job_agent.utils.dates import utcnow

logger = logging.getLogger(__name__)


class SubmissionRecorder:
    """Records submission attempts and maintains the clean-submission count."""

    def __init__(self, db_session: Session):
        """
        Initialize the recorder.

        Args:
            db_session: Database session
        """
        self.db_session = db_session

    def record(
        self,
        application: Application,
        platform_account: PlatformAccount,
        outcome: SubmissionOutcome,
        initiated_by: str = "user",
    ) -> Application:
        """
        Persist the result of a submission attempt.

        Args:
            application: The application that was submitted
            platform_account: Its platform
            outcome: What the submitter observed
            initiated_by: "user" or "agent"

        Returns:
            The updated application
        """
        job = self.db_session.query(Job).filter(Job.id == application.job_id).first()
        job_label = f"'{job.title}' at {job.company}" if job else f"job {application.job_id}"

        if not outcome.submitted:
            return self._record_failure(application, platform_account, outcome, job_label)

        # Clicking submit is not the same as the employer receiving anything.
        # When nothing confirms the send and the form is still sitting there,
        # recording SUBMITTED tells the user a lie and — worse — locks the
        # application so the send can never be retried. It stays reviewable.
        # confirmation_url is only ever page.url, so it is set on every
        # attempt including the ones that went nowhere. Confirmation is the
        # only thing that distinguishes a send from a click.
        if not outcome.confirmed:
            return self._record_unconfirmed(
                application, platform_account, outcome, job_label, initiated_by
            )

        application.submission_status = ApplicationStatus.SUBMITTED
        application.submitted_at = utcnow()
        application.confirmation_ref = outcome.confirmation_ref
        application.confirmation_url = outcome.confirmation_url
        application.screenshot_path = outcome.after_screenshot or application.screenshot_path
        application.updated_at = utcnow()

        counted = self._maybe_count_clean(application, platform_account, outcome)

        self.db_session.commit()
        self.db_session.refresh(application)

        self._log(
            AuditAction.APPLICATION_SUBMITTED,
            f"Submitted application for {job_label}"
            + (f" (ref {outcome.confirmation_ref})" if outcome.confirmation_ref else "")
            + ("" if outcome.confirmed else " — CONFIRMATION NOT FOUND"),
            {
                "application_id": application.id,
                "job_id": application.job_id,
                "initiated_by": initiated_by,
                "counted_toward_clean_submissions": counted,
                "clean_submissions_count": platform_account.clean_submissions_count,
                **outcome.to_dict(),
            },
            platform=platform_account.platform,
            actor=initiated_by,
            result="success" if outcome.is_clean else "partial",
        )

        if not outcome.confirmed:
            logger.warning(
                f"Application #{application.id} was submitted but no confirmation was "
                f"found — check {outcome.confirmation_url} before assuming it landed"
            )

        return application

    def _record_unconfirmed(
        self,
        application: Application,
        platform_account: PlatformAccount,
        outcome: SubmissionOutcome,
        job_label: str,
        initiated_by: str,
    ) -> Application:
        """
        Record a submit click that produced no evidence of being received.

        The application is left in the review queue so it can be sent again
        once the cause is fixed. It never counts toward the clean-submission
        record, because nothing was shown to have gone anywhere.

        Args:
            application: The application
            platform_account: Its platform
            outcome: What the submitter observed
            job_label: Human-readable job description
            initiated_by: "user" or "agent"

        Returns:
            The application, still queued for review
        """
        application.submission_status = ApplicationStatus.QUEUED_FOR_REVIEW
        application.screenshot_path = (
            outcome.after_screenshot or application.screenshot_path
        )
        application.updated_at = utcnow()

        self.db_session.commit()
        self.db_session.refresh(application)

        self._log(
            AuditAction.APPLICATION_SUBMITTED,
            f"Submit was clicked for {job_label} but nothing confirmed the send — "
            f"left in the tray so it can be sent again",
            {
                "application_id": application.id,
                "job_id": application.job_id,
                "initiated_by": initiated_by,
                **outcome.to_dict(),
            },
            platform=platform_account.platform,
            actor=initiated_by,
            result="partial",
        )

        logger.warning(
            f"Application #{application.id}: submit clicked, no confirmation — "
            f"kept in the review queue rather than marked submitted"
        )

        return application

    def _record_failure(
        self,
        application: Application,
        platform_account: PlatformAccount,
        outcome: SubmissionOutcome,
        job_label: str,
    ) -> Application:
        """
        Record an attempt that never submitted.

        The application stays in the queue so the user can retry rather than
        silently losing the work.
        """
        application.updated_at = utcnow()
        self.db_session.commit()

        self._log(
            AuditAction.ERROR_OCCURRED,
            f"Submission failed for {job_label}: {outcome.error_message}",
            {"application_id": application.id, **outcome.to_dict()},
            platform=platform_account.platform,
            result="failure",
        )

        logger.error(
            f"Application #{application.id} not submitted: {outcome.error_message}"
        )

        return application

    def _maybe_count_clean(
        self,
        application: Application,
        platform_account: PlatformAccount,
        outcome: SubmissionOutcome,
    ) -> bool:
        """
        Advance the platform's track record, if this submission earned it.

        Args:
            application: The submitted application
            platform_account: Its platform
            outcome: What the submitter observed

        Returns:
            True if the count was incremented
        """
        if not outcome.is_clean:
            logger.info(
                f"Not counting application #{application.id} toward "
                f"{platform_account.platform}'s clean submissions: "
                f"{'unconfirmed' if not outcome.confirmed else 'validation errors'}"
            )
            return False

        if not application.reviewed_by_user:
            # An auto-submitted application must not raise the threshold that
            # permitted it in the first place.
            logger.info(
                f"Not counting application #{application.id}: it was not reviewed "
                f"by a human"
            )
            return False

        platform_account.clean_submissions_count = (
            platform_account.clean_submissions_count or 0
        ) + 1
        platform_account.updated_at = utcnow()

        logger.info(
            f"{platform_account.platform} clean submissions now "
            f"{platform_account.clean_submissions_count}"
        )

        return True

    def _log(
        self,
        action: AuditAction,
        detail: str,
        detail_json: dict,
        platform: Optional[str] = None,
        actor: str = "agent",
        result: str = "success",
    ) -> None:
        """Write an audit entry."""
        self.db_session.add(
            AuditLog(
                timestamp=utcnow(),
                action=action,
                platform=platform,
                actor=actor,
                detail=detail,
                detail_json=detail_json,
                result=result,
            )
        )
        self.db_session.commit()
