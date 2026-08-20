"""
Application Analyst (Phase 11).

The last reader before an application reaches an employer.

Every step upstream verifies its own work: tailoring checks that it invented
nothing, the filler checks that a control accepted what it typed, the gate
checks that a human approved. None of them reads the *finished application* the
way a recruiter will — resume, letter, answers and attachments together, against
the posting they are answering. Each piece can pass its own check while the
application as a whole is one nobody would send.

That gap is what this module closes. It is the only place allowed to refuse an
application for being weak rather than broken, and it runs on every submission,
attended or not.

Two severities, and the difference matters:

- **blocker** — the application is wrong, not merely improvable. A resume with
  no email on it, a letter addressed to a different company, an answer that is
  not one of the choices the form offers. These stop the submission.
- **warning** — the application is sound but would land better with work. These
  are shown and do not stop anything; the user decides.

The bar for a blocker is deliberately narrow: something a recruiter would count
against the candidate, that the user would not have chosen had they noticed it.
Taste is not a blocker. A gate that refuses too often gets routed around, and
then it protects nothing.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from sqlalchemy.orm import Session

from job_agent.config import settings
from job_agent.models.database import (
    Application,
    CandidateProfile,
    DocumentType,
    DocumentVersion,
    Job,
    MasterDocument,
)
from job_agent.services.document_parser import (
    EMAIL_PATTERN,
    has_email_address,
    has_phone_number,
)

logger = logging.getLogger(__name__)

# Text that means a template was never filled in. Any of these reaching an
# employer reads as carelessness whatever else the document says.
#
# Bracketed text only counts when it reads like a template token. Plain
# brackets are ordinary punctuation — "(BSCS) [Honours]" is a real line on a
# real resume, and blocking it would teach the user to ignore this check.
PLACEHOLDER_PATTERNS = [
    re.compile(
        r"\[[^\]]*\b(your|name|company|employer|date|title|position|role"
        r"|address|insert|here)\b[^\]]*\]",
        re.I,
    ),
    re.compile(r"\{\{[^}]{2,40}\}\}"),        # {{ company }}
    re.compile(r"\blorem ipsum\b", re.I),
    re.compile(r"\bTODO\b"),
    re.compile(r"\bXXXX+\b", re.I),
    re.compile(r"\binsert (your|the) \w+", re.I),
    re.compile(r"\byour (company|name) here\b", re.I),
]

# An employment line: the date range an experience entry hangs off. The month
# names on either side are optional and are not captured — "Aug 2023 – May 2026"
# and "2023 – 2026" describe the same span, and a duplicate entry is often
# written both ways.
DATE_RANGE_PATTERN = re.compile(
    r"((?:19|20)\d{2})\s*[–—-]\s*(?:[A-Za-z]{3,9}\.?\s+)?"
    r"((?:19|20)\d{2}|present|current|now)",
    re.IGNORECASE,
)

# Qualifications tailoring may not quietly drop. Mirrors the same list the
# tailoring service rejects output on — repeated here because a document can
# also lose one between generation and submission, by being regenerated,
# replaced by hand, or attached from another job.
CREDENTIAL_TERMS = (
    "bachelor", "master's", "masters", "mba", "phd", "doctorate",
    "diploma", "certified", "certification",
)


@dataclass
class Finding:
    """One thing the analyst noticed, and what to do about it."""

    check: str  # Short slug, stable enough to test against
    severity: str  # "blocker" | "warning"
    detail: str  # What is wrong, in the user's terms
    fix: str = ""  # The next action that resolves it
    # Whether the user may send anyway. False for defects — a resume with no
    # email on it is not a matter of opinion. True for the one judgement the
    # analyst makes on the user's behalf: whether this posting is worth an
    # application. That call is theirs, and an override is recorded.
    overridable: bool = False

    def to_dict(self) -> dict:
        """Serialize for the dashboard."""
        return {
            "check": self.check,
            "severity": self.severity,
            "detail": self.detail,
            "fix": self.fix,
            "overridable": self.overridable,
        }


@dataclass
class ReadinessReport:
    """Whether an application is fit to send, and what stands in the way."""

    application_id: Optional[int] = None
    findings: List[Finding] = field(default_factory=list)
    checks_passed: List[str] = field(default_factory=list)
    fit_score: Optional[int] = None
    ats_score: Optional[int] = None

    @property
    def blockers(self) -> List[Finding]:
        """Findings that stop the submission."""
        return [f for f in self.findings if f.severity == "blocker"]

    @property
    def warnings(self) -> List[Finding]:
        """Findings worth seeing that do not stop anything."""
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def ready(self) -> bool:
        """True when nothing blocks the submission."""
        return not self.blockers

    def to_dict(self) -> dict:
        """Serialize for the dashboard and the audit log."""
        return {
            "application_id": self.application_id,
            "ready": self.ready,
            "fit_score": self.fit_score,
            "ats_score": self.ats_score,
            "blockers": [f.to_dict() for f in self.blockers],
            "warnings": [f.to_dict() for f in self.warnings],
            "checks_passed": self.checks_passed,
        }


@dataclass
class _Subject:
    """Everything the analyst reads, gathered once."""

    application: Application
    job: Optional[Job] = None
    master_resume: Optional[MasterDocument] = None
    resume: Optional[DocumentVersion] = None
    cover_letter: Optional[DocumentVersion] = None
    profile: Optional[CandidateProfile] = None
    # A fit report computed by the caller. The semantic matcher needs to be
    # awaited and the checks are synchronous, so `analyse_async` works it out
    # first and hands it in. None means "work it out the lexical way".
    fit: Optional[object] = None

    @property
    def resume_text(self) -> str:
        """The tailored resume's text, or empty."""
        return (self.resume.content_text if self.resume else "") or ""

    @property
    def master_text(self) -> str:
        """The master resume's text, or empty."""
        return (self.master_resume.content_text if self.master_resume else "") or ""

    @property
    def posting_text(self) -> str:
        """The posting's description and requirements as one block."""
        if not self.job:
            return ""

        return "\n".join(filter(None, [self.job.description, self.job.requirements]))

    @property
    def answers(self) -> dict:
        """Every question on the form with the value that will be submitted."""
        merged = {}

        for question, detail in (self.application.filled_fields or {}).items():
            merged[question] = {**detail, "answer": detail.get("value")}

        for question, detail in (self.application.deferred_fields or {}).items():
            merged[question] = {
                **detail,
                "answer": detail.get("value_entered_by_user"),
            }

        return merged


class ApplicationAnalyst:
    """Reads a finished application the way its recipient will."""

    def __init__(self, db_session: Session):
        """
        Initialize the analyst.

        Args:
            db_session: Database session
        """
        self.db_session = db_session

    def analyse(self, application: Application, fit: Optional[object] = None) -> ReadinessReport:
        """
        Judge whether an application is fit to send.

        Args:
            application: The application about to be submitted
            fit: A precomputed fit report. Supplied by `analyse_async`, which
                can await the semantic matcher; without it the fit check falls
                back to comparing shared words, which understates a resume
                that answers a posting in different language.

        Returns:
            A ReadinessReport. Never raises: a check that cannot run is
            reported as a warning, because an analyst that crashes on an odd
            document would otherwise block every submission behind it.
        """
        report = ReadinessReport(application_id=application.id)
        subject = self._gather(application)
        subject.fit = fit

        checks = (
            self._check_profile_ready,
            self._check_resume_attached,
            self._check_resume_is_for_this_job,
            self._check_resume_reaches_you,
            self._check_resume_keeps_credentials,
            self._check_no_placeholders,
            self._check_no_repeated_history,
            self._check_pdf_matches_what_was_reviewed,
            self._check_machine_readable,
            self._check_answers_the_posting,
            self._check_the_posting_is_a_real_vacancy,
            self._check_cover_letter_present,
            self._check_cover_letter_addresses_this_job,
            self._check_required_questions_answered,
            self._check_answers_are_on_the_menu,
            self._check_answers_look_written,
        )

        for check in checks:
            try:
                check(subject, report)
            except Exception as e:
                logger.warning(f"{check.__name__} could not run: {type(e).__name__}: {e}")
                report.findings.append(
                    Finding(
                        check=check.__name__.removeprefix("_check_"),
                        severity="warning",
                        detail=f"This check could not run ({type(e).__name__}).",
                        fix="Read the document yourself before releasing.",
                    )
                )

        logger.info(
            f"Analyst on application #{application.id}: "
            f"{'ready' if report.ready else 'not ready'} — "
            f"{len(report.blockers)} blocker(s), {len(report.warnings)} warning(s)"
        )

        return report

    async def analyse_async(self, application: Application) -> ReadinessReport:
        """
        Judge an application, matching the posting by meaning rather than
        by shared words.

        The checks themselves are synchronous and stay that way — only the fit
        report needs a model. It is computed here and handed to `analyse`, so
        there is one set of checks rather than two that can drift apart.

        Args:
            application: The application about to be submitted

        Returns:
            A ReadinessReport
        """
        from job_agent.services.fit_report import analyse_fit_async

        subject = self._gather(application)
        fit = None

        if subject.resume and subject.posting_text:
            try:
                fit = await analyse_fit_async(subject.posting_text, subject.resume_text)
            except Exception as e:
                logger.info(
                    f"Semantic fit unavailable ({type(e).__name__}: {e}) — "
                    f"falling back to matching on shared words"
                )

        return self.analyse(application, fit=fit)

    # ------------------------------------------------------------------
    # Gathering
    # ------------------------------------------------------------------

    def _gather(self, application: Application) -> _Subject:
        """
        Load the documents and posting this application is made of.

        Args:
            application: The application

        Returns:
            A _Subject with whatever exists; missing pieces stay None and are
            reported by the checks that need them.
        """
        subject = _Subject(application=application)

        subject.job = (
            self.db_session.query(Job).filter(Job.id == application.job_id).first()
        )

        subject.master_resume = (
            self.db_session.query(MasterDocument)
            .filter(
                MasterDocument.doc_type == DocumentType.RESUME,
                MasterDocument.is_active == True,  # noqa: E712
            )
            .first()
        )

        if application.resume_version_id:
            subject.resume = (
                self.db_session.query(DocumentVersion)
                .filter(DocumentVersion.id == application.resume_version_id)
                .first()
            )

        if application.cover_letter_version_id:
            subject.cover_letter = (
                self.db_session.query(DocumentVersion)
                .filter(DocumentVersion.id == application.cover_letter_version_id)
                .first()
            )

        if application.candidate_profile_id:
            subject.profile = (
                self.db_session.query(CandidateProfile)
                .filter(CandidateProfile.id == application.candidate_profile_id)
                .first()
            )
        if not subject.profile:
            subject.profile = (
                self.db_session.query(CandidateProfile)
                .filter(CandidateProfile.is_active == True)
                .first()
            )

        return subject

    # ------------------------------------------------------------------
    # The resume
    # ------------------------------------------------------------------

    @staticmethod
    def _check_resume_attached(subject: _Subject, report: ReadinessReport) -> None:
        """
        A resume the form will actually receive must exist on disk.

        Carrying no resume at all is a warning, not a blocker: an application
        sent by email, or a form that never asked for one, is legitimately
        document-free. A resume that is *recorded* and missing from disk is a
        different thing — the upload will fail or attach nothing, and the user
        believes a document was sent.
        """
        if not subject.resume:
            report.findings.append(
                Finding(
                    check="resume_attached",
                    severity="warning",
                    detail="No tailored resume is attached to this application.",
                    fix="Generate documents for this job if the form takes one.",
                )
            )
            return

        path = Path(subject.resume.pdf_path) if subject.resume.pdf_path else None

        if not path or not path.exists():
            report.findings.append(
                Finding(
                    check="resume_attached",
                    severity="blocker",
                    detail=(
                        f"Resume version #{subject.resume.id} has no PDF on disk"
                        f"{f' ({path})' if path else ''} — the form has nothing to upload."
                    ),
                    fix="Regenerate the resume for this job.",
                )
            )
            return

        report.checks_passed.append("a rendered resume is attached")

    @staticmethod
    def _check_resume_is_for_this_job(subject: _Subject, report: ReadinessReport) -> None:
        """
        The attached resume must be the one tailored for this posting.

        Versions are looked up by job elsewhere in the pipeline, and a
        mismatch means the wrong company's resume is about to be uploaded —
        invisible in a finished-looking PDF, and fatal when a recruiter reads
        another employer's name in it.
        """
        if not subject.resume or subject.resume.job_id is None:
            return

        if subject.resume.job_id != subject.application.job_id:
            report.findings.append(
                Finding(
                    check="resume_is_for_this_job",
                    severity="blocker",
                    detail=(
                        f"The attached resume was tailored for job "
                        f"#{subject.resume.job_id}, not job "
                        f"#{subject.application.job_id}."
                    ),
                    fix="Regenerate the documents for this job before submitting.",
                )
            )
            return

        report.checks_passed.append("the resume was tailored for this posting")

    @staticmethod
    def _check_profile_ready(subject: _Subject, report: ReadinessReport) -> None:
        """The candidate profile must be complete."""
        profile = subject.profile
        if not profile:
            return

        if not profile.full_name or not profile.full_name.strip():
            report.findings.append(
                Finding(
                    check="profile_ready",
                    severity="blocker",
                    detail="Your candidate profile is missing your full name.",
                    fix="Fill in your profile details on the Desk.",
                )
            )

        if not profile.email or not profile.email.strip():
            report.findings.append(
                Finding(
                    check="profile_ready",
                    severity="blocker",
                    detail="Your candidate profile is missing your email address.",
                    fix="Fill in your profile details on the Desk.",
                )
            )

        if profile.full_name and subject.resume:
            name_words = [w.lower() for w in profile.full_name.split() if len(w) > 1]
            if name_words and not any(w in subject.resume_text.lower() for w in name_words):
                report.findings.append(
                    Finding(
                        check="profile_name_on_resume",
                        severity="blocker",
                        detail=f"The name on your profile ('{profile.full_name}') does not match the resume.",
                        fix="Update your profile name or your master resume name to match.",
                    )
                )

    @staticmethod
    def _check_resume_reaches_you(subject: _Subject, report: ReadinessReport) -> None:
        """
        Whatever else tailoring changed, the employer must be able to reply.

        This is the failure that prompted the check: a rewrite kept the
        candidate's name and dropped the header line carrying their email,
        phone and location. The document still looked like a resume, and an
        interested recruiter had no way to contact them.

        Note: both the email check and phone check always run; a resume that
        dropped its header typically loses both, and suppressing the phone
        warning when the email blocker already fired hides a second problem
        the user needs to act on.
        """
        if not subject.resume:
            return

        text = subject.resume_text
        master = subject.master_text
        profile = subject.profile

        profile_email = profile.email if profile else None
        email_ok = True  # optimistic; set False if a blocker is raised

        if profile_email:
            if profile_email.lower() not in text.lower():
                report.findings.append(
                    Finding(
                        check="resume_reaches_you",
                        severity="blocker",
                        detail=(
                            f"The tailored resume does not contain your profile email "
                            f"'{profile_email}'."
                        ),
                        fix="Make sure the email is in your master resume and regenerate.",
                    )
                )
                email_ok = False
        else:
            master_has_email = has_email_address(master)
            tailored_has_email = has_email_address(text)

            if master_has_email and not tailored_has_email:
                report.findings.append(
                    Finding(
                        check="resume_reaches_you",
                        severity="blocker",
                        detail=(
                            "The tailored resume has no email address on it, though "
                            "your master resume does. An employer reading this PDF "
                            "cannot reply to you."
                        ),
                        fix="Regenerate the resume, or edit the master so the header survives tailoring.",
                    )
                )
                email_ok = False
            elif not tailored_has_email:
                report.findings.append(
                    Finding(
                        check="resume_reaches_you",
                        severity="blocker",
                        detail="No email address appears anywhere on the resume being sent.",
                        fix="Add an email address to your master resume and regenerate.",
                    )
                )
                email_ok = False

        if email_ok:
            report.checks_passed.append("the resume carries an email address")

        # Phone check runs regardless of email result: a headerless resume
        # typically loses both and both problems should be visible at once.
        profile_phone = profile.phone if profile else None
        if profile_phone:
            profile_digits = re.sub(r"\D", "", profile_phone)
            text_digits = re.sub(r"\D", "", text)
            if profile_digits and profile_digits not in text_digits:
                report.findings.append(
                    Finding(
                        check="resume_keeps_phone",
                        severity="warning",
                        detail=f"Your profile phone number '{profile_phone}' is not on this resume.",
                        fix="Regenerate the resume if you want recruiters to be able to call.",
                    )
                )
        elif has_phone_number(master) and not has_phone_number(text):
            report.findings.append(
                Finding(
                    check="resume_keeps_phone",
                    severity="warning",
                    detail="Your phone number is on the master resume but not on this one.",
                    fix="Regenerate the resume if you want recruiters to be able to call.",
                )
            )

    @staticmethod
    def _check_resume_keeps_credentials(
        subject: _Subject, report: ReadinessReport
    ) -> None:
        """A rewrite may reorder and reword; it may not cost a qualification."""
        if not subject.resume or not subject.master_text:
            return

        tailored = subject.resume_text.lower()
        master = subject.master_text.lower()

        dropped = [
            term for term in CREDENTIAL_TERMS
            if term in master and term not in tailored
        ]

        if dropped:
            report.findings.append(
                Finding(
                    check="resume_keeps_credentials",
                    severity="blocker",
                    detail=(
                        f"The tailored resume dropped a qualification the master "
                        f"lists: {', '.join(dropped)}."
                    ),
                    fix="Regenerate the resume — screening filters on these.",
                )
            )
            return

        report.checks_passed.append("qualifications survived tailoring")

    @staticmethod
    def _check_no_placeholders(subject: _Subject, report: ReadinessReport) -> None:
        """Template scaffolding must never reach an employer."""
        for label, version in (
            ("resume", subject.resume),
            ("cover letter", subject.cover_letter),
        ):
            if not version:
                continue

            for pattern in PLACEHOLDER_PATTERNS:
                found = pattern.search(version.content_text or "")

                if found:
                    report.findings.append(
                        Finding(
                            check="no_placeholders",
                            severity="blocker",
                            detail=(
                                f"The {label} still contains unfilled template "
                                f"text: {found.group(0)[:60]!r}."
                            ),
                            fix=f"Regenerate or edit the {label} before sending.",
                        )
                    )
                    break

        if not any(f.check == "no_placeholders" for f in report.findings):
            report.checks_passed.append("no template placeholders left in the documents")

    @staticmethod
    def _check_no_repeated_history(subject: _Subject, report: ReadinessReport) -> None:
        """
        The same job must not appear twice under two headings.

        A model asked to reorganise a resume sometimes keeps the section it
        built and the section it copied from, so one employer is listed twice
        with the same dates. It reads as padding, and it is the kind of thing
        the writer never sees because they know what the document was meant
        to say.
        """
        if not subject.resume:
            return

        seen: dict = {}
        duplicates: List[str] = []

        for line in subject.resume_text.splitlines():
            stripped = line.strip()
            match = DATE_RANGE_PATTERN.search(stripped)

            if not match or stripped[:1] in ("-", "•", "*", "·"):
                continue

            # An employment line is identified by its employer and dates, not
            # by its wording: "Dedicated CSR, Mindbridge | Aug 2023 – May 2026"
            # and "CSR / Trainer | Mindbridge | Aug 2023 – May 2026" are the
            # same job written twice.
            employer = ApplicationAnalyst._employer_key(stripped)
            key = (employer, match.group(1), match.group(2).lower())

            if not employer:
                continue

            if key in seen:
                duplicates.append(stripped[:70])
            else:
                seen[key] = stripped

        if duplicates:
            report.findings.append(
                Finding(
                    check="no_repeated_history",
                    severity="blocker",
                    detail=(
                        f"The same role is listed twice on the resume: "
                        f"{duplicates[0]!r}."
                    ),
                    fix="Regenerate the resume — a duplicated entry reads as padding.",
                )
            )
            return

        report.checks_passed.append("no role is listed twice")

    @staticmethod
    def _employer_key(line: str) -> str:
        """
        The employer named in an experience line, normalised for comparison.

        Args:
            line: A line from the resume

        Returns:
            A lowercase employer fragment, or "" if the line names none
        """
        # Company names carry a legal suffix far more often than job titles do,
        # which is what makes them findable without a company list.
        match = re.search(
            r"([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*)*\s+"
            r"(?:Pvt\.?|Ltd\.?|Inc\.?|LLC|GmbH|Limited|Corporation|Corp\.?|Co\.?))",
            line,
        )

        if match:
            return re.sub(r"[^a-z]", "", match.group(1).lower())

        return ""

    @staticmethod
    def _check_pdf_matches_what_was_reviewed(
        subject: _Subject, report: ReadinessReport
    ) -> None:
        """
        The PDF being uploaded must be the document that was reviewed.

        Everything else in this module reads `content_text` from the database.
        The employer receives the rendered file. They diverge when rendering
        half-fails, when a version's PDF is regenerated from different text, or
        when the file on disk belongs to another version — and the divergence
        is invisible unless someone opens the actual file.
        """
        if not subject.resume or not subject.resume.pdf_path:
            return

        path = Path(subject.resume.pdf_path)

        if not path.exists():
            return  # Already blocked by _check_resume_attached

        from job_agent.services.document_parser import parse_document

        rendered_text, _, _ = parse_document(path)
        rendered = ApplicationAnalyst._letters(rendered_text)

        if not rendered:
            report.findings.append(
                Finding(
                    check="pdf_matches_review",
                    severity="blocker",
                    detail=(
                        "No text could be read out of the resume PDF. An "
                        "applicant tracking system will read it the same way."
                    ),
                    fix="Regenerate the resume and open the PDF yourself.",
                )
            )
            return

        # Compared on the first line — the candidate's name — plus the email,
        # rather than whole-document equality: the renderer legitimately
        # reflows text, and demanding a character match would fail on every
        # document that wrapped a line.
        name = next(
            (line.strip() for line in subject.resume_text.splitlines() if line.strip()),
            "",
        )
        missing = []

        if name and ApplicationAnalyst._letters(name) not in rendered:
            missing.append(f"your name ({name[:40]})")

        email = EMAIL_PATTERN.search(subject.resume_text)

        if email and ApplicationAnalyst._letters(email.group(0)) not in rendered:
            missing.append("your email address")

        if missing:
            report.findings.append(
                Finding(
                    check="pdf_matches_review",
                    severity="blocker",
                    detail=(
                        f"The PDF that would be uploaded is missing "
                        f"{' and '.join(missing)}, though the reviewed text has it."
                    ),
                    fix="Regenerate the resume, then open the PDF before releasing.",
                )
            )
            return

        report.checks_passed.append("the PDF matches the text that was reviewed")

    @staticmethod
    def _letters(text: str) -> str:
        """Lowercase letters and digits only, for comparisons across layout."""
        return re.sub(r"[^a-z0-9]", "", (text or "").lower())

    @staticmethod
    def _check_machine_readable(subject: _Subject, report: ReadinessReport) -> None:
        """
        Score the resume the way the employer's first reader — software — will.

        The floor is low on purpose. This blocks documents no ATS can parse,
        not documents that could be better written; the score itself is shown
        either way so the user can decide what to improve.
        """
        if not subject.resume:
            return

        from job_agent.services.ats_check import check_ats

        ats = check_ats(subject.resume_text)
        report.ats_score = ats.score

        floor = settings.analyst_min_ats_score

        if ats.score < floor:
            worst = ats.failures[0].detail if ats.failures else ""
            report.findings.append(
                Finding(
                    check="machine_readable",
                    severity="blocker",
                    detail=(
                        f"The resume scores {ats.score}/100 on machine "
                        f"readability, below the {floor} floor. {worst}"
                    ),
                    fix="Run the ATS improver on your master resume, then regenerate.",
                )
            )
            return

        if not ats.passed:
            report.findings.append(
                Finding(
                    check="machine_readable",
                    severity="warning",
                    detail=(
                        f"The resume scores {ats.score}/100 on machine "
                        f"readability. "
                        + (ats.failures[0].detail if ats.failures else "")
                    ),
                    fix=ats.failures[0].fix if ats.failures else "",
                )
            )
            return

        report.checks_passed.append(f"machine readability {ats.score}/100")

    @staticmethod
    def _check_answers_the_posting(subject: _Subject, report: ReadinessReport) -> None:
        """
        Whether the resume being sent speaks to what this posting asks for.

        This is the "is it a strong application" question, and it is a warning
        at the top of the range and a blocker only at the bottom. A resume that
        evidences none of a posting's requirements is not a long shot; it is a
        different career, and sending it spends the user's name for nothing.
        """
        if not subject.resume or not subject.posting_text:
            return

        from job_agent.services.fit_report import analyse_fit

        fit = subject.fit or analyse_fit(subject.posting_text, subject.resume_text)

        if not fit.matches:
            return  # The posting listed no requirements to check against

        report.fit_score = fit.score
        floor = settings.analyst_min_fit_score

        if fit.score < floor:
            missing = "; ".join(m.requirement[:60] for m in fit.missing[:2])
            report.findings.append(
                Finding(
                    check="answers_the_posting",
                    severity="blocker",
                    overridable=True,
                    detail=(
                        f"The resume evidences {fit.score}% of what this posting "
                        f"asks for, below the {floor}% floor. Unevidenced: {missing}."
                    ),
                    fix=(
                        "Regenerate the resume so what you have done for this "
                        "kind of work leads it, or spend the application on a "
                        "closer posting. Send it anyway if you disagree — this "
                        "is a judgement about odds, not a defect."
                    ),
                )
            )
            return

        if not fit.worth_applying:
            report.findings.append(
                Finding(
                    check="answers_the_posting",
                    severity="warning",
                    detail=(
                        f"The resume evidences {fit.score}% of this posting's "
                        f"requirements — a long shot."
                    ),
                    fix="Worth sending, but expect a low response rate.",
                )
            )
            return

        report.checks_passed.append(
            f"evidences {fit.score}% of the posting's requirements"
        )

    @staticmethod
    def _check_the_posting_is_a_real_vacancy(
        subject: _Subject, report: ReadinessReport
    ) -> None:
        """
        Say so when the posting admits it is not a job.

        Boards carry "talent pipeline" and "future opening" listings that read
        exactly like vacancies and hire nobody. The pipeline already knows the
        phrases — it strips them out before scoring fit — and then says nothing,
        so the user answers twenty questions and waits on a reply that was never
        coming. Not a reason to block: joining a talent pool is a legitimate
        thing to do on purpose.
        """
        if not subject.posting_text:
            return

        found = re.search(
            r"(this is not an active job[^.]*|future opening pipeline[^.]*"
            r"|talent (pool|pipeline)[^.]*|not currently hiring[^.]*)",
            subject.posting_text,
            re.IGNORECASE,
        )

        if not found:
            report.checks_passed.append("the posting is an open vacancy")
            return

        report.findings.append(
            Finding(
                check="posting_is_a_real_vacancy",
                severity="warning",
                detail=(
                    f"This posting says it is not an open role: "
                    f"“{found.group(0).strip()[:120]}”."
                ),
                fix=(
                    "Worth sending only as a talent-pool entry — expect no "
                    "reply on a timeline."
                ),
            )
        )

    # ------------------------------------------------------------------
    # The cover letter
    # ------------------------------------------------------------------

    @staticmethod
    def _check_cover_letter_present(subject: _Subject, report: ReadinessReport) -> None:
        """
        A form that asks for a cover letter should not be sent one blank.

        Optional is still a signal: on a posting where every other applicant
        attaches one, the empty field is the difference. Required and empty is
        a rejection.
        """
        field_detail = None

        for question, detail in subject.answers.items():
            if re.search(r"cover|motivation", question, re.I):
                field_detail = detail
                break

        if field_detail is None:
            return  # This form does not ask for one

        if subject.cover_letter:
            report.checks_passed.append("a cover letter is attached")
            return

        if field_detail.get("required"):
            report.findings.append(
                Finding(
                    check="cover_letter_present",
                    severity="blocker",
                    detail="This form requires a cover letter and none is attached.",
                    fix="Generate a cover letter for this job, then refill the form.",
                )
            )
            return

        report.findings.append(
            Finding(
                check="cover_letter_present",
                severity="warning",
                detail="This form offers a cover letter field and none is attached.",
                fix="Generate one for this job — it is the cheapest edge available.",
            )
        )

    @staticmethod
    def _check_cover_letter_addresses_this_job(
        subject: _Subject, report: ReadinessReport
    ) -> None:
        """
        A letter must name the company and role it is applying to.

        A generic letter is a wasted attachment; a letter naming the *previous*
        company is worse than none, and both come from the same place — reusing
        a draft written for another posting.
        """
        if not subject.cover_letter or not subject.job:
            return

        text = (subject.cover_letter.content_text or "").lower()
        company = (subject.job.company or "").strip()
        title = (subject.job.title or "").strip()

        if company and company.lower() not in text:
            report.findings.append(
                Finding(
                    check="cover_letter_addresses_this_job",
                    severity="blocker",
                    detail=f"The cover letter never mentions {company}.",
                    fix="Regenerate the cover letter for this job.",
                )
            )
            return

        # The full title rarely survives rewording; its distinctive words do.
        distinctive = [
            word for word in re.findall(r"[a-z]{4,}", title.lower())
            if word not in ("senior", "junior", "staff", "lead", "remote")
        ]

        if distinctive and not any(word in text for word in distinctive):
            report.findings.append(
                Finding(
                    check="cover_letter_addresses_this_job",
                    severity="warning",
                    detail=f"The cover letter never refers to the role ({title}).",
                    fix="Regenerate it, or name the role in the opening line.",
                )
            )
            return

        report.checks_passed.append("the cover letter names this company and role")

    # ------------------------------------------------------------------
    # The answers
    # ------------------------------------------------------------------

    @staticmethod
    def _check_required_questions_answered(
        subject: _Subject, report: ReadinessReport
    ) -> None:
        """Every question the form marks required must carry an answer."""
        unanswered = [
            question
            for question, detail in (subject.application.deferred_fields or {}).items()
            if detail.get("required") and detail.get("value_entered_by_user") in (None, "")
        ]

        if unanswered:
            report.findings.append(
                Finding(
                    check="required_questions_answered",
                    severity="blocker",
                    detail=(
                        f"{len(unanswered)} required question(s) have no answer: "
                        f"{', '.join(q[:50] for q in unanswered[:3])}."
                    ),
                    fix="Answer them in the tray before releasing.",
                )
            )
            return

        report.checks_passed.append("every required question is answered")

    @staticmethod
    def _check_answers_are_on_the_menu(
        subject: _Subject, report: ReadinessReport
    ) -> None:
        """
        An answer to a fixed-list question must be one of the choices.

        A dropdown accepts nothing else. An answer that is not on the list is
        either silently discarded — submitting the form with that question
        blank — or it stops the submission at the last moment, after the user
        believed the application was sent. Both are worth catching here, where
        the fix is one edit rather than a re-fill.
        """
        offenders = []

        for question, detail in subject.answers.items():
            options = detail.get("options") or []
            answer = detail.get("answer")

            if not options or answer in (None, ""):
                continue

            wanted = ApplicationAnalyst._fold(str(answer))

            if not any(ApplicationAnalyst._fold(option) == wanted for option in options):
                offenders.append((question, str(answer), options))

        for question, answer, options in offenders:
            report.findings.append(
                Finding(
                    check="answers_are_on_the_menu",
                    severity="blocker",
                    detail=(
                        f"{question[:60]!r} is answered {answer[:40]!r}, which is "
                        f"not one of the {len(options)} choices the form offers."
                    ),
                    fix=f"Pick one of: {', '.join(o[:40] for o in options[:4])}…",
                )
            )

        if not offenders:
            report.checks_passed.append("every choice answer is on the form's list")

    @staticmethod
    def _fold(text: str) -> str:
        """Collapse the whitespace variants a form and a stored answer differ on."""
        return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip().lower()

    @staticmethod
    def _check_answers_look_written(subject: _Subject, report: ReadinessReport) -> None:
        """
        Open questions deserve a real answer, not a word.

        "Why do you want to work here?" answered with "Yes" is worse than
        leaving it blank, and it happens when a drafted answer is accepted
        without being read.
        """
        thin = []

        for question, detail in subject.answers.items():
            if detail.get("options") or detail.get("field_type") not in ("text", "textarea"):
                continue

            answer = (detail.get("answer") or "").strip()

            # Only the questions that actually want prose. A short box asking
            # for a city or a LinkedIn URL is answered correctly in two words.
            if not answer or len(question) < 25 or "?" not in question:
                continue

            if len(answer.split()) < 4 or ApplicationAnalyst._fold(answer) == ApplicationAnalyst._fold(question):
                thin.append((question, answer))

        for question, answer in thin:
            report.findings.append(
                Finding(
                    check="answers_look_written",
                    severity="warning",
                    detail=f"{question[:60]!r} is answered with just {answer[:40]!r}.",
                    fix="Write two or three sentences — this is read by a person.",
                )
            )

        if not thin:
            report.checks_passed.append("open questions have written answers")


def analyse_application(db_session: Session, application: Application) -> ReadinessReport:
    """
    Judge whether an application is fit to send.

    Args:
        db_session: Database session
        application: The application about to be submitted

    Returns:
        A ReadinessReport
    """
    return ApplicationAnalyst(db_session).analyse(application)
