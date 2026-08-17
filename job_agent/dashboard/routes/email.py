"""
Email Application API Routes (Phase 6b).

- POST /api/v1/email/drafts — Compose an application email for a job
- GET  /api/v1/email/drafts — List drafts
- GET  /api/v1/email/drafts/{id} — Full draft, exactly as it would be sent
- PATCH /api/v1/email/drafts/{id} — Edit the subject or body before sending
- POST /api/v1/email/drafts/{id}/approve — Approve sending
- POST /api/v1/email/drafts/{id}/send — Send an approved draft
- POST /api/v1/email/drafts/{id}/discard — Throw the draft away
- GET  /api/v1/email/threads — Sent applications and any replies
- POST /api/v1/email/check-replies — Poll the inbox for replies

An email application always stops for review, whatever the platform's
automation mode is set to, and `/send` refuses anything not explicitly
approved.
"""

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from job_agent.config import settings
from job_agent.dashboard.deps import get_session
from job_agent.models.database import (
    Application,
    EmailDraft,
    EmailDraftStatus,
    EmailThread,
    Job,
)
from job_agent.services.email_detector import EmailApplicationDetector
from job_agent.services.email_service import (
    EmailApplicationError,
    EmailApplicationService,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/email", tags=["email"])
SessionDep = get_session


def _draft_summary(draft: EmailDraft, job: Optional[Job] = None) -> dict:
    """Serialize a draft for listings."""
    return {
        "id": draft.id,
        "job_id": draft.job_id,
        "job_title": job.title if job else None,
        "company": job.company if job else None,
        "to_email": draft.to_email,
        "subject": draft.subject,
        "status": draft.status.value,
        "reviewed_by_user": draft.reviewed_by_user,
        "attachment_count": len(draft.attachments or []),
        "recipient_source": draft.recipient_source,
        "sent_at": draft.sent_at.isoformat() if draft.sent_at else None,
        "send_method": draft.send_method,
        "error_message": draft.error_message,
        "created_at": draft.created_at.isoformat(),
    }


def _get_draft(session: Session, draft_id: int) -> EmailDraft:
    """Fetch a draft or raise 404."""
    draft = session.query(EmailDraft).filter(EmailDraft.id == draft_id).first()

    if not draft:
        raise HTTPException(status_code=404, detail=f"Draft {draft_id} not found")

    return draft


# ============================================================================
# Drafting
# ============================================================================

@router.post("/drafts")
async def create_draft(
    job_id: int = Query(..., description="Job to apply for"),
    to_email: Optional[str] = Query(
        None, description="Recipient; detected from the posting when omitted"
    ),
    application_id: Optional[int] = Query(None, description="Link to an application"),
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Compose an application email and queue it for review.

    The recipient is detected from the posting when not supplied, and the
    reasoning is returned so the user can judge it before approving.

    Args:
        job_id: Job to apply for
        to_email: Recipient override
        application_id: Application to link the email to

    Returns:
        The drafted email
    """
    job = session.query(Job).filter(Job.id == job_id).first()

    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    application = None
    if application_id:
        application = session.query(Application).filter(
            Application.id == application_id
        ).first()

        if not application:
            raise HTTPException(
                status_code=404, detail=f"Application {application_id} not found"
            )

    try:
        draft = EmailApplicationService(session).create_draft(job, to_email, application)
    except EmailApplicationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # `status` is the draft's state throughout; `action` is what just happened.
    # Spreading the summary last would otherwise silently overwrite one with
    # the other.
    return {
        "action": "drafted",
        "next_step": "Review the draft, then approve and send it",
        **_draft_summary(draft, job),
    }


@router.get("/drafts")
async def list_drafts(
    status: Optional[str] = Query(None, description="Filter by status"),
    session: Session = Depends(SessionDep),
) -> dict:
    """
    List application email drafts.

    Args:
        status: Optional status filter

    Returns:
        Drafts and counts
    """
    query = session.query(EmailDraft)

    if status:
        query = query.filter(EmailDraft.status == status)

    drafts = query.order_by(EmailDraft.created_at.desc()).all()

    jobs = {
        job.id: job
        for job in session.query(Job).filter(
            Job.id.in_([d.job_id for d in drafts] or [0])
        ).all()
    }

    service = EmailApplicationService(session)

    return {
        "total": len(drafts),
        "awaiting_review": sum(
            1 for d in drafts if d.status == EmailDraftStatus.DRAFT
        ),
        "approved_unsent": sum(
            1 for d in drafts if d.status == EmailDraftStatus.APPROVED
        ),
        "hourly_quota_remaining": service.remaining_hourly_quota(),
        "hourly_limit": settings.email_rate_limit,
        "drafts": [_draft_summary(d, jobs.get(d.job_id)) for d in drafts],
    }


@router.get("/drafts/{draft_id}")
async def get_draft(
    draft_id: int,
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Get a draft exactly as it would be sent.

    Args:
        draft_id: Draft ID

    Returns:
        Full draft, including body and attachment details
    """
    draft = _get_draft(session, draft_id)
    job = session.query(Job).filter(Job.id == draft.job_id).first()

    attachments = [
        {
            "path": path,
            "name": Path(path).name,
            "exists": Path(path).exists(),
            "size_bytes": Path(path).stat().st_size if Path(path).exists() else None,
        }
        for path in (draft.attachments or [])
    ]

    missing = [a["name"] for a in attachments if not a["exists"]]

    return {
        **_draft_summary(draft, job),
        "body": draft.body,
        "attachments": attachments,
        "missing_attachments": missing,
        "user_notes": draft.user_notes,
        "job": {
            "id": job.id,
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "description": job.description,
        } if job else None,
    }


@router.patch("/drafts/{draft_id}")
async def edit_draft(
    draft_id: int,
    payload: dict,
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Edit a draft before it is sent.

    Editing clears any previous approval — an approval applies to the text the
    user actually read, not to whatever the draft says later.

    Args:
        draft_id: Draft ID
        payload: {"subject": str, "body": str, "to_email": str}

    Returns:
        The updated draft
    """
    draft = _get_draft(session, draft_id)

    if draft.status == EmailDraftStatus.SENT:
        raise HTTPException(status_code=400, detail="This email has already been sent")

    from job_agent.services.email_sender import EmailSender
    from job_agent.utils.dates import utcnow

    if "to_email" in payload:
        if not EmailSender.is_valid_address(payload["to_email"]):
            raise HTTPException(
                status_code=400, detail=f"'{payload['to_email']}' is not a valid address"
            )
        draft.to_email = payload["to_email"].strip()
        draft.recipient_source = "edited by you"

    if "subject" in payload:
        draft.subject = payload["subject"]

    if "body" in payload:
        draft.body = payload["body"]

    if draft.status == EmailDraftStatus.APPROVED:
        draft.status = EmailDraftStatus.DRAFT
        draft.reviewed_by_user = False

    draft.updated_at = utcnow()
    session.commit()
    session.refresh(draft)

    return {
        "action": "updated",
        "reapproval_required": draft.status == EmailDraftStatus.DRAFT,
        **_draft_summary(draft),
    }


# ============================================================================
# Approve, send, discard
# ============================================================================

@router.post("/drafts/{draft_id}/approve")
async def approve_draft(
    draft_id: int,
    payload: Optional[dict] = None,
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Approve a draft for sending.

    Args:
        draft_id: Draft ID
        payload: {"notes": str} optional

    Returns:
        The approved draft
    """
    draft = _get_draft(session, draft_id)

    try:
        draft = EmailApplicationService(session).approve(
            draft, (payload or {}).get("notes")
        )
    except EmailApplicationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "action": "approved",
        "next_step": "POST /send to deliver this email",
        **_draft_summary(draft),
    }


@router.post("/drafts/{draft_id}/send")
async def send_draft(
    draft_id: int,
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Send an approved draft.

    This is irreversible: the message leaves the user's own address. It
    requires an approved draft and available hourly quota.

    Args:
        draft_id: Draft ID

    Returns:
        Send outcome
    """
    draft = _get_draft(session, draft_id)

    try:
        draft = EmailApplicationService(session).send(draft)
    except EmailApplicationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if draft.status == EmailDraftStatus.FAILED:
        raise HTTPException(
            status_code=502,
            detail=f"The email could not be sent: {draft.error_message}",
        )

    return {
        "status": "sent",
        "id": draft.id,
        "to_email": draft.to_email,
        "sent_at": draft.sent_at.isoformat() if draft.sent_at else None,
        "send_method": draft.send_method,
        "message_id": draft.sent_message_id,
    }


@router.post("/drafts/{draft_id}/discard")
async def discard_draft(
    draft_id: int,
    payload: Optional[dict] = None,
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Discard a draft without sending.

    Args:
        draft_id: Draft ID
        payload: {"reason": str} optional

    Returns:
        The discarded draft
    """
    draft = _get_draft(session, draft_id)

    try:
        draft = EmailApplicationService(session).discard(
            draft, (payload or {}).get("reason")
        )
    except EmailApplicationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"status": "discarded", "id": draft.id}


# ============================================================================
# Threads and replies
# ============================================================================

@router.get("/threads")
async def list_threads(session: Session = Depends(SessionDep)) -> dict:
    """
    List sent application emails and any replies.

    Returns:
        Threads and reply counts
    """
    threads = session.query(EmailThread).order_by(EmailThread.sent_at.desc()).all()

    return {
        "total": len(threads),
        "awaiting_reply": sum(1 for t in threads if not t.reply_received_at),
        "replies_received": sum(1 for t in threads if t.reply_received_at),
        "threads": [
            {
                "id": t.id,
                "application_id": t.application_id,
                "recipient_email": t.recipient_email,
                "subject": t.subject,
                "sent_at": t.sent_at.isoformat(),
                "status": t.thread_status.value,
                "reply_received_at": (
                    t.reply_received_at.isoformat() if t.reply_received_at else None
                ),
                "reply_snippet": t.reply_snippet,
            }
            for t in threads
        ],
    }


@router.post("/check-replies")
async def check_replies(session: Session = Depends(SessionDep)) -> dict:
    """
    Poll the inbox for replies to sent applications.

    Returns:
        Threads that gained a reply
    """
    try:
        updated = EmailApplicationService(session).check_replies()
    except EmailApplicationError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "checked_at": None if not updated else updated[0].reply_received_at.isoformat(),
        "new_replies": len(updated),
        "threads": [
            {
                "id": t.id,
                "recipient_email": t.recipient_email,
                "reply_snippet": t.reply_snippet,
            }
            for t in updated
        ],
    }


# ============================================================================
# Detection preview
# ============================================================================

@router.get("/detect")
async def detect_recipient(
    job_id: int = Query(..., description="Job to inspect"),
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Show which address would be used for a job, and why.

    Useful before drafting: it lists every address found in the posting with
    its score, so a wrong pick is visible rather than silent.

    Args:
        job_id: Job to inspect

    Returns:
        The chosen address and all candidates
    """
    job = session.query(Job).filter(Job.id == job_id).first()

    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    text = " ".join(
        str(getattr(job, attribute, "") or "")
        for attribute in ("description", "requirements")
    )

    candidates = EmailApplicationDetector.rank(text)
    chosen = EmailApplicationDetector.detect(job)

    return {
        "job_id": job.id,
        "apply_method": job.apply_method,
        "chosen": {
            "email": chosen.email,
            "score": chosen.score,
            "reasons": chosen.reasons,
        } if chosen else None,
        "candidates": [
            {
                "email": c.email,
                "score": c.score,
                "confident": c.is_confident,
                "reasons": c.reasons,
                "context": c.context,
            }
            for c in candidates
        ],
    }
