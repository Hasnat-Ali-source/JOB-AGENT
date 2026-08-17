"""
Run and Schedule API Routes (Phase 8).

- POST /api/v1/runs — Start a run now
- GET  /api/v1/runs — Run history with summaries
- GET  /api/v1/runs/{id} — One run in detail
- GET  /api/v1/schedule — Whether runs are installed and enabled
- POST /api/v1/schedule — Install a schedule (does not enable it)
- POST /api/v1/schedule/enable — Start running on schedule
- POST /api/v1/schedule/disable — Stop running on schedule
- DELETE /api/v1/schedule — Remove the schedule entirely

Installing and enabling are separate on purpose: writing a plist should never
be what starts an agent applying for jobs.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from job_agent.core.orchestrator import RunOrchestrator
from job_agent.core.scheduler import LaunchdScheduler, ScheduleTime
from job_agent.dashboard.deps import get_session
from job_agent.models.database import AgentRun, SearchProfile

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["runs"])
SessionDep = get_session


# ============================================================================
# Runs
# ============================================================================

@router.post("/runs")
async def start_run(
    profile_id: int = Query(..., description="Search profile to run"),
    platforms: Optional[List[str]] = Query(
        None, description="Restrict to these platforms"
    ),
    generate_documents: bool = Query(
        False, description="Tailor documents for jobs that pass the filters"
    ),
    queue_applications: bool = Query(
        False, description="Also fill application forms and queue them for review"
    ),
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Run the pipeline across the connected platforms now.

    This never submits applications: a run prepares work and stops. Submission
    is a separate, user-initiated act (see /api/v1/review/{id}/submit).

    Args:
        profile_id: Search profile to run
        platforms: Restrict to these platforms
        generate_documents: Also tailor documents for good matches
        queue_applications: Also fill and queue applications (never submits)

    Returns:
        The completed run summary
    """
    profile = session.query(SearchProfile).filter(
        SearchProfile.id == profile_id
    ).first()

    if not profile:
        raise HTTPException(status_code=404, detail=f"Search profile {profile_id} not found")

    run = await RunOrchestrator(session).run(
        profile,
        platforms=platforms,
        trigger="manual",
        generate_documents=generate_documents,
        queue_applications=queue_applications,
    )

    return RunOrchestrator.summary_dict(run)


@router.get("/runs")
async def list_runs(
    limit: int = Query(20, ge=1, le=200),
    session: Session = Depends(SessionDep),
) -> dict:
    """
    List recent runs, newest first.

    Args:
        limit: How many to return

    Returns:
        Run summaries and aggregate totals
    """
    runs = (
        session.query(AgentRun)
        .order_by(AgentRun.started_at.desc())
        .limit(limit)
        .all()
    )

    return {
        "total": len(runs),
        "needing_attention": sum(1 for r in runs if r.interruptions or r.errors),
        "runs": [RunOrchestrator.summary_dict(r) for r in runs],
    }


@router.get("/runs/{run_id}")
async def get_run(
    run_id: int,
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Get one run in detail.

    Args:
        run_id: Run ID

    Returns:
        Run detail
    """
    run = session.query(AgentRun).filter(AgentRun.id == run_id).first()

    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    return RunOrchestrator.summary_dict(run)


# ============================================================================
# Schedule
# ============================================================================

@router.get("/schedule")
async def get_schedule() -> dict:
    """
    Report the schedule's state.

    "installed" and "enabled" are separate: a plist can exist without launchd
    running it.

    Returns:
        Schedule status
    """
    return LaunchdScheduler().status()


@router.post("/schedule")
async def install_schedule(payload: dict) -> dict:
    """
    Install a schedule without enabling it.

    Args:
        payload: {"times": ["09:00", "18:00"], "profile": "Backend - Remote"}

    Returns:
        The resulting schedule status
    """
    raw_times = payload.get("times") or []

    if not raw_times:
        raise HTTPException(
            status_code=400,
            detail='Provide at least one run time, e.g. {"times": ["09:00", "18:00"]}',
        )

    try:
        times = [ScheduleTime.parse(t) for t in raw_times]
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    scheduler = LaunchdScheduler()

    try:
        scheduler.install(times, profile_name=payload.get("profile"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not install the schedule: {e}")

    return {
        "action": "installed",
        "next_step": "POST /api/v1/schedule/enable to start running on schedule",
        **scheduler.status(),
    }


@router.post("/schedule/enable")
async def enable_schedule() -> dict:
    """
    Start running on the installed schedule.

    Returns:
        The resulting schedule status
    """
    scheduler = LaunchdScheduler()
    success, message = scheduler.enable()

    if not success:
        raise HTTPException(status_code=400, detail=message)

    return {"action": "enabled", "message": message, **scheduler.status()}


@router.post("/schedule/disable")
async def disable_schedule() -> dict:
    """
    Stop running on schedule, leaving the schedule installed.

    Returns:
        The resulting schedule status
    """
    scheduler = LaunchdScheduler()
    success, message = scheduler.disable()

    if not success:
        raise HTTPException(status_code=400, detail=message)

    return {"action": "disabled", "message": message, **scheduler.status()}


@router.delete("/schedule")
async def remove_schedule() -> dict:
    """
    Remove the schedule entirely.

    Returns:
        The resulting schedule status
    """
    scheduler = LaunchdScheduler()
    success, message = scheduler.uninstall()

    if not success:
        raise HTTPException(status_code=400, detail=message)

    return {"action": "removed", "message": message, **scheduler.status()}
