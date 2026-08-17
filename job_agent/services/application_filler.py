"""
Application Filler (Phase 5).

Fills what it confidently knows, defers everything else, screenshots the result,
and queues the application for the user to review.

    read form → classify each field → fill KNOWN/REMEMBERED
                                    → defer SENSITIVE/UNKNOWN
                                    → screenshot → queue for review

**Nothing is submitted in this phase.** Every application ends in
QUEUED_FOR_REVIEW regardless of automation mode; submission arrives in Phase 6
behind the clean-submissions gate. The user's own hands are the only thing that
sends an application to an employer right now.

A deferred field is left untouched on the page. Filling a placeholder — even an
obviously fake one — risks it being submitted verbatim if anything downstream
goes wrong, so blank is the safer failure.
"""

import logging
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from job_agent.config import settings
from job_agent.models.database import (
    Application,
    ApplicationStatus,
    AuditAction,
    AuditLog,
    CandidateProfile,
    DocumentVersion,
    FieldCategory,
    Job,
    PlatformAccount,
)
from job_agent.services.field_classifier import FieldClassifier, FormField
from job_agent.services.form_reader import FormReader
from job_agent.utils.dates import utcnow

logger = logging.getLogger(__name__)


@dataclass
class FillOutcome:
    """Result of filling one application form."""

    filled_fields: Dict[str, Any] = dataclass_field(default_factory=dict)
    deferred_fields: Dict[str, Any] = dataclass_field(default_factory=dict)
    screenshot_path: Optional[str] = None
    errors: List[str] = dataclass_field(default_factory=list)

    @property
    def needs_user_input(self) -> bool:
        """True when something must be answered before this can be submitted."""
        return bool(self.deferred_fields)

    @property
    def required_deferred(self) -> List[str]:
        """Deferred fields the form marks as required."""
        return [
            name for name, detail in self.deferred_fields.items()
            if detail.get("required")
        ]


class ApplicationFiller:
    """Fills application forms and queues them for review."""

    def __init__(self, db_session: Session, screenshots_dir: Optional[Path] = None):
        """
        Initialize the filler.

        Args:
            db_session: Database session
            screenshots_dir: Where filled-form screenshots are written
                (defaults to app support; tests point this at a temp dir)
        """
        self.db_session = db_session
        self.screenshots_dir = screenshots_dir or (settings.documents_dir / "screenshots")

    # ------------------------------------------------------------------
    # Filling
    # ------------------------------------------------------------------

    async def fill_form(
        self,
        page: Any,
        profile: CandidateProfile,
        documents: Optional[Dict[str, str]] = None,
        job: Any = None,
        master_text: str = "",
    ) -> FillOutcome:
        """
        Fill the application form currently open in the browser.

        Args:
            page: Playwright page on the application form
            profile: The user's candidate profile
            documents: {"resume": path, "cover_letter": path} for file uploads
            job: The job being applied to, used to draft answers to open
                questions. Omitted, the questions are simply left blank.
            master_text: The user's master resume — the only source of facts
                a drafted answer may draw on

        Returns:
            FillOutcome describing what was filled and what was deferred
        """
        documents = documents or {}
        outcome = FillOutcome()

        fields = await FormReader.read_fields(page)
        classifier = FieldClassifier(profile, dict(profile.remembered_answers or {}))

        for form_field in fields:
            classification = classifier.classify(form_field)

            # Document uploads resolve against the generated package
            if classification.profile_key in ("resume", "cover_letter"):
                document_path = documents.get(classification.profile_key)

                if not document_path:
                    self._defer(
                        outcome, form_field, FieldCategory.UNKNOWN,
                        f"No tailored {classification.profile_key.replace('_', ' ')} "
                        f"was generated for this job",
                    )
                    continue

                if await self._attach_file(page, form_field, document_path, outcome):
                    outcome.filled_fields[form_field.question] = {
                        "value": document_path,
                        "selector": form_field.selector,
                        "category": FieldCategory.KNOWN.value,
                        "reason": classification.reason,
                    }
                continue

            if not classification.is_fillable:
                self._defer(
                    outcome, form_field, classification.category, classification.reason
                )
                continue

            if await self._fill_field(page, form_field, classification.value, outcome):
                outcome.filled_fields[form_field.question] = {
                    "value": classification.value,
                    "selector": form_field.selector,
                    "category": classification.category.value,
                    "reason": classification.reason,
                }

        if job is not None and master_text:
            await self._draft_open_answers(outcome, job, master_text)

        outcome.screenshot_path = await self._capture_screenshot(page, outcome)

        logger.info(
            f"Filled {len(outcome.filled_fields)} field(s), "
            f"deferred {len(outcome.deferred_fields)} on {page.url}"
        )

        return outcome

    async def _draft_open_answers(
        self,
        outcome: FillOutcome,
        job: Any,
        master_text: str,
    ) -> None:
        """
        Draft answers to the open written questions, for the user to review.

        A form asks "Which countries did you manage Benefits in?" and the
        answer is sitting in the resume the agent has already read. Leaving an
        empty box makes the user write from scratch what the agent could have
        put in front of them.

        Drafts are suggestions, not answers: they are recorded separately from
        `value_entered_by_user`, so nothing counts as answered until the user
        has seen it. Demographic and compensation questions are never drafted
        — those are the user's own and stay blank whatever the resume says.

        Args:
            outcome: The fill outcome, enriched in place
            job: The job being applied to
            master_text: The user's master resume
        """
        from job_agent.services.tailoring import get_tailoring_service

        service = get_tailoring_service()

        for question, detail in outcome.deferred_fields.items():
            if detail.get("category") == FieldCategory.SENSITIVE.value:
                continue

            # A drafted sentence cannot answer a fixed list or a consent tick.
            if detail.get("options") or detail.get("field_type") not in ("text", "textarea"):
                continue

            try:
                draft = await service.draft_answer(question, master_text, job)
            except Exception as e:
                logger.info(f"Drafting failed for {question[:40]!r}: {e}")
                continue

            if draft:
                detail["suggested_answer"] = draft
                detail["reason"] = (
                    f"{detail.get('reason', '')} A draft is filled in below, "
                    f"written only from your resume — read it and change it."
                ).strip()

    @staticmethod
    def _defer(
        outcome: FillOutcome,
        form_field: FormField,
        category: FieldCategory,
        reason: str,
    ) -> None:
        """
        Record a field for the user to answer, leaving the page untouched.

        Args:
            outcome: Outcome being built
            form_field: The field
            category: Why it wasn't filled
            reason: Human-readable explanation for the review queue
        """
        outcome.deferred_fields[form_field.question] = {
            "selector": form_field.selector,
            "category": category.value,
            "reason": reason,
            "question": form_field.question,
            "field_type": form_field.field_type,
            "options": form_field.options,
            "required": form_field.required,
            "value_entered_by_user": None,
            "suggested_answer": None,
        }

    async def _fill_field(
        self,
        page: Any,
        form_field: FormField,
        value: str,
        outcome: FillOutcome,
    ) -> bool:
        """
        Type or select a value into one field.

        Args:
            page: Playwright page
            form_field: The field to fill
            value: Value to enter
            outcome: Outcome collecting errors

        Returns:
            True if the field was filled
        """
        try:
            locator = page.locator(form_field.selector).first

            # A field with a fixed list of answers only accepts one of them,
            # whatever tag it wears. Custom dropdowns render as text inputs, so
            # a value that merely looked right for the question — a country
            # matched onto "…require sponsorship … in your current location?" —
            # would be typed in and rejected at submit, or worse, accepted as
            # an answer the user never gave.
            if form_field.options:
                matched = self._closest_option(value, form_field.options)

                if not matched:
                    self._defer(
                        outcome, form_field, FieldCategory.UNKNOWN,
                        f"'{value}' does not match any option on this dropdown",
                    )
                    return False

                if form_field.tag == "select":
                    await locator.select_option(label=matched)
                else:
                    await locator.fill(matched)

            elif form_field.field_type in ("checkbox", "radio"):
                # A yes/no answer to a checkbox is still the user's call
                self._defer(
                    outcome, form_field, FieldCategory.UNKNOWN,
                    "Multiple-choice answers are left for you to select",
                )
                return False

            else:
                await locator.fill(value)

            return True

        except Exception as e:
            message = f"Could not fill '{form_field.question}': {e}"
            logger.warning(message)
            outcome.errors.append(message)
            self._defer(
                outcome, form_field, FieldCategory.UNKNOWN,
                "The agent could not fill this field automatically",
            )
            return False

    async def _attach_file(
        self,
        page: Any,
        form_field: FormField,
        file_path: str,
        outcome: FillOutcome,
    ) -> bool:
        """
        Attach a document to a file input.

        Args:
            page: Playwright page
            form_field: The file field
            file_path: Path to the document
            outcome: Outcome collecting errors

        Returns:
            True if the file was attached
        """
        if not Path(file_path).exists():
            message = f"Document missing from disk: {file_path}"
            logger.error(message)
            outcome.errors.append(message)
            self._defer(outcome, form_field, FieldCategory.UNKNOWN, message)
            return False

        try:
            await page.locator(form_field.selector).first.set_input_files(file_path)
            return True
        except Exception as e:
            message = f"Could not attach {Path(file_path).name}: {e}"
            logger.warning(message)
            outcome.errors.append(message)
            self._defer(
                outcome, form_field, FieldCategory.UNKNOWN,
                "The agent could not attach this file automatically",
            )
            return False

    @staticmethod
    def _closest_option(value: str, options: List[str]) -> Optional[str]:
        """
        Find the dropdown option matching a value.

        Args:
            value: Desired value
            options: Available option labels

        Returns:
            The matching option, or None
        """
        lowered = value.strip().lower()

        for option in options:
            if option.strip().lower() == lowered:
                return option

        for option in options:
            if lowered in option.strip().lower():
                return option

        return None

    async def _capture_screenshot(self, page: Any, outcome: FillOutcome) -> Optional[str]:
        """
        Screenshot the filled form for the review queue.

        Args:
            page: Playwright page
            outcome: Outcome collecting errors

        Returns:
            Path to the screenshot, or None if capture failed
        """
        timestamp = utcnow().strftime("%Y%m%d_%H%M%S")
        path = self.screenshots_dir / f"form_{timestamp}.png"
        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            await page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception as e:
            message = f"Could not capture the filled form: {e}"
            logger.warning(message)
            outcome.errors.append(message)
            return None

    # ------------------------------------------------------------------
    # Review queue
    # ------------------------------------------------------------------

    def queue_for_review(
        self,
        job: Job,
        platform_account: PlatformAccount,
        profile: CandidateProfile,
        outcome: FillOutcome,
        form_url: str,
        resume_version: Optional[DocumentVersion] = None,
        cover_letter_version: Optional[DocumentVersion] = None,
    ) -> Application:
        """
        Store a filled form as an application awaiting user review.

        Phase 5 never submits: the status is QUEUED_FOR_REVIEW whatever the
        platform's automation mode says.

        Args:
            job: The job applied for
            platform_account: Platform the form belongs to
            profile: Candidate profile used to fill it
            outcome: What was filled and deferred
            form_url: URL of the application form
            resume_version: Tailored resume attached, if any
            cover_letter_version: Tailored cover letter attached, if any

        Returns:
            The stored Application
        """
        application = Application(
            job_id=job.id,
            platform_account_id=platform_account.id,
            candidate_profile_id=profile.id,
            resume_version_id=resume_version.id if resume_version else None,
            cover_letter_version_id=cover_letter_version.id if cover_letter_version else None,
            resume_version=resume_version.pdf_path if resume_version else None,
            cover_letter_version=cover_letter_version.pdf_path if cover_letter_version else None,
            filled_fields=outcome.filled_fields,
            deferred_fields=outcome.deferred_fields,
            screenshot_path=outcome.screenshot_path,
            form_url=form_url,
            submission_status=ApplicationStatus.QUEUED_FOR_REVIEW,
            filled_at=utcnow(),
        )

        self.db_session.add(application)
        self.db_session.commit()
        self.db_session.refresh(application)

        self._log(
            AuditAction.APPLICATION_FILLED,
            f"Filled {len(outcome.filled_fields)} field(s) for '{job.title}' "
            f"at {job.company}",
            {
                "application_id": application.id,
                "job_id": job.id,
                "filled": list(outcome.filled_fields),
                "deferred": list(outcome.deferred_fields),
                "errors": outcome.errors,
            },
            platform=platform_account.platform,
        )

        for question, detail in outcome.deferred_fields.items():
            self._log(
                AuditAction.FIELD_DEFERRED,
                f"Deferred '{question}' ({detail['category']}): {detail['reason']}",
                {"application_id": application.id, **detail},
                platform=platform_account.platform,
                result="paused",
            )

        self._log(
            AuditAction.APPLICATION_QUEUED,
            f"Application for '{job.title}' at {job.company} is awaiting your review"
            + (f" — {len(outcome.required_deferred)} required question(s) unanswered"
               if outcome.required_deferred else ""),
            {
                "application_id": application.id,
                "required_unanswered": outcome.required_deferred,
            },
            platform=platform_account.platform,
            result="paused",
        )

        logger.info(
            f"Queued application #{application.id} for review "
            f"({len(outcome.deferred_fields)} field(s) need you)"
        )

        return application

    def _log(
        self,
        action: AuditAction,
        detail: str,
        detail_json: dict,
        platform: Optional[str] = None,
        result: str = "success",
    ) -> None:
        """Write an audit entry."""
        self.db_session.add(
            AuditLog(
                timestamp=utcnow(),
                action=action,
                platform=platform,
                actor="agent",
                detail=detail,
                detail_json=detail_json,
                result=result,
            )
        )
        self.db_session.commit()
