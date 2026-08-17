"""
Email Composer (Phase 6b).

Builds the application email: subject, body, and attachments.

The body comes from the tailored cover letter produced in Phase 4, which has
already been verified against the user's master document. Nothing new is
written here — composing prose about the candidate at send time would bypass
that verification entirely. When no cover letter exists, a short factual note
is assembled from the candidate profile and the job, containing no claims about
the candidate beyond their name and the role they're applying for.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from sqlalchemy.orm import Session

from job_agent.models.database import (
    CandidateProfile,
    DocumentType,
    DocumentVersion,
    Job,
)

logger = logging.getLogger(__name__)


@dataclass
class ComposedEmail:
    """A drafted application email."""

    to_email: str
    subject: str
    body: str
    attachments: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    unverified_documents: List[int] = field(default_factory=list)

    @property
    def is_sendable(self) -> bool:
        """True when nothing blocks this from being sent after approval."""
        return bool(self.to_email and self.subject and self.body.strip())


class EmailComposer:
    """Composes application emails from verified material."""

    def __init__(self, db_session: Session):
        """
        Initialize the composer.

        Args:
            db_session: Database session
        """
        self.db_session = db_session

    def compose(
        self,
        job: Job,
        to_email: str,
        profile: Optional[CandidateProfile] = None,
        resume_version: Optional[DocumentVersion] = None,
        cover_letter_version: Optional[DocumentVersion] = None,
    ) -> ComposedEmail:
        """
        Compose an application email.

        Args:
            job: The job being applied for
            to_email: Recipient address
            profile: Candidate profile (for the sign-off and contact details)
            resume_version: Tailored resume to attach
            cover_letter_version: Tailored cover letter (body + attachment)

        Returns:
            ComposedEmail ready for the user to review
        """
        composed = ComposedEmail(
            to_email=to_email,
            subject=self._subject(job, profile),
            body="",
        )

        if cover_letter_version and cover_letter_version.content_text.strip():
            composed.body = self._body_from_cover_letter(cover_letter_version, profile)
        else:
            composed.body = self._minimal_body(job, profile)
            composed.warnings.append(
                "No tailored cover letter was available — the body is a short "
                "factual note. Edit it before sending."
            )

        for version, label in (
            (resume_version, "resume"),
            (cover_letter_version, "cover letter"),
        ):
            if not version:
                if label == "resume":
                    composed.warnings.append("No resume is attached")
                continue

            if version.fabrication_flags:
                composed.unverified_documents.append(version.id)
                composed.warnings.append(
                    f"The {label} contains {len(version.fabrication_flags)} claim(s) "
                    f"not supported by your master document"
                )

            if version.pdf_path and Path(version.pdf_path).exists():
                composed.attachments.append(version.pdf_path)
            elif version.pdf_path:
                composed.warnings.append(
                    f"The {label} PDF is missing from disk: {version.pdf_path}"
                )

        logger.info(
            f"Composed an application email to {to_email} for '{job.title}' "
            f"with {len(composed.attachments)} attachment(s)"
        )

        return composed

    # ------------------------------------------------------------------
    # Parts
    # ------------------------------------------------------------------

    @staticmethod
    def _subject(job: Job, profile: Optional[CandidateProfile]) -> str:
        """
        Build the subject line.

        Recruiters filter on these, so the role comes first and the reference
        is included when the posting has one.
        """
        parts = [f"Application: {job.title}"]

        external_id = (job.external_id or "").strip()
        # Only include a reference that looks like one, not a URL slug
        if external_id and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-_]{1,19}", external_id):
            parts.append(f"({external_id})")

        if profile and profile.full_name:
            parts.append(f"— {profile.full_name}")

        return " ".join(parts)

    def _body_from_cover_letter(
        self, version: DocumentVersion, profile: Optional[CandidateProfile]
    ) -> str:
        """
        Use the verified cover letter as the email body.

        Args:
            version: The tailored cover letter
            profile: Candidate profile, for appending contact details

        Returns:
            Email body text
        """
        body = version.content_text.strip()
        contact = self._contact_block(profile)

        # Avoid repeating details the letter already signs off with
        if contact and not self._contains_contact(body, profile):
            body = f"{body}\n\n{contact}"

        return body

    @staticmethod
    def _minimal_body(job: Job, profile: Optional[CandidateProfile]) -> str:
        """
        A short factual note used when no cover letter exists.

        Deliberately makes no claim about the candidate's experience — the
        attached resume speaks for that. Anything else would be prose the
        fabrication check never saw.
        """
        name = profile.full_name if profile and profile.full_name else "the applicant"

        lines = [
            "Dear Hiring Team,",
            "",
            f"I would like to apply for the {job.title} position"
            + (f" at {job.company}" if job.company else "")
            + ". My resume is attached.",
            "",
            "Thank you for your consideration.",
            "",
            "Best regards,",
            name,
        ]

        contact = EmailComposer._contact_block(profile)
        if contact:
            lines.extend(["", contact])

        return "\n".join(lines)

    @staticmethod
    def _contact_block(profile: Optional[CandidateProfile]) -> str:
        """Build a contact footer from the profile."""
        if not profile:
            return ""

        parts = [
            value for value in (
                profile.email,
                profile.phone,
                profile.linkedin_url,
            ) if value
        ]

        return " | ".join(parts)

    @staticmethod
    def _contains_contact(body: str, profile: Optional[CandidateProfile]) -> bool:
        """True if the body already shows the candidate's email."""
        if not profile or not profile.email:
            return False

        return profile.email.lower() in body.lower()

    # ------------------------------------------------------------------
    # Lookup helpers
    # ------------------------------------------------------------------

    def latest_documents(self, job: Job) -> tuple:
        """
        Find the most recent tailored documents for a job.

        Args:
            job: The job

        Returns:
            (resume_version, cover_letter_version), either may be None
        """
        def latest(doc_type: DocumentType) -> Optional[DocumentVersion]:
            return (
                self.db_session.query(DocumentVersion)
                .filter(
                    DocumentVersion.job_id == job.id,
                    DocumentVersion.doc_type == doc_type,
                )
                .order_by(DocumentVersion.created_at.desc())
                .first()
            )

        return latest(DocumentType.RESUME), latest(DocumentType.COVER_LETTER)
