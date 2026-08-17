"""
Health & Recovery API Routes (Phase 9).

- GET  /api/v1/health — Every platform's state, and what needs the user
- POST /api/v1/health/check — Re-check sessions now
- GET  /api/v1/interruptions — CAPTCHAs, MFA prompts and expired sessions
- GET  /api/v1/interruptions/{id}/screenshot — What the agent saw
- POST /api/v1/interruptions/{id}/resolve — "I've handled it"
- POST /api/v1/platforms/{platform}/reconnect — Open a browser to sign in
- POST /api/v1/platforms/{platform}/resume — Bring a platform back into service

This is what a "Reconnect" button in the dashboard calls. The agent opens the
browser window; the user signs in. Credentials are never handled here.
"""

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from job_agent.dashboard.deps import get_session
from job_agent.models.database import PlatformAccount, PlatformInterruption
from job_agent.services.session_monitor import SessionMonitor

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["health"])
SessionDep = get_session


def _get_account(session: Session, platform: str) -> PlatformAccount:
    """Fetch a platform account or raise 404."""
    account = session.query(PlatformAccount).filter(
        PlatformAccount.platform == platform
    ).first()

    if not account:
        raise HTTPException(status_code=404, detail=f"{platform} is not connected")

    return account


def _get_interruption(session: Session, interruption_id: int) -> PlatformInterruption:
    """Fetch an interruption or raise 404."""
    record = session.query(PlatformInterruption).filter(
        PlatformInterruption.id == interruption_id
    ).first()

    if not record:
        raise HTTPException(
            status_code=404, detail=f"Interruption {interruption_id} not found"
        )

    return record


# ============================================================================
# Health
# ============================================================================

@router.get("/health")
async def platform_health(session: Session = Depends(SessionDep)) -> dict:
    """
    Report every platform's state.

    `needs_reconnect` is what a dashboard shows a Reconnect button for.

    Returns:
        Per-platform health and aggregate counts
    """
    monitor = SessionMonitor(session)
    platforms = [health.to_dict() for health in monitor.health()]

    return {
        "total": len(platforms),
        "healthy": sum(1 for p in platforms if p["healthy"]),
        "needs_attention": sum(1 for p in platforms if not p["healthy"]),
        "open_interruptions": len(monitor.open_interruptions()),
        "pending_jobs": sum(p["pending_jobs"] for p in platforms),
        "platforms": platforms,
    }


@router.post("/health/check")
async def check_now(
    platform: Optional[str] = Query(None, description="Restrict to one platform"),
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Re-check platform sessions now.

    Anything newly wrong is recorded as an interruption; a platform that checks
    out fine is marked healthy again.

    Args:
        platform: Restrict to one platform

    Returns:
        Per-platform check results
    """
    results = await SessionMonitor(session).monitor(platform)

    return {
        "checked": len(results),
        "healthy": sum(1 for r in results if r["healthy"]),
        "results": results,
    }


# ============================================================================
# Interruptions
# ============================================================================

@router.get("/interruptions")
async def list_interruptions(
    platform: Optional[str] = Query(None, description="Filter by platform"),
    include_resolved: bool = Query(False, description="Include resolved ones"),
    session: Session = Depends(SessionDep),
) -> dict:
    """
    List interruptions awaiting the user.

    Args:
        platform: Filter by platform
        include_resolved: Also return resolved interruptions

    Returns:
        Interruptions, newest first
    """
    query = session.query(PlatformInterruption)

    if platform:
        query = query.filter(PlatformInterruption.platform == platform)

    if not include_resolved:
        query = query.filter(PlatformInterruption.resolved_at.is_(None))

    records = query.order_by(PlatformInterruption.detected_at.desc()).all()

    return {
        "total": len(records),
        "open": sum(1 for r in records if r.is_open),
        "interruptions": [SessionMonitor.describe(r) for r in records],
    }


@router.get("/interruptions/{interruption_id}/screenshot")
async def interruption_screenshot(
    interruption_id: int,
    session: Session = Depends(SessionDep),
) -> FileResponse:
    """
    Download the screenshot of what stopped the run.

    Args:
        interruption_id: Interruption ID

    Returns:
        The PNG image
    """
    record = _get_interruption(session, interruption_id)

    if not record.screenshot_path:
        raise HTTPException(status_code=404, detail="No screenshot was captured")

    path = Path(record.screenshot_path)

    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Screenshot missing from disk: {path}")

    return FileResponse(path=path, media_type="image/png", filename=path.name)


@router.post("/interruptions/{interruption_id}/resolve")
async def resolve_interruption(
    interruption_id: int,
    verify: bool = Query(
        True, description="Confirm the session works before closing this"
    ),
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Mark an interruption handled.

    By default the platform's session is checked first: a user who says "I've
    done it" but hasn't would otherwise send the next run straight back into
    the same wall. Pass verify=false to override.

    Args:
        interruption_id: Interruption ID
        verify: Check the session before closing

    Returns:
        Whether it was resolved, and whether that was verified
    """
    record = _get_interruption(session, interruption_id)

    if not record.is_open:
        return {
            "resolved": True,
            "verified": False,
            "message": f"Already resolved at {record.resolved_at.isoformat()}",
        }

    result = await SessionMonitor(session).resolve(record, resolved_by="user", verify=verify)

    if not result["resolved"]:
        raise HTTPException(status_code=409, detail=result["message"])

    return result


# ============================================================================
# Recovery
# ============================================================================

@router.post("/platforms/{platform}/reconnect")
async def reconnect_platform(
    platform: str,
    login_url: Optional[str] = Query(None, description="Where to open the browser"),
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Open a browser window so the user can sign in again.

    The agent opens the window and waits. It never enters credentials.

    Args:
        platform: Platform to reconnect
        login_url: Where to open

    Returns:
        Whether the window opened, and what to do next
    """
    account = _get_account(session, platform)

    result = await SessionMonitor(session).reconnect(account, login_url)

    if not result["opened"]:
        raise HTTPException(status_code=502, detail=result["message"])

    return {
        **result,
        "next_step": (
            f"After signing in, POST /api/v1/platforms/{platform}/resume to confirm "
            f"and bring the platform back into service"
        ),
    }


@router.post("/platforms/{platform}/resume")
async def resume_platform(
    platform: str,
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Bring a platform back into service once its session is healthy.

    Verifies the session, closes the interruptions that were blocking it, and
    reports the work waiting.

    Args:
        platform: Platform to resume

    Returns:
        Resume result, including pending work
    """
    account = _get_account(session, platform)

    result = await SessionMonitor(session).resume(account)

    if not result["resumed"]:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{platform} still isn't usable: {result['reason']}. "
                f"Reconnect it first."
            ),
        )

    return result
