"""
Audit, Export & Settings API Routes (Phase 10).

- GET /api/v1/audit — The audit log, filterable by platform, action and date
- GET /api/v1/audit/actions — Action types present, for building filters
- GET /api/v1/audit/summary — Activity totals
- GET /api/v1/exports — What can be exported, and its columns
- GET /api/v1/exports/{name} — Download a CSV
- GET /api/v1/settings — Current thresholds and limits
- PATCH /api/v1/settings — Change per-platform limits

Settings split in two, deliberately. Per-platform limits (daily searches,
applications, automation mode) live in the database and are editable here.
Process-level settings (the clean-submissions threshold, email rate limit,
fit-score threshold) come from the environment and are shown read-only: a
running agent shouldn't be able to lower its own safety threshold through an
API call.
"""

import html
import logging
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import func

from sqlalchemy.orm import Session

from job_agent.config import settings
from job_agent.dashboard.deps import get_session
from job_agent.models.database import (
    Application,
    AuditAction,
    AuditLog,
    AutomationMode,
    ConnectionStatus,
    Job,
    PlatformAccount,
)
from job_agent.services.exporter import Exporter
from job_agent.utils.dates import utcnow

logger = logging.getLogger(__name__)

# Entries that mean the agent stopped and needs a person. Deferring a question
# and queueing an application for review are logged as "paused" too, but those
# are the agent doing its job — counting them made a healthy register report
# "490 lines that stopped" out of 776, which reads as a broken product.
ATTENTION_ACTIONS = {
    AuditAction.INTERRUPTION_RAISED,
    AuditAction.PLATFORM_SKIPPED,
    AuditAction.DOCUMENT_FLAGGED,
    AuditAction.RUN_INTERRUPTED,
}

router = APIRouter(prefix="/api/v1", tags=["audit"])
SessionDep = get_session


# ============================================================================
# Audit log
# ============================================================================

@router.get("/audit")
async def list_audit(
    platform: Optional[str] = Query(None, description="Filter by platform"),
    action: Optional[str] = Query(None, description="Filter by action type"),
    days: Optional[int] = Query(None, ge=1, description="Only the last N days"),
    result: Optional[str] = Query(None, description="Filter by result"),
    needs_attention: bool = Query(
        False, description="Only lines where the agent stopped and needs a person"
    ),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Read the audit log, newest first.

    Args:
        platform: Filter by platform
        action: Filter by action type
        days: Only entries from the last N days
        result: Filter by result ("success", "partial", "paused", "failure")
        needs_attention: Only interruptions, skips, flags and failures
        skip: Skip this many
        limit: Return this many

    Returns:
        Entries and the total matching count
    """
    since = utcnow() - timedelta(days=days) if days else None

    query = Exporter(session).audit_query(platform=platform, action=action, since=since)

    if result:
        query = query.filter(AuditLog.result == result)

    if needs_attention:
        # The register is mostly a record of routine work. This is the view
        # that answers "is anything wrong?" without reading 700 lines.
        query = query.filter(
            (AuditLog.result == "failure")
            | (AuditLog.action.in_(list(ATTENTION_ACTIONS)))
        )

    total = query.count()
    entries = query.offset(skip).limit(limit).all()

    return {
        "total": total,
        "returned": len(entries),
        "entries": [
            {
                "id": entry.id,
                # "2026-08-14 14:32:15 linkedin search_run ..." reads as a log line
                "timestamp": entry.timestamp.isoformat(sep=" ", timespec="seconds"),
                "action": entry.action.value,
                "platform": entry.platform,
                "actor": entry.actor,
                "result": entry.result,
                "detail": entry.detail,
                "detail_json": entry.detail_json,
                "error_message": entry.error_message,
            }
            for entry in entries
        ],
    }


@router.get("/audit/actions")
async def audit_actions(session: Session = Depends(SessionDep)) -> dict:
    """
    List the action types and platforms actually present in the log.

    Filters built from this only offer values that will match something.

    Returns:
        Actions with counts, and the platforms seen
    """
    action_counts = (
        session.query(AuditLog.action, func.count(AuditLog.id))
        .group_by(AuditLog.action)
        .all()
    )

    platforms = [
        row[0] for row in
        session.query(AuditLog.platform).distinct().all()
        if row[0]
    ]

    return {
        "actions": sorted(
            [{"action": a.value, "count": c} for a, c in action_counts],
            key=lambda x: x["count"], reverse=True,
        ),
        "platforms": sorted(platforms),
        "results": ["success", "partial", "paused", "failure"],
    }


@router.get("/audit/summary")
async def audit_summary(
    days: int = Query(7, ge=1, le=365, description="Window to summarize"),
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Summarize recent activity.

    Args:
        days: Window in days

    Returns:
        Totals over the window
    """
    since = utcnow() - timedelta(days=days)

    entries = session.query(AuditLog).filter(AuditLog.timestamp >= since).all()

    by_action: dict = {}
    by_platform: dict = {}
    by_result: dict = {}

    for entry in entries:
        by_action[entry.action.value] = by_action.get(entry.action.value, 0) + 1
        by_result[entry.result] = by_result.get(entry.result, 0) + 1
        if entry.platform:
            by_platform[entry.platform] = by_platform.get(entry.platform, 0) + 1

    return {
        "window_days": days,
        "total_entries": len(entries),
        "by_action": by_action,
        "by_platform": by_platform,
        "by_result": by_result,
        "needs_attention": sum(
            1 for entry in entries
            if entry.result == "failure" or entry.action in ATTENTION_ACTIONS
        ),
    }


# ============================================================================
# Exports
# ============================================================================

@router.get("/exports")
async def list_exports(session: Session = Depends(SessionDep)) -> dict:
    """
    List the available exports and their columns.

    Returns:
        Export names, columns and row counts
    """
    exporter = Exporter(session)

    counts = {
        "applications": session.query(func.count(Application.id)).scalar() or 0,
        "jobs": session.query(func.count(Job.id)).scalar() or 0,
        "audit": session.query(func.count(AuditLog.id)).scalar() or 0,
    }

    return {
        "exports": [
            {
                "name": name,
                "columns": columns,
                "rows": counts.get(name),
                "url": f"/api/v1/exports/{name}",
            }
            for name, columns in exporter.available().items()
        ]
    }


@router.get("/exports/{name}", response_class=PlainTextResponse)
async def download_export(
    name: str,
    platform: Optional[str] = Query(None, description="Jobs/audit: filter by platform"),
    action: Optional[str] = Query(None, description="Audit: filter by action"),
    days: Optional[int] = Query(None, ge=1, description="Audit: last N days"),
    include_filtered: bool = Query(True, description="Jobs: include filtered-out jobs"),
    session: Session = Depends(SessionDep),
) -> PlainTextResponse:
    """
    Download an export as CSV.

    Args:
        name: applications | jobs | audit | runs | emails
        platform: Filter by platform (jobs, audit)
        action: Filter by action (audit)
        days: Only the last N days (audit)
        include_filtered: Include hard-filtered jobs (jobs)

    Returns:
        CSV content, as a file download
    """
    exporter = Exporter(session)

    filters: dict = {}
    if name == "jobs":
        filters = {"platform": platform, "include_filtered": include_filtered}
    elif name == "audit":
        filters = {
            "platform": platform,
            "action": action,
            "since": utcnow() - timedelta(days=days) if days else None,
        }

    try:
        rows = exporter.rows_for(name, **filters)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    content = Exporter.to_csv(rows)
    filename = f"job-agent-{name}-{utcnow().strftime('%Y%m%d')}.csv"

    return PlainTextResponse(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ============================================================================
# Settings
# ============================================================================

@router.get("/settings")
async def get_settings(session: Session = Depends(SessionDep)) -> dict:
    """
    Show current settings.

    Per-platform limits are editable; the safety thresholds are read-only here
    and come from the environment.

    Returns:
        Editable and read-only settings
    """
    accounts = session.query(PlatformAccount).order_by(PlatformAccount.platform).all()

    return {
        "platforms": [
            {
                "platform": account.platform,
                "automation_mode": account.automation_mode.value,
                "daily_search_limit": account.daily_search_limit,
                "daily_apply_limit": account.daily_apply_limit,
                "daily_message_limit": account.daily_message_limit,
                "clean_submissions_count": account.clean_submissions_count,
                "search_url": account.search_url,
                "status": account.status.value,
            }
            for account in accounts
        ],
        "thresholds": {
            "clean_submissions_threshold": settings.clean_submissions_threshold,
            "fit_score_threshold": settings.fit_score_threshold,
            "email_rate_limit": settings.email_rate_limit,
            "document_max_upload_mb": settings.document_max_upload_mb,
            "pdf_engine": settings.pdf_engine,
            "pdf_isolate_weasyprint": settings.pdf_isolate_weasyprint,
            "editable": False,
            "note": (
                "These come from the environment (.env). They're read-only here so a "
                "running agent can't lower its own review threshold through the API."
            ),
        },
    }


@router.patch("/settings/platforms/{platform}")
async def update_platform_settings(
    platform: str,
    payload: dict,
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Update a platform's limits or automation mode.

    Raising `automation_mode` to search_fill_submit does not bypass the
    clean-submissions gate — that still requires the platform's own reviewed
    track record.

    Args:
        platform: Platform to update
        payload: Any of daily_search_limit, daily_apply_limit,
            daily_message_limit, automation_mode, search_url

    Returns:
        The updated settings
    """
    account = session.query(PlatformAccount).filter(
        PlatformAccount.platform == platform
    ).first()

    if not account:
        raise HTTPException(status_code=404, detail=f"{platform} is not connected")

    for field in ("daily_search_limit", "daily_apply_limit", "daily_message_limit"):
        if field not in payload:
            continue

        value = payload[field]

        if not isinstance(value, int) or value < 0:
            raise HTTPException(
                status_code=400, detail=f"{field} must be a non-negative integer"
            )

        setattr(account, field, value)

    if "automation_mode" in payload:
        try:
            account.automation_mode = AutomationMode(payload["automation_mode"])
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"automation_mode must be one of "
                    f"{', '.join(m.value for m in AutomationMode)}"
                ),
            )

    if "search_url" in payload:
        # A URL copied out of a page's source arrives entity-encoded —
        # "?q=remote&amp;l=london" — and the second parameter then becomes one
        # called "amp;l", so the station searches with half its query silently
        # dropped. Decoding on the way in is cheap and the symptom is invisible.
        account.search_url = html.unescape(payload["search_url"]) or None

    # Whether a station needs signing into is a guess made once, when it was
    # added, and it is easy to get wrong: most consumer job boards are
    # searchable logged out, and ticking the box strands the station in
    # `needs_signin` for ever. There was no way to change the answer short of
    # deleting the station and adding it again, and the error the user got
    # ("reconnect it first") pointed at a reconnection that could never
    # succeed. Marking a station public clears that state in one step.
    if "paused" in payload:
        account.paused = bool(payload["paused"])

    if "requires_signin" in payload:
        account.requires_signin = bool(payload["requires_signin"])

        if account.requires_signin:
            # It may genuinely need an account. Only a real check can say
            # whether the saved session is good, so ask for one.
            if account.status == ConnectionStatus.CONNECTED:
                account.status = ConnectionStatus.NEEDS_SIGNIN
        elif account.status == ConnectionStatus.NEEDS_SIGNIN:
            # A public board has nothing to be signed out of.
            account.status = ConnectionStatus.CONNECTED
            account.last_error = None

    account.updated_at = utcnow()
    session.commit()
    session.refresh(account)

    return {
        "action": "updated",
        "platform": account.platform,
        "automation_mode": account.automation_mode.value,
        "daily_search_limit": account.daily_search_limit,
        "daily_apply_limit": account.daily_apply_limit,
        "daily_message_limit": account.daily_message_limit,
        "search_url": account.search_url,
        "requires_signin": bool(account.requires_signin),
        "paused": bool(account.paused),
        "connection_status": account.status.value if hasattr(account.status, "value") else account.status,
        "note": (
            "Submission still requires "
            f"{settings.clean_submissions_threshold} reviewed submissions on this "
            f"platform (currently {account.clean_submissions_count})."
        ),
    }
