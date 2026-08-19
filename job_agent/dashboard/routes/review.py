"""
Review Queue API Routes (Phase 5).

Where the user sees what the agent filled, answers what it wouldn't, and
decides what happens next.

- GET  /api/v1/review — Applications awaiting review
- GET  /api/v1/review/{id} — Side-by-side: filled form + source job posting
- GET  /api/v1/review/{id}/screenshot — The filled form as the agent saw it
- GET  /api/v1/review/{id}/analysis — The pre-submission read of the whole
  application: documents, answers and attachments against the posting
- POST /api/v1/review/{id}/regenerate-documents — Rebuild the tailored
  documents and attach the new ones (clears the approval)
- POST /api/v1/review/{id}/answers — Answer deferred questions
- POST /api/v1/review/{id}/approve — Mark reviewed and ready to send
- POST /api/v1/review/{id}/discard — Throw the application away

Nothing here submits an application to an employer. "Approve" records the
user's decision and hands the form back to them; automatic submission arrives
in Phase 6.

Candidate profile endpoints live here too, since the profile exists to fill
these forms:
- GET/POST /api/v1/review/profile
"""

import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from job_agent.connectors import create_connector_for_account
from job_agent.core.session_manager import get_session_manager
from job_agent.dashboard.deps import get_session
from job_agent.models.database import (
    Application,
    ApplicationStatus,
    AuditAction,
    AuditLog,
    CandidateProfile,
    DocumentVersion,
    FieldCategory,
    Job,
    MasterDocument,
    PlatformAccount,
)
from job_agent.services.answer_carry import CarryResult, carry_answers_forward
from job_agent.services.application_analyst import ApplicationAnalyst
from job_agent.services.field_classifier import FieldClassifier
from job_agent.services.submission_gate import SubmissionGate
from job_agent.services.submission_recorder import SubmissionRecorder
from job_agent.services.submitter import SubmissionOutcome
from job_agent.utils.dates import utcnow

logger = logging.getLogger(__name__)

# Writing a reviewed value back should fail fast. Playwright's 30s default
# turned one unreachable field into a half-minute stall per field.
RESTORE_TIMEOUT_MS = 6000

router = APIRouter(prefix="/api/v1/review", tags=["review"])
SessionDep = get_session


def _summary(application: Application, job: Optional[Job]) -> dict:
    """Serialize an application for the queue listing."""
    deferred = application.deferred_fields or {}
    unanswered = [
        question for question, detail in deferred.items()
        if detail.get("value_entered_by_user") in (None, "")
    ]
    required_unanswered = [
        question for question in unanswered
        if deferred[question].get("required")
    ]

    return {
        "id": application.id,
        "job_id": application.job_id,
        "job_title": job.title if job else None,
        "company": job.company if job else None,
        "status": application.submission_status.value,
        "form_url": application.form_url,
        "filled_count": len(application.filled_fields or {}),
        "form_steps": (application.form_walk or {}).get("step_count", 1),
        "deferred_count": len(deferred),
        "unanswered_count": len(unanswered),
        "required_unanswered": required_unanswered,
        "ready_to_submit": not required_unanswered,
        "has_screenshot": bool(application.screenshot_path),
        "reviewed_by_user": application.reviewed_by_user,
        "filled_at": application.filled_at.isoformat() if application.filled_at else None,
        "reviewed_at": application.reviewed_at.isoformat() if application.reviewed_at else None,
    }


def _get_application(session: Session, application_id: int) -> Application:
    """Fetch an application or raise 404."""
    application = session.query(Application).filter(
        Application.id == application_id
    ).first()

    if not application:
        raise HTTPException(status_code=404, detail=f"Application {application_id} not found")

    return application


def _audit(
    session: Session,
    action: AuditAction,
    detail: str,
    detail_json: dict,
    result: str = "success",
) -> None:
    """Write an audit entry for a user action."""
    session.add(
        AuditLog(
            timestamp=utcnow(),
            action=action,
            actor="user",  # These endpoints are only reached by a human
            detail=detail,
            detail_json=detail_json,
            result=result,
        )
    )
    session.commit()


# ============================================================================
# Candidate profile
# ============================================================================

@router.get("/profile")
async def get_profile(session: Session = Depends(SessionDep)) -> dict:
    """
    Get the active candidate profile.

    Returns:
        The profile, or a not-configured marker
    """
    profile = session.query(CandidateProfile).filter(
        CandidateProfile.is_active == True  # noqa: E712 — SQL comparison
    ).first()

    if not profile:
        return {
            "configured": False,
            "message": "No candidate profile yet — POST one before filling forms",
        }

    return {
        "configured": True,
        "id": profile.id,
        "full_name": profile.full_name,
        "email": profile.email,
        "phone": profile.phone,
        "location": profile.location,
        "linkedin_url": profile.linkedin_url,
        "github_url": profile.github_url,
        "portfolio_url": profile.portfolio_url,
        "website_url": profile.website_url,
        "work_authorization": profile.work_authorization,
        "requires_sponsorship": profile.requires_sponsorship,
        "willing_to_relocate": profile.willing_to_relocate,
        "years_experience": profile.years_experience,
        "notice_period": profile.notice_period,
        "remembered_answers": profile.remembered_answers,
    }


@router.post("/profile")
async def save_profile(
    profile_data: dict,
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Create or update the candidate profile.

    Args:
        profile_data: Profile fields (full_name and email are required)

    Returns:
        The saved profile id and name
    """
    if not profile_data.get("full_name") or not profile_data.get("email"):
        raise HTTPException(
            status_code=400, detail="full_name and email are required"
        )

    profile = session.query(CandidateProfile).filter(
        CandidateProfile.is_active == True  # noqa: E712
    ).first()

    editable = (
        "full_name", "email", "phone", "location", "linkedin_url", "github_url",
        "portfolio_url", "website_url", "work_authorization",
        "requires_sponsorship", "willing_to_relocate", "years_experience",
        "notice_period",
    )

    if profile:
        for key in editable:
            if key in profile_data:
                setattr(profile, key, profile_data[key])
        profile.updated_at = utcnow()
    else:
        profile = CandidateProfile(
            **{k: profile_data.get(k) for k in editable if k in profile_data}
        )
        session.add(profile)

    session.commit()
    session.refresh(profile)

    return {"status": "saved", "id": profile.id, "full_name": profile.full_name}


# ============================================================================
# Review queue
# ============================================================================

@router.get("")
async def list_queue(
    status: Optional[str] = Query(
        "queued_for_review", description="Filter by status; pass 'all' for everything"
    ),
    for_current_resume: bool = Query(
        True, description="Only applications built from the resume in use"
    ),
    session: Session = Depends(SessionDep),
) -> dict:
    """
    List applications awaiting review.

    Args:
        status: Application status to filter by, or "all"
        for_current_resume: Hide applications whose documents were built from
            a resume no longer in use. On by default: after switching resume
            the tray otherwise still offers applications carrying the old
            one's tailored PDF, and releasing one sends a document written
            from a resume the user has moved away from.

    Returns:
        Queue entries and counts
    """
    from job_agent.services.resume_sync import (
        active_master,
        application_uses_current_resume,
    )

    query = session.query(Application)

    if status and status != "all":
        query = query.filter(Application.submission_status == status)

    applications = query.order_by(Application.created_at.desc()).all()

    master = active_master(session) if for_current_resume else None
    hidden = 0

    if master:
        current, stale = [], 0

        for application in applications:
            if application_uses_current_resume(session, application, master.id):
                current.append(application)
            else:
                stale += 1

        applications, hidden = current, stale

    jobs = {
        job.id: job
        for job in session.query(Job).filter(
            Job.id.in_([a.job_id for a in applications] or [0])
        ).all()
    }

    entries = [_summary(a, jobs.get(a.job_id)) for a in applications]

    return {
        "total": len(entries),
        "needs_answers": sum(1 for e in entries if e["required_unanswered"]),
        "ready_to_submit": sum(1 for e in entries if e["ready_to_submit"]),
        # So the tray can say "3 built from an earlier resume are not shown"
        # rather than appearing to have lost them.
        "hidden_from_earlier_resumes": hidden,
        "resume_in_use": master.name if master else None,
        "applications": entries,
    }


@router.get("/{application_id}")
async def review_detail(
    application_id: int,
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Everything needed to review one application side by side.

    Returns the filled fields, the questions the agent declined to answer (with
    reasons), the attached documents, and the source job posting.

    Args:
        application_id: Application ID

    Returns:
        Full review payload
    """
    application = _get_application(session, application_id)
    job = session.query(Job).filter(Job.id == application.job_id).first()

    deferred = application.deferred_fields or {}

    # Sensitive questions are grouped separately: they are not gaps in the
    # agent's knowledge, they are questions only the user may answer.
    sensitive = {
        q: d for q, d in deferred.items()
        if d.get("category") == FieldCategory.SENSITIVE.value
    }
    other = {
        q: d for q, d in deferred.items()
        if d.get("category") != FieldCategory.SENSITIVE.value
    }

    documents = []
    for version_id, kind in (
        (application.resume_version_id, "resume"),
        (application.cover_letter_version_id, "cover_letter"),
    ):
        if not version_id:
            continue
        version = session.query(DocumentVersion).filter(
            DocumentVersion.id == version_id
        ).first()
        if version:
            # The text travels with the link. A document the user can only
            # reach by opening a PDF in another tab is one they approve without
            # reading — which is how a rewritten resume reached an employer
            # with its contact details missing.
            master = session.query(MasterDocument).filter(
                MasterDocument.id == version.master_document_id
            ).first()

            documents.append({
                "kind": kind,
                "version_id": version.id,
                "verified": version.is_verified,
                "fabrication_flags": version.fabrication_flags,
                "pdf_url": f"/api/v1/documents/versions/{version.id}/pdf",
                "generator": version.generator,
                "tailoring_notes": version.tailoring_notes or [],
                "content_text": version.content_text,
                "written_from": master.name if master else None,
                "created_at": version.created_at.isoformat(),
            })

    analysis = (await ApplicationAnalyst(session).analyse_async(application)).to_dict()

    return {
        **_summary(application, job),
        "filled_fields": application.filled_fields or {},
        # How many steps of this form were read, and where the agent stopped.
        "walk": application.form_walk,
        "sensitive_questions": sensitive,
        "other_questions": other,
        "documents": documents,
        "unverified_documents": [d for d in documents if not d["verified"]],
        "analysis": analysis,
        "screenshot_url": (
            f"/api/v1/review/{application_id}/screenshot"
            if application.screenshot_path else None
        ),
        "user_notes": application.user_notes,
        "job": {
            "id": job.id,
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "salary": job.salary,
            "description": job.description,
            "requirements": job.requirements,
            "apply_method": job.apply_method,
            "fit_score": job.fit_score,
        } if job else None,
    }


@router.get("/{application_id}/screenshot")
async def get_screenshot(
    application_id: int,
    session: Session = Depends(SessionDep),
) -> FileResponse:
    """
    Download the screenshot of the filled form.

    Args:
        application_id: Application ID

    Returns:
        The PNG image
    """
    application = _get_application(session, application_id)

    if not application.screenshot_path:
        raise HTTPException(status_code=404, detail="No screenshot was captured")

    path = Path(application.screenshot_path)

    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Screenshot missing from disk: {path}")

    return FileResponse(path=path, media_type="image/png", filename=path.name)


@router.post("/{application_id}/answers")
async def submit_answers(
    application_id: int,
    payload: dict,
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Answer deferred questions.

    Answers are recorded against the application. Non-sensitive answers may
    also be remembered on the profile for future forms; sensitive ones never
    are — the user re-answers those each time, deliberately.

    Args:
        application_id: Application ID
        payload: {"answers": {question: answer}, "remember": bool}

    Returns:
        Updated counts
    """
    application = _get_application(session, application_id)

    answers = payload.get("answers") or {}
    if not isinstance(answers, dict) or not answers:
        raise HTTPException(status_code=400, detail="Provide answers as {question: answer}")

    deferred = dict(application.deferred_fields or {})
    unknown_questions = [q for q in answers if q not in deferred]

    if unknown_questions:
        raise HTTPException(
            status_code=400,
            detail=f"Not deferred on this application: {', '.join(unknown_questions)}",
        )

    remember = bool(payload.get("remember", True))

    # Demographic answers are never kept by default — the agent must not
    # accumulate them off the back of a routine "save". They are still the
    # user's own words about themselves, though, and retyping gender and
    # ethnicity on every application is friction that protects nobody, so
    # keeping them is offered as a separate, explicit choice.
    remember_sensitive = bool(payload.get("remember_sensitive", False))
    profile = (
        session.query(CandidateProfile)
        .filter(CandidateProfile.id == application.candidate_profile_id)
        .first()
        if application.candidate_profile_id else None
    )
    remembered = dict(profile.remembered_answers or {}) if profile else {}
    remembered_count = 0

    for question, answer in answers.items():
        deferred[question] = {**deferred[question], "value_entered_by_user": answer}

        is_sensitive = deferred[question].get("category") == FieldCategory.SENSITIVE.value
        may_remember = remember_sensitive if is_sensitive else remember

        if may_remember and profile:
            remembered[FieldClassifier.remember_key(question)] = answer
            remembered_count += 1

    application.deferred_fields = deferred
    application.updated_at = utcnow()

    if profile and remembered_count:
        profile.remembered_answers = remembered
        profile.updated_at = utcnow()

    # The other applications in the tray ask the same questions. Saving the
    # answer for *next time* was never the whole job — the forms that need it
    # are the ones already waiting, and they were filled before this answer
    # existed.
    carried = carry_answers_forward(
        session, profile, exclude_application_id=application_id
    )

    session.commit()
    session.refresh(application)

    outstanding = [
        q for q, d in deferred.items()
        if d.get("required") and d.get("value_entered_by_user") in (None, "")
    ]

    if carried.changed_anything:
        _audit(
            session,
            AuditAction.APPLICATION_REVIEWED,
            f"Carried {carried.answers_filled} answer(s) onto "
            f"{carried.applications_updated} other application(s) in the tray",
            {"source_application_id": application_id, **carried.to_dict()},
        )

    return {
        "status": "recorded",
        "answered": list(answers),
        "remembered_for_future_forms": remembered_count,
        "required_unanswered": outstanding,
        "ready_to_submit": not outstanding,
        "carried_to_other_applications": carried.to_dict(),
    }


@router.post("/apply-saved-answers")
async def apply_saved_answers(session: Session = Depends(SessionDep)) -> dict:
    """
    Put every answer already saved onto the applications still waiting.

    Answers are carried forward automatically from now on, when one is saved
    and again when an application is submitted. That does nothing for the
    answers saved *before* — and there are usually a great many of them,
    sitting in the profile while the tray asks the same questions again. This
    is the one-off catch-up for that backlog.

    Returns:
        What it filled, and on which applications
    """
    profile = session.query(CandidateProfile).filter(
        CandidateProfile.is_active == True  # noqa: E712 — SQL comparison
    ).first()

    if not profile:
        raise HTTPException(
            status_code=404,
            detail="No candidate profile yet — there are no saved answers to apply",
        )

    saved = len(profile.remembered_answers or {})
    carried = carry_answers_forward(session, profile)

    session.commit()

    if carried.changed_anything:
        _audit(
            session,
            AuditAction.APPLICATION_REVIEWED,
            f"Applied {carried.answers_filled} saved answer(s) to "
            f"{carried.applications_updated} waiting application(s)",
            carried.to_dict(),
        )

    return {
        "status": "applied",
        "saved_answers": saved,
        **carried.to_dict(),
        "message": carried.describe() or (
            f"Nothing to fill — the {saved} answer(s) you have saved do not "
            f"match any unanswered question in the tray."
            if saved else
            "You have not saved any answers yet. Answer a form's questions and "
            "tick 'save these answers' and they will carry to the rest."
        ),
    }


@router.post("/{application_id}/regenerate-documents")
async def regenerate_documents(
    application_id: int,
    fit_to_posting: bool = Query(
        False,
        description=(
            "Aim the resume at this posting as hard as honesty allows, and "
            "report what it was worth"
        ),
    ),
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Rebuild this application's documents and attach the new ones.

    Generating documents for a job and re-pointing an application at them are
    two different things, and only the first had an endpoint. So an
    application whose resume the analyst refused could be fixed and still
    carried the refused file: the new version existed, and nothing attached
    it. The user's only way out was to discard the application and let a whole
    run prepare it again.

    Approval does not survive this. The user approved the documents that were
    there when they read it; these are different documents.

    Args:
        application_id: Application ID

    Returns:
        What is now attached, and the analyst's verdict on it

    Raises:
        HTTPException: If the job or a master resume is missing
    """
    from job_agent.models.database import DocumentType
    from job_agent.services.document_builder import DocumentBuilder

    application = _get_application(session, application_id)
    job = session.query(Job).filter(Job.id == application.job_id).first()

    if not job:
        raise HTTPException(
            status_code=404, detail=f"Job {application.job_id} not found"
        )

    builder = DocumentBuilder(session)

    try:
        versions = await (
            builder.build_fitted_package(job)
            if fit_to_posting
            else builder.build_application_package(job)
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    by_type = {version.doc_type: version for version in versions}
    resume = by_type.get(DocumentType.RESUME)
    cover = by_type.get(DocumentType.COVER_LETTER)

    application.resume_version_id = resume.id if resume else None
    application.resume_version = resume.pdf_path if resume else None
    application.cover_letter_version_id = cover.id if cover else None
    application.cover_letter_version = cover.pdf_path if cover else None

    _reattach_uploads(application, resume, cover)

    # What was approved is gone. Saying so is the point: silently keeping the
    # approval would let a document the user has never seen be released on the
    # strength of their having read a different one.
    application.reviewed_by_user = False
    application.reviewed_at = None
    application.updated_at = utcnow()

    session.commit()
    session.refresh(application)

    report = await ApplicationAnalyst(session).analyse_async(application)

    # What the fit rewrite actually achieved, taken from the notes the builder
    # recorded on the version. Saying "fit 54% → 91%" is the difference
    # between an action the user trusts and a button they press hopefully.
    fit_notes = (resume.tailoring_notes or []) if resume else []

    _audit(
        session,
        AuditAction.DOCUMENT_TAILORED,
        f"Regenerated documents for '{job.title}' and re-attached them to "
        f"application #{application_id} — approval was cleared",
        {
            "application_id": application_id,
            "resume_version_id": application.resume_version_id,
            "cover_letter_version_id": application.cover_letter_version_id,
            "ready": report.ready,
        },
    )

    return {
        "status": "regenerated",
        "id": application.id,
        "resume_version_id": application.resume_version_id,
        "cover_letter_version_id": application.cover_letter_version_id,
        "reviewed_by_user": False,
        "analysis": report.to_dict(),
        "fitted_to_posting": fit_to_posting,
        "tailoring_notes": fit_notes,
        "next_step": (
            "Read the new documents in the tray and approve again — the "
            "earlier approval was for documents that no longer exist."
        ),
    }


def _reattach_uploads(application: Application, resume, cover) -> None:
    """
    Point the form's upload fields at the newly rendered files.

    The submission path replays `filled_fields` onto the reopened form, so a
    document that is not named there is not uploaded, however correctly it was
    generated. A cover-letter field that was deferred for want of a letter is
    promoted to a filled field now that there is one.

    Args:
        application: The application, mutated in place
        resume: The new resume version, or None
        cover: The new cover letter version, or None
    """
    filled = dict(application.filled_fields or {})
    deferred = dict(application.deferred_fields or {})

    for pattern, version, label in (
        (r"resume|\bcv\b", resume, "resume"),
        (r"cover|motivation", cover, "cover letter"),
    ):
        if not version or not version.pdf_path:
            continue

        for source in (filled, deferred):
            for question, detail in list(source.items()):
                if not re.search(pattern, question, re.I):
                    continue

                # Only an upload. "Paste your resume below" is a textarea that
                # matches the same word, and writing a file path into it would
                # submit the string "/Users/…/resume_v44.pdf" as the answer.
                current = str(detail.get("value") or "")

                if detail.get("field_type") != "file" and current and not current.lower().endswith(
                    (".pdf", ".docx", ".doc", ".txt")
                ):
                    continue

                filled[question] = {
                    **detail,
                    "value": version.pdf_path,
                    "category": "known",
                    "reason": f"Regenerated {label} for this job",
                }
                deferred.pop(question, None)

    # SQLAlchemy tracks JSON columns by identity, so both must be reassigned
    # rather than mutated in place.
    application.filled_fields = filled
    application.deferred_fields = deferred


@router.post("/{application_id}/approve")
async def approve(
    application_id: int,
    payload: Optional[dict] = None,
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Mark an application reviewed and ready to send.

    This does **not** transmit anything to an employer. Through Phase 5 the
    user submits the form themselves in the open browser window; approval
    records that they have read the filled form and the answers.

    Required questions must be answered first — approving a form with a blank
    required field would produce a rejected or half-complete application.

    Args:
        application_id: Application ID
        payload: {"notes": str} optional reviewer notes

    Returns:
        Updated application state
    """
    application = _get_application(session, application_id)
    job = session.query(Job).filter(Job.id == application.job_id).first()

    deferred = application.deferred_fields or {}
    outstanding = [
        q for q, d in deferred.items()
        if d.get("required") and d.get("value_entered_by_user") in (None, "")
    ]

    if outstanding:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{len(outstanding)} required question(s) still unanswered: "
                f"{', '.join(outstanding)}"
            ),
        )

    application.reviewed_by_user = True
    application.reviewed_at = utcnow()
    application.updated_at = utcnow()

    if payload and payload.get("notes"):
        application.user_notes = payload["notes"]

    session.commit()
    session.refresh(application)

    _audit(
        session,
        AuditAction.APPLICATION_APPROVED,
        f"User approved the application for '{job.title if job else application.job_id}'",
        {
            "application_id": application.id,
            "filled": len(application.filled_fields or {}),
            "answered": len([
                d for d in deferred.values()
                if d.get("value_entered_by_user") not in (None, "")
            ]),
        },
    )

    return {
        "status": "approved",
        "id": application.id,
        "reviewed_at": application.reviewed_at.isoformat(),
        "submission_status": application.submission_status.value,
        "next_step": (
            "Submit the form yourself in the open browser window. "
            "Automatic submission arrives in Phase 6."
        ),
    }


@router.get("/{application_id}/eligibility")
async def submission_eligibility(
    application_id: int,
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Explain whether this application can be submitted, and by which path.

    Returns both verdicts: what the user may do right now, and what an
    unattended run would be allowed to do — each with the checks that passed
    and the reasons anything is blocked.

    Args:
        application_id: Application ID

    Returns:
        Gate decision detail
    """
    application = _get_application(session, application_id)
    account = session.query(PlatformAccount).filter(
        PlatformAccount.id == application.platform_account_id
    ).first()

    if not account:
        raise HTTPException(status_code=404, detail="Platform account not found")

    connector = create_connector_for_account(account)

    return SubmissionGate(session).describe(application, account, connector)


@router.get("/{application_id}/analysis")
async def application_analysis(
    application_id: int,
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Read the finished application the way its recipient will.

    The resume and letter that are actually attached, the answers that will
    actually be submitted, and the posting they answer — checked together
    rather than one step at a time. Blockers here stop submission; warnings
    are the user's call.

    Args:
        application_id: Application ID

    Returns:
        The readiness report
    """
    application = _get_application(session, application_id)

    return (await ApplicationAnalyst(session).analyse_async(application)).to_dict()


# A react-select renders its menu as visible [role="option"] nodes whose ids
# look like "react-select-<field>-option-2". Matching them by exact text is the
# only safe way in: the page's own bullet lists are also <li> elements, and a
# loose text filter clicked those instead — silently selecting nothing.
_VISIBLE_OPTIONS_JS = r"""() => Array.from(
    document.querySelectorAll('[role="option"]')
)
    .filter(el => el.offsetParent !== null)
    .map(el => ({id: el.id || '', text: (el.innerText || '').trim()}))"""

# What a control displays, including custom dropdowns that keep their chosen
# label in a sibling node rather than in the input's value.
_SHOWN_VALUE_JS = r"""(selector) => {
    const el = document.querySelector(selector);

    // An upload widget replaces its <input type=file> with a chip naming the
    // attached file, so the control the value was written to is gone —
    // evidence of success, not of failure.
    if (!el) return document.body.innerText.replace(/\s+/g, ' ').trim();

    const type = (el.type || '').toLowerCase();
    if (type === 'checkbox') return el.checked ? 'checked' : '';
    if (el.value) return el.value;

    const shell = el.closest('.select-shell') || el.parentElement;
    return shell ? (shell.innerText || '').replace(/\s+/g, ' ').trim() : '';
}"""


async def _choose_from_custom_dropdown(page, locator, value: str) -> None:
    """
    Pick a value from a dropdown that renders as a text input.

    Typing the value and pressing Enter looks natural and is wrong: on this
    form typing "Male" highlighted "Female", because the filter matches
    substrings and Enter takes whatever is highlighted. The menu is opened and
    the option with exactly that label is clicked instead.

    Args:
        page: Playwright page
        locator: The control to open
        value: The exact option label to select

    Raises:
        RuntimeError: If the menu has no option with that label
    """
    await locator.click(timeout=RESTORE_TIMEOUT_MS)
    await page.wait_for_timeout(400)

    options = await page.evaluate(_VISIBLE_OPTIONS_JS)

    # These labels arrive with non-breaking spaces and soft wrapping, so a
    # literal comparison misses an option the user plainly chose.
    wanted = _normalise(value)
    match = next((o for o in options if _normalise(o["text"]) == wanted), None)

    if match is None:
        match = next(
            (o for o in options if _normalise(o["text"]).startswith(wanted[:60])),
            None,
        )

    if match is None or not match["id"]:
        await page.keyboard.press("Escape")
        raise RuntimeError(f"no option labelled {wanted[:40]!r} in the open menu")

    await page.locator(f"#{css_escape(match['id'])}").first.click(
        timeout=RESTORE_TIMEOUT_MS
    )
    await page.wait_for_timeout(250)


def _normalise(text: str) -> str:
    """
    Fold the whitespace variants a form and a stored answer disagree about.

    Args:
        text: Raw label or stored value

    Returns:
        Lowercased text with non-breaking spaces and runs of whitespace
        collapsed to single spaces
    """
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip().lower()


def css_escape(value: str) -> str:
    """Escape an id for use in a CSS selector."""
    return re.sub(r"([^a-zA-Z0-9_-])", r"\\\1", value)


def _label_head(label: str) -> str:
    """
    The part of an option label a control still shows once it is chosen.

    Most labels are short, so verifying a selection means looking for the whole
    thing. EEO questions are the exception: their options are a category
    followed by its legal definition —

        Asian (Not Hispanic or Latino): A person having origins in any of the
        original peoples of the Far East, Southeast Asia, or the Indian
        Subcontinent…

    — and what the form displays afterwards is the category alone. Checking a
    40-character slice of that lands mid-definition and matches nothing, so a
    selection that took was reported as lost and blocked the submission.

    Args:
        label: A normalised option label

    Returns:
        The leading category, or the label itself when it has no definition
        hanging off it
    """
    head = re.split(r"\s*[:—–]\s+", label, maxsplit=1)[0].strip()

    # A head short enough to be ambiguous ("no", "other") is worse than no
    # check at all — it would match the wrong option's text in the same shell.
    return head if len(head) >= 8 else label[:40]


async def _wait_until_shown(
    page,
    selector: str,
    expected: str,
    attempts: int = 20,
) -> bool:
    """
    Poll until the control shows the written value, or give up.

    These widgets repaint asynchronously — an upload becomes a chip, a custom
    dropdown repaints its label — so reading straight after writing catches
    some of them mid-render and calls a good value a failure.

    Args:
        page: Playwright page
        selector: The control
        expected: What should be showing
        attempts: How many times to look, 300ms apart. An upload chip
            on this form appears around 1.8s after the file is set.

    Returns:
        True as soon as the value is visible
    """
    for attempt in range(attempts):
        if await _control_shows(page, selector, expected):
            return True

        await page.wait_for_timeout(300)

    return False


async def _control_shows(page, selector: str, expected: str) -> bool:
    """
    Whether the control now displays the value that was written to it.

    Args:
        page: Playwright page
        selector: The control
        expected: What should now be showing

    Returns:
        True if the value took
    """
    try:
        shown = await page.evaluate(_SHOWN_VALUE_JS, selector)
    except Exception:
        return False

    if not shown:
        return False

    shown = _normalise(shown)
    wanted = _normalise(expected)

    # A file input reports a path; a dropdown reports its label inside the
    # surrounding shell text; a text box reports exactly what was typed.
    if (
        wanted[:40] in shown
        or shown in wanted
        or shown == "checked"
        or Path(wanted).name in shown
        or _label_head(wanted) in shown
    ):
        return True

    # A phone widget reformats what it is given — "0104215890" is displayed as
    # "010-421 5890" — so the characters differ while the answer does not.
    digits_wanted = re.sub(r"\D", "", wanted)
    digits_shown = re.sub(r"\D", "", shown)

    return bool(digits_wanted) and digits_wanted in digits_shown


# Asks the page, not our own bookkeeping, whether the form is ready to send.
_PREFLIGHT_JS = r"""() => {
    const out = {empty_required: [], errors: [], filled: 0};

    const shownValue = (el) => {
        const type = (el.type || '').toLowerCase();
        if (type === 'checkbox') return el.checked ? 'checked' : '';
        if (type === 'file') return el.files && el.files.length ? 'file' : '';
        if (el.value) return el.value;

        // A custom dropdown keeps its chosen label beside the input.
        const shell = el.closest('.select-shell') || el.parentElement;
        const text = shell ? (shell.innerText || '') : '';
        const lines = text.split(String.fromCharCode(10)).map(s => s.trim())
            .filter(s => s && !/^select/i.test(s) && !s.endsWith('*'));
        return lines.length ? lines[lines.length - 1] : '';
    };

    const labelFor = (el) => {
        if (el.id) {
            const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
            if (l && l.innerText.trim()) return l.innerText.trim();
        }
        const w = el.closest('div');
        return (w ? w.innerText.trim() : '') || el.name || el.id || 'a field';
    };

    document.querySelectorAll('input, select, textarea').forEach(el => {
        const type = (el.type || '').toLowerCase();
        if (['submit','button','reset','image','hidden'].includes(type)) return;

        const style = getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') return;

        // The unnamed proxy input inside a custom select is not a question.
        if (!el.id && !el.name) return;

        const required = el.required || el.getAttribute('aria-required') === 'true';
        if (!required) return;

        if (shownValue(el)) out.filled += 1;
        else out.empty_required.push(labelFor(el).replace(/\s+/g, ' ').slice(0, 70));
    });

    document.querySelectorAll(
        '[role="alert"], [aria-invalid="true"], [class*="error" i]'
    ).forEach(el => {
        const t = (el.innerText || '').trim();
        if (t && t.length < 150 && el.offsetParent !== null) out.errors.push(t);
    });

    return out;
}"""


async def _preflight(page) -> dict:
    """
    Ask the form whether it is complete, immediately before submitting.

    Everything else in this route tracks what the agent *did*. This asks what
    the page actually holds — the only view that catches a value that slid off
    a control, a field the form added since, or an error it is already showing.

    Args:
        page: Playwright page on the filled form

    Returns:
        {"empty_required": [...], "errors": [...], "filled": int}
    """
    try:
        result = await page.evaluate(_PREFLIGHT_JS)
    except Exception as e:
        logger.warning(f"Pre-flight check could not read the form: {e}")
        return {"empty_required": [], "errors": [], "filled": 0}

    logger.info(
        f"Pre-flight: {result['filled']} required field(s) filled, "
        f"{len(result['empty_required'])} empty, {len(result['errors'])} error(s)"
    )

    return result


async def _refresh_against_live_form(page, values: list) -> list:
    """
    Re-describe each stored answer using the form as it is right now.

    Args:
        page: Playwright page on the application form
        values: (question, selector, value, options) as stored on the application

    Returns:
        The same answers, with each one's options taken from the live control
        where the form still has it
    """
    from job_agent.services.form_reader import FormReader

    try:
        live = {field.selector: field for field in await FormReader.read_fields(page)}
    except Exception as e:
        logger.warning(f"Could not re-read the form before submitting: {e}")
        return values

    refreshed = []

    for question, selector, value, options in values:
        field = live.get(selector)

        if field is None:
            refreshed.append((question, selector, value, options))
            continue

        if field.options and not options:
            logger.info(
                f"{question[:50]!r} is a dropdown on the live form — the stored "
                f"application had it as free text"
            )

        refreshed.append((question, selector, value, field.options or options))

    return refreshed


def _hand_back_for_a_choice(session, application: Application, questions: list) -> None:
    """
    Return questions to the tray with the choices the form really offers.

    Args:
        session: Database session
        application: The application being submitted
        questions: (question, live options) pairs the saved answer cannot satisfy
    """
    deferred = dict(application.deferred_fields or {})

    for question, options in questions:
        detail = dict(deferred.get(question) or {})
        detail["options"] = options
        detail["value_entered_by_user"] = None
        detail["reason"] = (
            "This is a dropdown on the form — it only accepts one of the "
            "choices below, and the answer saved earlier was not one of them."
        )
        deferred[question] = detail

    application.deferred_fields = deferred
    application.reviewed_by_user = False
    application.updated_at = utcnow()

    session.commit()

    logger.info(
        f"Application #{application.id}: handed {len(questions)} question(s) "
        f"back to the tray with their real options"
    )


async def _restore_reviewed_form(page, application: Application, session=None) -> None:
    """
    Reopen the application form and write back exactly what was reviewed.

    An application is filled during a run and submitted later — often after the
    browser that filled it has closed. Refusing to submit in that case makes
    the whole review step unusable, and re-running the filler would produce a
    *new* form the user has not seen. Replaying the stored values is the only
    version where what the user approved is what the employer receives.

    Args:
        page: Playwright page to drive
        application: The approved application, carrying every stored value
        session: Database session, so a question the form turns out to answer
            from a fixed list can be handed back with its real choices

    Raises:
        Exception: If the form cannot be reached or a required value cannot be
            written — the caller turns that into a refusal to submit
    """
    await page.goto(application.form_url, wait_until="domcontentloaded")
    await page.wait_for_timeout(2500)

    # Everything the agent filled, plus everything the user answered. The
    # options list travels with each value: a fixed-list control has to be
    # picked from its own list, while a plain box is simply typed into.
    values: list[tuple[str, str, str, list]] = []

    for question, detail in (application.filled_fields or {}).items():
        if detail.get("selector") and detail.get("value") is not None:
            values.append(
                (question, detail["selector"], str(detail["value"]), detail.get("options") or [])
            )

    for question, detail in (application.deferred_fields or {}).items():
        answer = detail.get("value_entered_by_user")
        if detail.get("selector") and answer not in (None, ""):
            values.append(
                (question, detail["selector"], str(answer), detail.get("options") or [])
            )

    # What a field is gets read from the form now, not from what was captured
    # when it was filled. An application queued yesterday carries yesterday's
    # idea of each control — and a dropdown recorded then as a plain text box
    # is typed into, which react-select discards the moment focus leaves. The
    # form in front of us is the only authority on what it will accept.
    values = await _refresh_against_live_form(page, values)

    restored, failed = 0, []
    needs_choice: list = []

    for question, selector, value, options in values:
        try:
            locator = page.locator(selector).first

            if not await locator.count():
                failed.append(question)
                continue

            kind = await locator.evaluate(
                "el => el.tagName.toLowerCase() + ':' + (el.type || '')"
            )

            # Whether this is a dropdown must come from the page, not from the
            # options list captured when the form was filled. That list is
            # sometimes empty — the probe missed it — and the control was then
            # typed into like a text box. react-select discards typed text the
            # moment focus leaves, so the value verified at write time and was
            # gone by the time the form was submitted.
            is_custom_dropdown = await locator.evaluate(
                """el => el.getAttribute('role') === 'combobox'
                    || el.hasAttribute('aria-expanded')
                    || !!el.closest('.select-shell')
                    || /select|combobox/i.test(el.className || '')"""
            )

            if kind.startswith("select"):
                await locator.select_option(label=value, timeout=RESTORE_TIMEOUT_MS)
            elif ":file" in kind:
                await locator.set_input_files(value, timeout=RESTORE_TIMEOUT_MS)
            elif ":checkbox" in kind:
                if value.lower() in ("yes", "true", "on", "1"):
                    await locator.check(timeout=RESTORE_TIMEOUT_MS)
            elif options or is_custom_dropdown:
                try:
                    await _choose_from_custom_dropdown(page, locator, value)
                except RuntimeError:
                    # The answer is free text but the form only accepts one of
                    # its own options. The agent must not guess which — "I am
                    # based in Malaysia." plausibly means "No", but that is a
                    # factual claim on a real application. Hand the question
                    # back with the choices the form actually offers.
                    needs_choice.append((question, options))
                    raise
            else:
                # An ordinary text box. Clicking it first is what made this
                # time out: fill() focuses on its own.
                await locator.fill(value, timeout=RESTORE_TIMEOUT_MS)

            # Writing without checking is how "restored 27 of 27" was
            # reported onto a form that was still empty: a stray click landed
            # on a bullet in the job description, raised nothing, and selected
            # nothing. A value counts as restored only once the control shows it.
            if await _wait_until_shown(page, selector, value):
                restored += 1
            else:
                failed.append(question)
                logger.warning(f"Value did not take on the form: {question[:50]!r}")
        except Exception as e:
            logger.info(f"Could not restore {question[:40]!r}: {type(e).__name__}")
            failed.append(question)

    logger.info(
        f"Restored {restored} of {len(values)} reviewed values onto "
        f"{application.form_url}"
    )

    # Everything is written and focus has settled. Re-read each control: a
    # value that only survived while its field had focus is not an answer.
    still_missing = []

    for question, selector, value, _ in values:
        if not await _control_shows(page, selector, value):
            still_missing.append(question)

    if still_missing:
        logger.warning(f"Values lost after focus moved: {still_missing}")
        failed = list(dict.fromkeys(failed + still_missing))

    if needs_choice and session is not None:
        _hand_back_for_a_choice(session, application, needs_choice)

        raise RuntimeError(
            f"{len(needs_choice)} question(s) turned out to be dropdowns the "
            f"form answers from a fixed list, and the saved answer is not one "
            f"of the choices. They are back in the tray with the real options "
            f"— pick one and release again. Nothing was submitted."
        )

    if failed:
        raise RuntimeError(
            f"{len(failed)} reviewed value(s) could not be written back to the "
            f"form — first: {failed[0][:60]!r}. Submitting now would send an "
            f"application that is not the one you approved."
        )


@router.post("/{application_id}/submit")
async def submit_application(
    application_id: int,
    accept_weak_fit: bool = Query(
        False,
        description="Send despite the analyst judging this posting a poor match",
    ),
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Submit a reviewed application to the employer.

    This is the irreversible step: the form is submitted under the user's name
    in their authenticated browser session. It requires an approved application
    that the analyst has read end to end: every required question answered, no
    document carrying unsupported claims, and nothing wrong with the resume,
    letter or answers that the user would not have chosen.

    Args:
        application_id: Application ID
        accept_weak_fit: Waives the one finding that is a judgement rather than
            a defect — that the resume evidences too little of what the posting
            asks for. Recorded in the audit log when used. Defects cannot be
            waived here; they are fixed by regenerating the documents.

    Returns:
        The submission result, including any confirmation reference
    """
    application = _get_application(session, application_id)
    account = session.query(PlatformAccount).filter(
        PlatformAccount.id == application.platform_account_id
    ).first()

    if not account:
        raise HTTPException(status_code=404, detail="Platform account not found")

    if not application.reviewed_by_user:
        raise HTTPException(
            status_code=400,
            detail="Approve this application first — submission follows review",
        )

    decision = SubmissionGate(session).check_user_directed(
        application, accept_weak_fit=accept_weak_fit
    )

    if not decision.allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot submit: {'; '.join(decision.blockers)}",
        )

    _audit(
        session,
        AuditAction.APPLICATION_ANALYSED,
        f"Pre-submission analysis passed for application #{application_id}"
        + (" (weak fit accepted by the user)" if accept_weak_fit else ""),
        {
            "application_id": application_id,
            "accept_weak_fit": accept_weak_fit,
            "analysis": decision.analysis,
        },
    )

    connector = create_connector_for_account(account)

    if not connector:
        raise HTTPException(
            status_code=400, detail=f"No connector registered for {account.platform}"
        )

    session_manager = await get_session_manager()

    # A public board has nothing to sign into; requiring a saved session for
    # one refused every submission to it.
    page = await session_manager.get_page(
        account.platform,
        needs_signin=connector.capabilities.requires_manual_signin,
    )

    if not page:
        raise HTTPException(
            status_code=409,
            detail=(
                f"No authenticated browser session for {account.platform} — "
                f"reconnect the platform and re-open the form"
            ),
        )

    connector.set_page(page)

    # The form was filled during a run, in a browser that has since closed.
    # Rather than refuse, reopen it and write back exactly what was reviewed —
    # then the promise "what you approved is what gets sent" is kept by
    # reconstruction rather than by luck.
    if application.form_url and page.url != application.form_url:
        try:
            await _restore_reviewed_form(page, application, session)
        except Exception as e:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Could not reopen the filled form ({application.form_url}) "
                    f"to submit what you approved: {e}"
                ),
            )

    # Final check before anything reaches the employer. Everything above put
    # values into the form; this asks the form itself whether it is complete,
    # because a required field left empty is a rejected application and the
    # rejection arrives silently.
    preflight = await _preflight(page)

    if preflight["empty_required"]:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Held back — {len(preflight['empty_required'])} required field(s) "
                f"are still empty on the form: "
                f"{'; '.join(preflight['empty_required'][:4])}"
                + ("…" if len(preflight["empty_required"]) > 4 else "")
                + ". Nothing was submitted."
            ),
        )

    if preflight["errors"]:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Held back — the form is showing an error: "
                f"{preflight['errors'][0]}. Nothing was submitted."
            ),
        )

    from job_agent.connectors.base import ApplicationSession as ConnectorSession
    from job_agent.connectors.base import JobPosting

    job = session.query(Job).filter(Job.id == application.job_id).first()

    connector_session = ConnectorSession(
        job=JobPosting(
            platform=account.platform,
            external_id=job.external_id if job else "",
            title=job.title if job else "",
            company=job.company if job else "",
            location=job.location if job else "",
        ),
        platform_account_id=account.id,
        form_url=application.form_url,
    )

    result = await connector.submit_application(connector_session)

    outcome_data = (connector_session.form_state or {}).get("submission", {})
    outcome = SubmissionOutcome(
        submitted=outcome_data.get("submitted", result.success),
        confirmed=outcome_data.get("confirmed", result.success),
        confirmation_ref=result.confirmation_ref,
        confirmation_url=result.confirmation_url,
        confirmation_message=result.confirmation_message,
        validation_errors=outcome_data.get("validation_errors", []),
        after_screenshot=outcome_data.get("after_screenshot"),
        error_message=result.error_message,
    )

    SubmissionRecorder(session).record(application, account, outcome, initiated_by="user")

    # A submitted application has proved which of its answers the user stands
    # behind. Everything still waiting in the tray asks most of the same
    # questions, so it inherits them now rather than asking again.
    carried = CarryResult()

    if outcome.submitted:
        profile = (
            session.query(CandidateProfile)
            .filter(CandidateProfile.id == application.candidate_profile_id)
            .first()
            if application.candidate_profile_id else None
        )

        carried = carry_answers_forward(
            session, profile, exclude_application_id=application_id
        )

        if carried.changed_anything:
            _audit(
                session,
                AuditAction.APPLICATION_REVIEWED,
                f"Carried {carried.answers_filled} answer(s) from the submitted "
                f"application onto {carried.applications_updated} still waiting",
                {"source_application_id": application_id, **carried.to_dict()},
            )

        session.commit()

    session.refresh(application)
    session.refresh(account)

    return {
        "status": application.submission_status.value,
        "id": application.id,
        "submitted": outcome.submitted,
        "confirmed": outcome.confirmed,
        "confirmation_ref": application.confirmation_ref,
        "confirmation_url": application.confirmation_url,
        "clean_submissions_count": account.clean_submissions_count,
        "warning": (
            None if outcome.confirmed
            else "Submitted, but no confirmation was found — verify with the employer"
        ),
        "errors": outcome.validation_errors,
        "carried_to_other_applications": carried.to_dict(),
    }


@router.post("/{application_id}/discard")
async def discard(
    application_id: int,
    payload: Optional[dict] = None,
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Discard an application without applying.

    Args:
        application_id: Application ID
        payload: {"reason": str} optional

    Returns:
        Updated application state
    """
    application = _get_application(session, application_id)
    job = session.query(Job).filter(Job.id == application.job_id).first()

    application.submission_status = ApplicationStatus.REJECTED
    application.reviewed_by_user = True
    application.reviewed_at = utcnow()
    application.updated_at = utcnow()

    reason = (payload or {}).get("reason")
    if reason:
        application.user_notes = reason

    session.commit()

    _audit(
        session,
        AuditAction.APPLICATION_DISCARDED,
        f"User discarded the application for "
        f"'{job.title if job else application.job_id}'"
        + (f": {reason}" if reason else ""),
        {"application_id": application.id, "reason": reason},
    )

    return {"status": "discarded", "id": application.id}
