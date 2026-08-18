"""
Document Builder (Phase 4).

Orchestrates the document workflow:

    upload  → parse → store MasterDocument
    tailor  → generate variant → verify → render PDF → store DocumentVersion

Storage layout under ~/Library/Application Support/job-agent/documents/:

    masters/<doc_type>/<id>_<name>.<ext>      the user's original upload, kept verbatim
    versions/<job_id>/<doc_type>_v<id>.pdf    generated variants

The master upload is never modified. Variants are always new files, so the
user can compare any submitted document against the original.
"""

import logging
import re
import shutil
from pathlib import Path
from typing import List, Optional

from sqlalchemy.orm import Session

from job_agent.config import settings
from job_agent.models.database import (
    AuditAction,
    AuditLog,
    DocumentType,
    DocumentVersion,
    Job,
    MasterDocument,
)
from job_agent.services.document_parser import parse_document
from job_agent.services.pdf_renderer import PdfRenderer
from job_agent.services.tailoring import get_tailoring_service
from job_agent.utils.dates import utcnow

logger = logging.getLogger(__name__)


class DocumentBuilder:
    """Creates master documents and tailored variants."""

    def __init__(self, db_session: Session, documents_dir: Optional[Path] = None):
        """
        Initialize the builder.

        Args:
            db_session: Database session
            documents_dir: Root storage directory (defaults to app support;
                tests point this at a temporary directory)
        """
        self.db_session = db_session
        self.documents_dir = documents_dir or settings.documents_dir
        self.renderer = PdfRenderer()
        self.tailoring = get_tailoring_service()

    # ------------------------------------------------------------------
    # Masters
    # ------------------------------------------------------------------

    def upload_master(
        self,
        source_path: Path,
        doc_type: DocumentType,
        name: Optional[str] = None,
        make_active: bool = True,
    ) -> MasterDocument:
        """
        Parse and store a master document.

        The original file is copied into managed storage so the record stays
        valid even if the user moves or deletes the file they uploaded.

        Args:
            source_path: File the user selected (.docx, .pdf, .txt, .md)
            doc_type: Resume or cover letter
            name: Display name (defaults to the file stem)
            make_active: Deactivate other masters of this type

        Returns:
            The stored MasterDocument

        Raises:
            FileNotFoundError: If the file doesn't exist
            ValueError: If the format is unsupported, the file is too large,
                or no text could be extracted
        """
        source_path = Path(source_path)

        if not source_path.exists():
            raise FileNotFoundError(f"Document not found: {source_path}")

        size_mb = source_path.stat().st_size / (1024 * 1024)
        if size_mb > settings.document_max_upload_mb:
            raise ValueError(
                f"{source_path.name} is {size_mb:.1f} MB, over the "
                f"{settings.document_max_upload_mb} MB limit"
            )

        content_text, sections, doc_format = parse_document(source_path)

        document = MasterDocument(
            doc_type=doc_type,
            name=name or source_path.stem,
            source_path="",  # Set below, once we have an id for the filename
            source_format=doc_format,
            content_text=content_text,
            sections=sections,
            is_active=make_active,
        )

        self.db_session.add(document)
        self.db_session.commit()
        self.db_session.refresh(document)

        # Copy the original into managed storage
        stored_path = (
            self.documents_dir / "masters" / doc_type.value
            / f"{document.id}_{self._slug(document.name)}{source_path.suffix.lower()}"
        )
        stored_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, stored_path)

        document.source_path = str(stored_path)
        document.updated_at = utcnow()

        if make_active:
            self._deactivate_other_masters(doc_type, keep_id=document.id)

        self.db_session.commit()
        self.db_session.refresh(document)

        self._log(
            AuditAction.DOCUMENT_UPLOADED,
            f"Stored master {doc_type.value} '{document.name}' "
            f"({doc_format.value}, {len(content_text)} chars)",
            {
                "master_document_id": document.id,
                "doc_type": doc_type.value,
                "format": doc_format.value,
                "sections": list(sections),
            },
        )

        logger.info(f"Stored master {doc_type.value} #{document.id}: {document.name}")

        return document

    def get_active_master(self, doc_type: DocumentType) -> Optional[MasterDocument]:
        """
        Get the active master document of a type.

        Args:
            doc_type: Resume or cover letter

        Returns:
            The active MasterDocument, or None
        """
        return (
            self.db_session.query(MasterDocument)
            .filter(
                MasterDocument.doc_type == doc_type,
                MasterDocument.is_active == True,  # noqa: E712 — SQL comparison
            )
            .order_by(MasterDocument.created_at.desc())
            .first()
        )

    def _deactivate_other_masters(self, doc_type: DocumentType, keep_id: int) -> None:
        """Mark every other master of this type inactive."""
        others = (
            self.db_session.query(MasterDocument)
            .filter(MasterDocument.doc_type == doc_type, MasterDocument.id != keep_id)
            .all()
        )

        for other in others:
            other.is_active = False

    # ------------------------------------------------------------------
    # Variants
    # ------------------------------------------------------------------

    async def build_variant(
        self,
        job: Job,
        doc_type: DocumentType,
        master: Optional[MasterDocument] = None,
        fit_to_posting: bool = False,
    ) -> DocumentVersion:
        """
        Generate, verify, and render a tailored variant for a job.

        Args:
            job: The job to tailor for
            doc_type: Resume or cover letter
            master: Master to tailor (defaults to the active one)
            fit_to_posting: Aim the resume at this posting as hard as honesty
                allows — reframe the summary, reword every passage into the
                employer's language, and keep each change only where it does
                not cost fit (see `fit_rewrite`). Ordinary tailoring rewords;
                this one is measured against the posting and reports what it
                was worth.

        Returns:
            The stored DocumentVersion, with pdf_path set

        Raises:
            ValueError: If no master of this type has been uploaded
        """
        master = master or self.get_active_master(doc_type)
        extra_notes: List[str] = []

        # A cover letter states facts about the candidate, and the resume is
        # already the authority on those. Requiring a master *letter* before
        # one can be written meant every form offering a cover-letter field got
        # it blank — the agent had the material and no permission to use it.
        # Writing from the resume changes nothing about the rule that matters:
        # the letter may still say only what the resume says.
        if not master and doc_type == DocumentType.COVER_LETTER:
            master = self.get_active_master(DocumentType.RESUME)

            if master:
                extra_notes.append(
                    "Written from your master resume — you have not uploaded a "
                    "master cover letter, so every claim in it comes from the "
                    "resume. Read it before releasing."
                )

        if not master:
            raise ValueError(
                f"No active master {doc_type.value} — upload one before "
                f"generating tailored documents"
            )

        from job_agent.models.database import CandidateProfile
        profile = (
            self.db_session.query(CandidateProfile)
            .filter(CandidateProfile.is_active == True)
            .first()
        )

        logger.info(
            f"Tailoring {doc_type.value} for '{job.title}' at '{job.company}' "
            f"from master #{master.id}"
        )

        if fit_to_posting and doc_type == DocumentType.RESUME:
            from job_agent.services.fit_rewrite import FitRewriter
            from job_agent.services.tailoring import TailoringResult

            fitted = await FitRewriter(self.tailoring).rewrite(
                master.content_text, job
            )

            result = TailoringResult(
                content_text=fitted.content_text,
                generator="fit-rewrite",
                notes=[fitted.describe(), *fitted.notes],
            )

            from job_agent.services.fabrication_check import verify_no_fabrication

            result.fabrication_flags = verify_no_fabrication(
                master.content_text,
                result.content_text,
                allowed_terms=self.tailoring._allowed_terms(job)
                + [
                    str(getattr(profile, attribute, "") or "")
                    for attribute in (
                        "full_name", "email", "phone", "location",
                        "linkedin_url", "github_url", "portfolio_url",
                        "website_url",
                    )
                ],
            )
        else:
            result = await self.tailoring.tailor(
                master.content_text, job, doc_type, profile=profile
            )
        
        if doc_type == DocumentType.RESUME and profile and profile.email:
            if profile.email.lower() not in result.content_text.lower():
                header_parts = [profile.full_name]
                sub_parts = []
                if profile.location:
                    sub_parts.append(profile.location)
                sub_parts.append(profile.email)
                if profile.phone:
                    sub_parts.append(profile.phone)
                header_parts.append(" | ".join(sub_parts))
                if profile.linkedin_url:
                    header_parts.append(profile.linkedin_url)
                
                header_text = "\n".join(header_parts) + "\n\n"
                result.content_text = header_text + result.content_text

        result.notes = list(result.notes) + extra_notes

        version = DocumentVersion(
            master_document_id=master.id,
            job_id=job.id,
            doc_type=doc_type,
            content_text=result.content_text,
            generator=result.generator,
            tailoring_notes=result.notes,
            fabrication_flags=result.fabrication_flags,
        )

        self.db_session.add(version)
        self.db_session.commit()
        self.db_session.refresh(version)

        # Render the PDF the user will review and submit
        pdf_path = (
            self.documents_dir / "versions" / str(job.id or "unassigned")
            / f"{doc_type.value}_v{version.id}.pdf"
        )

        try:
            self.renderer.render(
                result.content_text,
                pdf_path,
                title=f"{master.name} — {job.company} {job.title}",
            )
            version.pdf_path = str(pdf_path)
        except Exception as e:
            # The text is still useful even if rendering fails; don't lose it
            logger.error(f"PDF rendering failed for version #{version.id}: {e}")
            version.tailoring_notes = list(version.tailoring_notes) + [
                f"PDF rendering failed: {e}"
            ]

        self.db_session.commit()
        self.db_session.refresh(version)

        self._log(
            AuditAction.DOCUMENT_TAILORED,
            f"Generated {doc_type.value} for '{job.title}' at '{job.company}' "
            f"via {result.generator}",
            {
                "document_version_id": version.id,
                "master_document_id": master.id,
                "job_id": job.id,
                "generator": result.generator,
                "verified": version.is_verified,
            },
        )

        if version.fabrication_flags:
            self._log(
                AuditAction.DOCUMENT_FLAGGED,
                f"{doc_type.value} version #{version.id} contains "
                f"{len(version.fabrication_flags)} claim(s) not supported by the master",
                {
                    "document_version_id": version.id,
                    "flags": version.fabrication_flags,
                },
                result="paused",
            )

        return version

    async def build_fitted_package(
        self, job: Job
    ) -> List[DocumentVersion]:
        """
        Build a resume aimed squarely at one posting, plus a cover letter.

        Args:
            job: The posting

        Returns:
            The stored versions
        """
        versions = [
            await self.build_variant(job, DocumentType.RESUME, fit_to_posting=True)
        ]

        try:
            versions.append(await self.build_variant(job, DocumentType.COVER_LETTER))
        except ValueError as e:
            logger.info(f"No cover letter for '{job.title}': {e}")

        return versions

    async def build_application_package(
        self,
        job: Job,
        include_cover_letter: bool = True,
    ) -> List[DocumentVersion]:
        """
        Build every document needed to apply for a job.

        Args:
            job: The job to apply for
            include_cover_letter: Also generate a cover letter. One is written
                from the master resume when no master letter exists, so a form
                that asks for a letter is never sent an empty field.

        Returns:
            The generated versions (resume first)

        Raises:
            ValueError: If no master resume has been uploaded
        """
        versions = [await self.build_variant(job, DocumentType.RESUME)]

        if include_cover_letter:
            try:
                versions.append(await self.build_variant(job, DocumentType.COVER_LETTER))
            except Exception as e:
                # A missing letter costs the application an attachment. Losing
                # the tailored resume with it would cost the application.
                logger.warning(
                    f"Could not generate a cover letter for '{job.title}': {e}"
                )

        return versions

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _slug(text: str) -> str:
        """Make a filename-safe slug."""
        slug = re.sub(r"[^\w\s-]", "", text).strip().lower()
        slug = re.sub(r"[\s_-]+", "-", slug)
        return slug[:60] or "document"

    def _log(
        self,
        action: AuditAction,
        detail: str,
        detail_json: dict,
        result: str = "success",
    ) -> None:
        """Write an audit entry."""
        self.db_session.add(
            AuditLog(
                timestamp=utcnow(),
                action=action,
                actor="agent",
                detail=detail,
                detail_json=detail_json,
                result=result,
            )
        )
        self.db_session.commit()
