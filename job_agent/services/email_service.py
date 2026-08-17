"""
Email Application Service (Phase 6b).

Owns the lifecycle of an application email:

    detect recipient → compose draft → user reviews → user approves → send
                                                                      ↓
                                                          EmailThread → poll for replies

Two rules are enforced here rather than left to callers:

- **A draft can only be sent after the user approved it.** There is no code
  path from DRAFT to SENT. The automation mode is irrelevant: an email leaves
  the user's own address, in their name, to a person, and cannot be unsent.
- **Sending is rate-limited per hour** (`settings.email_rate_limit`). A loop
  bug that fires a hundred applications at one company in a minute would be
  unrecoverable reputationally, so the cap is checked at send time rather than
  trusted to the caller.
"""

import imaplib
import email as email_lib
import logging
from datetime import timedelta
from email.utils import parseaddr
from typing import List, Optional

from sqlalchemy.orm import Session

from job_agent.config import settings
from job_agent.models.database import (
    Application,
    ApplicationStatus,
    AuditAction,
    AuditLog,
    CandidateProfile,
    EmailDraft,
    EmailDraftStatus,
    EmailThread,
    EmailThreadStatus,
    Job,
)
from job_agent.services.email_composer import EmailComposer
from job_agent.services.email_detector import detect_application_email
from job_agent.services.email_sender import EmailSender, SendResult
from job_agent.utils.dates import utcnow

logger = logging.getLogger(__name__)


class EmailApplicationError(Exception):
    """Raised when an email application cannot proceed."""


class EmailApplicationService:
    """Drafts, sends, and tracks application emails."""

    def __init__(self, db_session: Session, sender: Optional[EmailSender] = None):
        """
        Initialize the service.

        Args:
            db_session: Database session
            sender: Sender to use (tests inject a fake)
        """
        self.db_session = db_session
        self.sender = sender or EmailSender()

    # ------------------------------------------------------------------
    # Drafting
    # ------------------------------------------------------------------

    def create_draft(
        self,
        job: Job,
        to_email: Optional[str] = None,
        application: Optional[Application] = None,
    ) -> EmailDraft:
        """
        Compose an application email and queue it for review.

        Args:
            job: The job to apply for
            to_email: Recipient override; detected from the posting when absent
            application: Linked application record, if one exists

        Returns:
            The stored draft, always in DRAFT status

        Raises:
            EmailApplicationError: If no recipient can be determined or the
                address is invalid
        """
        recipient_source = "supplied by you"

        if to_email:
            recipient = to_email.strip()
        else:
            candidate = detect_application_email(job)

            if not candidate:
                raise EmailApplicationError(
                    f"No application email address found in the posting for "
                    f"'{job.title}'. Supply one explicitly if you know it."
                )

            recipient = candidate.email
            recipient_source = "; ".join(candidate.reasons) or "found in the posting"

            self._log(
                AuditAction.EMAIL_RECIPIENT_DETECTED,
                f"Detected {recipient} as the application address for '{job.title}'",
                {
                    "job_id": job.id,
                    "email": recipient,
                    "score": candidate.score,
                    "reasons": candidate.reasons,
                },
            )

        if not EmailSender.is_valid_address(recipient):
            raise EmailApplicationError(f"'{recipient}' is not a valid email address")

        profile = (
            self.db_session.query(CandidateProfile)
            .filter(CandidateProfile.is_active == True)  # noqa: E712
            .first()
        )

        composer = EmailComposer(self.db_session)
        resume_version, cover_letter_version = composer.latest_documents(job)

        composed = composer.compose(
            job, recipient, profile, resume_version, cover_letter_version
        )

        draft = EmailDraft(
            application_id=application.id if application else None,
            job_id=job.id,
            to_email=composed.to_email,
            subject=composed.subject,
            body=composed.body,
            attachments=composed.attachments,
            recipient_source=recipient_source,
            status=EmailDraftStatus.DRAFT,
        )

        self.db_session.add(draft)
        self.db_session.commit()
        self.db_session.refresh(draft)

        self._log(
            AuditAction.EMAIL_DRAFTED,
            f"Drafted an application email to {recipient} for '{job.title}' "
            f"at {job.company}",
            {
                "draft_id": draft.id,
                "job_id": job.id,
                "attachments": composed.attachments,
                "warnings": composed.warnings,
                "unverified_documents": composed.unverified_documents,
            },
            result="paused",  # Waiting on the user, always
        )

        logger.info(
            f"Email draft #{draft.id} to {recipient} awaits review "
            f"({len(composed.warnings)} warning(s))"
        )

        return draft

    def approve(self, draft: EmailDraft, notes: Optional[str] = None) -> EmailDraft:
        """
        Record the user's approval to send a draft.

        Args:
            draft: The draft
            notes: Optional reviewer notes

        Returns:
            The updated draft

        Raises:
            EmailApplicationError: If the draft isn't in a state that can be approved
        """
        if draft.status == EmailDraftStatus.SENT:
            raise EmailApplicationError("This email has already been sent")

        if draft.status == EmailDraftStatus.DISCARDED:
            raise EmailApplicationError("This draft was discarded")

        draft.status = EmailDraftStatus.APPROVED
        draft.reviewed_by_user = True
        draft.updated_at = utcnow()

        if notes:
            draft.user_notes = notes

        self.db_session.commit()
        self.db_session.refresh(draft)

        self._log(
            AuditAction.EMAIL_APPROVED,
            f"User approved sending the application email to {draft.to_email}",
            {"draft_id": draft.id, "job_id": draft.job_id},
            actor="user",
        )

        return draft

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def send(self, draft: EmailDraft) -> EmailDraft:
        """
        Send an approved draft.

        Args:
            draft: The draft to send

        Returns:
            The updated draft

        Raises:
            EmailApplicationError: If the draft was not approved, was already
                sent, or the hourly rate limit is reached
        """
        if draft.status == EmailDraftStatus.SENT:
            raise EmailApplicationError(
                f"Already sent at {draft.sent_at.isoformat() if draft.sent_at else 'unknown time'}"
            )

        if draft.status != EmailDraftStatus.APPROVED or not draft.reviewed_by_user:
            raise EmailApplicationError(
                "This draft has not been approved — read it and approve before sending"
            )

        remaining = self.remaining_hourly_quota()

        if remaining <= 0:
            raise EmailApplicationError(
                f"Hourly email limit reached ({settings.email_rate_limit}/hour). "
                f"Try again later."
            )

        result = self.sender.send(
            draft.to_email, draft.subject, draft.body, list(draft.attachments or [])
        )

        return self._record_send(draft, result)

    def _record_send(self, draft: EmailDraft, result: SendResult) -> EmailDraft:
        """
        Persist the outcome of a send attempt.

        Args:
            draft: The draft
            result: What the sender reported

        Returns:
            The updated draft
        """
        job = self.db_session.query(Job).filter(Job.id == draft.job_id).first()
        job_label = f"'{job.title}' at {job.company}" if job else f"job {draft.job_id}"

        if not result.sent:
            draft.status = EmailDraftStatus.FAILED
            draft.error_message = result.error_message
            draft.updated_at = utcnow()
            self.db_session.commit()

            self._log(
                AuditAction.EMAIL_SEND_FAILED,
                f"Could not send the application email to {draft.to_email} "
                f"for {job_label}: {result.error_message}",
                {"draft_id": draft.id, "error": result.error_message},
                result="failure",
            )

            logger.error(f"Email draft #{draft.id} failed to send: {result.error_message}")

            return draft

        draft.status = EmailDraftStatus.SENT
        draft.send_method = result.method
        draft.sent_message_id = result.message_id
        draft.sent_at = utcnow()
        draft.error_message = None
        draft.updated_at = utcnow()

        thread = EmailThread(
            application_id=draft.application_id or 0,
            sent_message_id=result.message_id or f"draft-{draft.id}",
            recipient_email=draft.to_email,
            subject=draft.subject,
            sent_at=draft.sent_at,
            thread_status=EmailThreadStatus.AWAITING_REPLY,
        )
        self.db_session.add(thread)

        if draft.application_id:
            application = self.db_session.query(Application).filter(
                Application.id == draft.application_id
            ).first()

            if application:
                application.submission_status = ApplicationStatus.EMAIL_SENT
                application.submitted_at = draft.sent_at
                application.recipient_email = draft.to_email
                application.email_message_id = result.message_id
                application.updated_at = utcnow()

        self.db_session.commit()
        self.db_session.refresh(draft)

        self._log(
            AuditAction.EMAIL_SENT,
            f"Sent the application email to {draft.to_email} for {job_label} "
            f"via {result.method}",
            {
                "draft_id": draft.id,
                "job_id": draft.job_id,
                "message_id": result.message_id,
                "method": result.method,
                "attachments": draft.attachments,
            },
            actor="user",  # Only ever reached from an approved draft
        )

        logger.info(f"Email draft #{draft.id} sent via {result.method}")

        return draft

    def discard(self, draft: EmailDraft, reason: Optional[str] = None) -> EmailDraft:
        """
        Discard a draft without sending.

        Args:
            draft: The draft
            reason: Optional reason

        Returns:
            The updated draft

        Raises:
            EmailApplicationError: If it was already sent
        """
        if draft.status == EmailDraftStatus.SENT:
            raise EmailApplicationError("This email was already sent and cannot be discarded")

        draft.status = EmailDraftStatus.DISCARDED
        draft.reviewed_by_user = True
        draft.user_notes = reason or draft.user_notes
        draft.updated_at = utcnow()

        self.db_session.commit()
        self.db_session.refresh(draft)

        return draft

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def sent_last_hour(self) -> int:
        """
        Count emails sent in the past hour.

        Returns:
            Number sent
        """
        cutoff = utcnow() - timedelta(hours=1)

        return (
            self.db_session.query(EmailDraft)
            .filter(
                EmailDraft.status == EmailDraftStatus.SENT,
                EmailDraft.sent_at >= cutoff,
            )
            .count()
        )

    def remaining_hourly_quota(self) -> int:
        """
        How many more emails may be sent this hour.

        Returns:
            Remaining quota, never negative
        """
        return max(0, settings.email_rate_limit - self.sent_last_hour())

    # ------------------------------------------------------------------
    # Reply monitoring
    # ------------------------------------------------------------------

    def check_replies(self, imap_password: Optional[str] = None) -> List[EmailThread]:
        """
        Poll the inbox for replies to sent applications.

        Matches on the sender address of threads still awaiting a reply. A
        reply is linked back to its application and surfaced in the dashboard.

        Args:
            imap_password: App-specific password; read from the Keychain when
                not supplied

        Returns:
            Threads updated with a reply

        Raises:
            EmailApplicationError: If IMAP is not configured or unreachable
        """
        host = getattr(settings, "imap_host", None)
        username = getattr(settings, "imap_username", None) or getattr(
            settings, "smtp_username", None
        )

        if not host or not username:
            raise EmailApplicationError(
                "IMAP is not configured — set IMAP_HOST and IMAP_USERNAME"
            )

        if imap_password is None:
            from job_agent.utils.keychain import retrieve_app_password

            imap_password = retrieve_app_password("imap", username) or retrieve_app_password(
                "smtp", username
            )

        if not imap_password:
            raise EmailApplicationError(
                f"No app-specific password in the Keychain for {username}"
            )

        awaiting = (
            self.db_session.query(EmailThread)
            .filter(EmailThread.thread_status == EmailThreadStatus.AWAITING_REPLY)
            .all()
        )

        if not awaiting:
            return []

        by_sender = {thread.recipient_email.lower(): thread for thread in awaiting}
        updated: List[EmailThread] = []

        try:
            with imaplib.IMAP4_SSL(host, getattr(settings, "imap_port", 993)) as client:
                client.login(username, imap_password)
                client.select("INBOX")

                oldest = min(thread.sent_at for thread in awaiting)
                since = oldest.strftime("%d-%b-%Y")

                status, data = client.search(None, f'(SINCE "{since}")')

                if status != "OK":
                    return []

                for message_id in (data[0] or b"").split():
                    status, payload = client.fetch(message_id, "(RFC822)")

                    if status != "OK" or not payload or not payload[0]:
                        continue

                    message = email_lib.message_from_bytes(payload[0][1])
                    sender = parseaddr(message.get("From", ""))[1].lower()

                    thread = by_sender.get(sender)
                    if not thread or thread.reply_received_at:
                        continue

                    thread.reply_received_at = utcnow()
                    thread.reply_snippet = self._snippet(message)
                    thread.thread_status = EmailThreadStatus.REPLY_RECEIVED
                    updated.append(thread)

                    self._log(
                        AuditAction.EMAIL_REPLY_RECEIVED,
                        f"Reply received from {sender}",
                        {
                            "thread_id": thread.id,
                            "application_id": thread.application_id,
                            "snippet": thread.reply_snippet,
                        },
                    )

        except imaplib.IMAP4.error as e:
            raise EmailApplicationError(f"IMAP error: {e}")

        if updated:
            self.db_session.commit()
            logger.info(f"Linked {len(updated)} reply(ies) to sent applications")

        return updated

    @staticmethod
    def _snippet(message, length: int = 200) -> str:
        """
        Extract the first readable text from a reply.

        Args:
            message: Parsed email message
            length: Maximum characters

        Returns:
            Snippet text
        """
        try:
            if message.is_multipart():
                for part in message.walk():
                    if part.get_content_type() == "text/plain":
                        payload = part.get_payload(decode=True) or b""
                        text = payload.decode("utf-8", errors="replace")
                        break
                else:
                    text = ""
            else:
                payload = message.get_payload(decode=True) or b""
                text = payload.decode("utf-8", errors="replace")
        except Exception:
            text = ""

        return " ".join(text.split())[:length]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _log(
        self,
        action: AuditAction,
        detail: str,
        detail_json: dict,
        actor: str = "agent",
        result: str = "success",
    ) -> None:
        """Write an audit entry."""
        self.db_session.add(
            AuditLog(
                timestamp=utcnow(),
                action=action,
                actor=actor,
                detail=detail,
                detail_json=detail_json,
                result=result,
            )
        )
        self.db_session.commit()
