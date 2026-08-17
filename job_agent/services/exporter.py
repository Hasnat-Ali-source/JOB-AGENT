"""
CSV Export (Phase 10).

Exports jobs, applications and the audit log so the user's data leaves this
tool as easily as it entered it. Everything the agent has done is theirs: a
local SQLite file is only useful if it can be opened in a spreadsheet, handed
to someone, or kept after the tool is deleted.

Rows stream through `csv.writer` rather than being accumulated in memory, so a
year of audit entries exports without loading them all at once.
"""

import csv
import io
import logging
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional

from sqlalchemy.orm import Session

from job_agent.models.database import (
    AgentRun,
    Application,
    AuditLog,
    DocumentVersion,
    EmailDraft,
    Job,
    PlatformAccount,
)

logger = logging.getLogger(__name__)


def _iso(value: Optional[datetime]) -> str:
    """Format a datetime for CSV, or empty."""
    return value.isoformat(sep=" ", timespec="seconds") if value else ""


def _clean(value: Any) -> str:
    """
    Flatten a value into a single CSV cell.

    Newlines are collapsed because a job description with line breaks turns one
    row into many in most spreadsheet apps.
    """
    if value is None:
        return ""

    if isinstance(value, bool):
        return "yes" if value else "no"

    text = str(value)

    return " ".join(text.split())


class Exporter:
    """Builds CSV exports from the database."""

    def __init__(self, db_session: Session):
        """
        Initialize the exporter.

        Args:
            db_session: Database session
        """
        self.db_session = db_session

    # ------------------------------------------------------------------
    # Applications
    # ------------------------------------------------------------------

    APPLICATION_COLUMNS = [
        "application_id", "job_id", "platform", "company", "title", "location",
        "salary", "fit_score", "status", "reviewed_by_user", "filled_at",
        "submitted_at", "confirmation_ref", "confirmation_url", "recipient_email",
        "resume_pdf", "cover_letter_pdf", "documents_verified",
        "unanswered_required", "form_url", "user_notes",
    ]

    def applications(self) -> Iterator[List[str]]:
        """
        Export every application.

        Yields:
            The header row, then one row per application
        """
        yield self.APPLICATION_COLUMNS

        applications = (
            self.db_session.query(Application)
            .order_by(Application.created_at.desc(), Application.id.desc())
            .all()
        )

        jobs = self._by_id(Job, [a.job_id for a in applications])
        accounts = self._by_id(
            PlatformAccount, [a.platform_account_id for a in applications]
        )

        for application in applications:
            job = jobs.get(application.job_id)
            account = accounts.get(application.platform_account_id)

            deferred = application.deferred_fields or {}
            unanswered = [
                question for question, detail in deferred.items()
                if detail.get("required")
                and detail.get("value_entered_by_user") in (None, "")
            ]

            yield [
                _clean(application.id),
                _clean(application.job_id),
                _clean(account.platform if account else ""),
                _clean(job.company if job else ""),
                _clean(job.title if job else ""),
                _clean(job.location if job else ""),
                _clean(job.salary if job else ""),
                _clean(round(job.fit_score, 3) if job and job.fit_score else ""),
                _clean(application.submission_status.value),
                _clean(application.reviewed_by_user),
                _iso(application.filled_at),
                _iso(application.submitted_at),
                _clean(application.confirmation_ref),
                _clean(application.confirmation_url),
                _clean(application.recipient_email),
                _clean(application.resume_version),
                _clean(application.cover_letter_version),
                _clean(self._documents_verified(application)),
                _clean("; ".join(unanswered)),
                _clean(application.form_url),
                _clean(application.user_notes),
            ]

    def _documents_verified(self, application: Application) -> Optional[bool]:
        """
        Whether every attached document passed the fabrication check.

        Returns:
            True/False, or None when nothing is attached
        """
        version_ids = [
            vid for vid in (
                application.resume_version_id,
                application.cover_letter_version_id,
            ) if vid
        ]

        if not version_ids:
            return None

        versions = self.db_session.query(DocumentVersion).filter(
            DocumentVersion.id.in_(version_ids)
        ).all()

        return all(v.is_verified for v in versions)

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    JOB_COLUMNS = [
        "job_id", "platform", "external_id", "title", "company", "location",
        "job_type", "salary", "fit_score", "hard_filter_pass", "status",
        "apply_method", "recruiter_contact", "posted_at", "first_seen_at", "url",
    ]

    def jobs(
        self,
        platform: Optional[str] = None,
        include_filtered: bool = True,
    ) -> Iterator[List[str]]:
        """
        Export discovered jobs.

        Args:
            platform: Restrict to one platform
            include_filtered: Include jobs that failed the hard filters

        Yields:
            The header row, then one row per job
        """
        yield self.JOB_COLUMNS

        query = self.db_session.query(Job)

        if platform:
            query = query.filter(Job.platform == platform)

        if not include_filtered:
            query = query.filter(Job.hard_filter_pass == True)  # noqa: E712

        # id breaks ties so repeated exports are byte-identical
        for job in query.order_by(Job.first_seen_at.desc(), Job.id.desc()).all():
            yield [
                _clean(job.id),
                _clean(job.platform),
                _clean(job.external_id),
                _clean(job.title),
                _clean(job.company),
                _clean(job.location),
                _clean(job.job_type),
                _clean(job.salary),
                _clean(round(job.fit_score, 3) if job.fit_score else ""),
                _clean(job.hard_filter_pass),
                _clean(job.status),
                _clean(job.apply_method),
                _clean(job.recruiter_contact),
                _iso(job.posted_at),
                _iso(job.first_seen_at),
                _clean((job.raw_data or {}).get("url")),
            ]

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    AUDIT_COLUMNS = [
        "timestamp", "action", "platform", "actor", "result", "detail",
        "error_message", "detail_json",
    ]

    def audit(
        self,
        platform: Optional[str] = None,
        action: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> Iterator[List[str]]:
        """
        Export the audit log.

        Args:
            platform: Filter by platform
            action: Filter by action type
            since: Only entries at or after this time
            until: Only entries at or before this time

        Yields:
            The header row, then one row per entry
        """
        yield self.AUDIT_COLUMNS

        for entry in self.audit_query(platform, action, since, until).all():
            yield [
                _iso(entry.timestamp),
                _clean(entry.action.value),
                _clean(entry.platform),
                _clean(entry.actor),
                _clean(entry.result),
                _clean(entry.detail),
                _clean(entry.error_message),
                _clean(entry.detail_json) if entry.detail_json else "",
            ]

    def audit_query(
        self,
        platform: Optional[str] = None,
        action: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ):
        """
        Build the filtered audit query, shared by export and the dashboard view.

        Args:
            platform: Filter by platform
            action: Filter by action type
            since: Lower time bound
            until: Upper time bound

        Returns:
            A SQLAlchemy query, newest first
        """
        query = self.db_session.query(AuditLog)

        if platform:
            query = query.filter(AuditLog.platform == platform)

        if action:
            query = query.filter(AuditLog.action == action)

        if since:
            query = query.filter(AuditLog.timestamp >= since)

        if until:
            query = query.filter(AuditLog.timestamp <= until)

        return query.order_by(AuditLog.timestamp.desc(), AuditLog.id.desc())

    # ------------------------------------------------------------------
    # Runs and emails
    # ------------------------------------------------------------------

    RUN_COLUMNS = [
        "run_id", "trigger", "status", "started_at", "finished_at",
        "duration_seconds", "platforms_run", "platforms_skipped", "jobs_found",
        "new_jobs", "duplicates_skipped", "hard_filters_failed",
        "documents_generated", "applications_queued", "applications_submitted",
        "interruptions", "errors",
    ]

    def runs(self) -> Iterator[List[str]]:
        """
        Export run history.

        Yields:
            The header row, then one row per run
        """
        yield self.RUN_COLUMNS

        for run in self.db_session.query(AgentRun).order_by(
            AgentRun.started_at.desc(), AgentRun.id.desc()
        ).all():
            yield [
                _clean(run.id),
                _clean(run.trigger),
                _clean(run.status.value),
                _iso(run.started_at),
                _iso(run.finished_at),
                _clean(round(run.duration_seconds, 1) if run.duration_seconds else ""),
                _clean(", ".join(run.platforms_run or [])),
                _clean("; ".join(
                    f"{p}: {r}" for p, r in (run.platforms_skipped or {}).items()
                )),
                _clean(run.jobs_found),
                _clean(run.new_jobs),
                _clean(run.duplicates_skipped),
                _clean(run.hard_filters_failed),
                _clean(run.documents_generated),
                _clean(run.applications_queued),
                _clean(run.applications_submitted),
                _clean("; ".join(i.get("kind", "") for i in (run.interruptions or []))),
                _clean("; ".join(run.errors or [])),
            ]

    EMAIL_COLUMNS = [
        "draft_id", "job_id", "to_email", "subject", "status", "reviewed_by_user",
        "send_method", "sent_at", "message_id", "attachments", "error_message",
    ]

    def emails(self) -> Iterator[List[str]]:
        """
        Export email application drafts.

        Yields:
            The header row, then one row per draft
        """
        yield self.EMAIL_COLUMNS

        for draft in self.db_session.query(EmailDraft).order_by(
            EmailDraft.created_at.desc(), EmailDraft.id.desc()
        ).all():
            yield [
                _clean(draft.id),
                _clean(draft.job_id),
                _clean(draft.to_email),
                _clean(draft.subject),
                _clean(draft.status.value),
                _clean(draft.reviewed_by_user),
                _clean(draft.send_method),
                _iso(draft.sent_at),
                _clean(draft.sent_message_id),
                _clean("; ".join(draft.attachments or [])),
                _clean(draft.error_message),
            ]

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    @staticmethod
    def to_csv(rows: Iterator[List[str]]) -> str:
        """
        Render rows as CSV text.

        Args:
            rows: Header row followed by data rows

        Returns:
            CSV content
        """
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")

        for row in rows:
            writer.writerow(row)

        return buffer.getvalue()

    def available(self) -> Dict[str, List[str]]:
        """
        The exports on offer and their columns.

        Returns:
            {name: columns}
        """
        return {
            "applications": self.APPLICATION_COLUMNS,
            "jobs": self.JOB_COLUMNS,
            "audit": self.AUDIT_COLUMNS,
            "runs": self.RUN_COLUMNS,
            "emails": self.EMAIL_COLUMNS,
        }

    def rows_for(self, name: str, **filters) -> Iterator[List[str]]:
        """
        Get the rows for a named export.

        Args:
            name: Export name
            **filters: Passed to the underlying method

        Returns:
            Row iterator

        Raises:
            ValueError: If the export name isn't recognized
        """
        exports = {
            "applications": lambda: self.applications(),
            "jobs": lambda: self.jobs(**filters),
            "audit": lambda: self.audit(**filters),
            "runs": lambda: self.runs(),
            "emails": lambda: self.emails(),
        }

        if name not in exports:
            raise ValueError(
                f"Unknown export '{name}' — choose from {', '.join(exports)}"
            )

        return exports[name]()

    def _by_id(self, model, ids: List[int]) -> Dict[int, Any]:
        """
        Load records of a model by id.

        Args:
            model: SQLModel class
            ids: Ids to load

        Returns:
            {id: record}
        """
        unique = [i for i in set(ids) if i is not None]

        if not unique:
            return {}

        return {
            record.id: record
            for record in self.db_session.query(model).filter(model.id.in_(unique)).all()
        }
