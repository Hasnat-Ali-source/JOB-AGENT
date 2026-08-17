"""
Search API Routes (Phase 3).

Dashboard endpoints for job search and filtering:
- GET  /api/v1/search/profiles — List search profiles
- POST /api/v1/search/profiles — Create new search profile
- POST /api/v1/search/run — Run search on a platform
- GET  /api/v1/search/stats — Search statistics
- GET  /api/v1/jobs — List discovered jobs
- GET  /api/v1/jobs/{job_id} — Full job details
"""

import logging
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from job_agent.models.database import (
    SearchProfile, PlatformAccount, Job, AuditLog, AuditAction
)
from job_agent.dashboard.deps import get_session
from job_agent.core.search_pipeline import SearchPipeline
from job_agent.utils.dates import utcnow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/search", tags=["search"])
jobs_router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])
SessionDep = get_session


# ============================================================================
# Search Profile Routes
# ============================================================================

@router.get("/profiles")
async def list_search_profiles(
    session: Session = Depends(SessionDep),
) -> List[dict]:
    """
    List all search profiles.
    
    Returns:
        List of search profiles
    """
    profiles = session.query(SearchProfile).all()
    
    return [
        {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "target_titles": p.target_titles,
            "country": p.country,
            "region": p.region,
            "remote_pref": p.remote_pref,
            "is_active": p.is_active,
            "created_at": p.created_at.isoformat(),
        }
        for p in profiles
    ]


@router.post("/profiles")
async def create_search_profile(
    profile_data: dict,
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Create a new search profile.
    
    Args:
        profile_data: Profile data (name, target_titles, filters, etc.)
    
    Returns:
        Created profile
    """
    try:
        profile = SearchProfile(
            name=profile_data.get("name"),
            description=profile_data.get("description"),
            target_titles=profile_data.get("target_titles", []),
            alt_titles=profile_data.get("alt_titles", []),
            job_type=profile_data.get("job_type"),
            seniority=profile_data.get("seniority"),
            country=profile_data.get("country"),
            region=profile_data.get("region"),
            remote_pref=profile_data.get("remote_pref"),
            keywords=profile_data.get("keywords", []),
            exclusions=profile_data.get("exclusions", []),
            salary_min=profile_data.get("salary_min"),
            salary_max=profile_data.get("salary_max"),
            date_posted_within_days=profile_data.get("date_posted_within_days"),
        )
        
        session.add(profile)
        session.commit()
        
        logger.info(f"Created search profile: {profile.name}")
        
        return {
            "id": profile.id,
            "name": profile.name,
            "status": "created"
        }
    
    except Exception as e:
        logger.error(f"Error creating search profile: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/profiles/{profile_id}")
async def delete_search_profile(
    profile_id: int,
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Delete a search profile.

    Runs made with it are kept: the register is a record of what the agent
    actually did, and deleting a profile shouldn't rewrite that history.

    Args:
        profile_id: Profile to delete

    Returns:
        What was deleted

    Raises:
        HTTPException: If no such profile exists
    """
    profile = session.query(SearchProfile).filter(
        SearchProfile.id == profile_id
    ).first()

    if not profile:
        raise HTTPException(
            status_code=404, detail=f"Search profile {profile_id} not found"
        )

    name = profile.name

    session.delete(profile)
    session.commit()

    logger.info(f"Deleted search profile: {name}")

    return {"status": "deleted", "id": profile_id, "name": name}


# ============================================================================
# Search Execution Routes
# ============================================================================

@router.post("/run")
async def run_search(
    platform: str = Query(..., description="Platform to search on"),
    profile_id: int = Query(..., description="Search profile ID"),
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Run a search on a platform using a search profile.
    
    Process:
    1. Validate platform is connected
    2. Load search profile
    3. Run search pipeline (collect, filter, score, store)
    4. Return results
    
    Args:
        platform: Platform name (e.g., "linkedin", "greenhouse")
        profile_id: Search profile ID
    
    Returns:
        Search results (jobs_found, new_jobs, duplicates_skipped, etc.)
    """
    try:
        # Verify platform is connected
        account = session.query(PlatformAccount).filter(
            PlatformAccount.platform == platform
        ).first()
        
        if not account:
            raise HTTPException(status_code=404, detail=f"Platform {platform} not connected")
        
        # Load search profile
        search_profile = session.query(SearchProfile).filter(
            SearchProfile.id == profile_id
        ).first()
        
        if not search_profile:
            raise HTTPException(status_code=404, detail=f"Search profile {profile_id} not found")
        
        # Run search pipeline
        pipeline = SearchPipeline(session)
        result = await pipeline.search(account, search_profile)
        
        logger.info(
            f"Search complete: {result.jobs_found} found, "
            f"{result.new_jobs} new, {result.duplicates_skipped} duplicates"
        )
        
        return {
            "platform": platform,
            "profile_name": search_profile.name,
            "status": "success" if not result.errors else "partial",
            "jobs_found": result.jobs_found,
            "new_jobs": result.new_jobs,
            "duplicates_skipped": result.duplicates_skipped,
            "hard_filters_failed": result.hard_filters_failed,
            "limit_reached": result.limit_reached,
            "daily_search_limit": account.daily_search_limit,
            "errors": result.errors,
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error running search: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Job Discovery Routes
# ============================================================================

@jobs_router.get("")
async def list_jobs(
    platform: Optional[str] = Query(None, description="Filter by platform"),
    status: Optional[str] = Query(None, description="Filter by status"),
    hard_filter_pass: Optional[bool] = Query(None, description="Filter by hard filter pass"),
    min_fit_score: Optional[float] = Query(None, description="Minimum fit score"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    session: Session = Depends(SessionDep),
) -> dict:
    """
    List discovered jobs with optional filtering.
    
    Args:
        platform: Filter by platform
        status: Filter by status (new, reviewing, rejected, applied)
        hard_filter_pass: Filter by hard filter pass/fail
        min_fit_score: Minimum fit score (0-1)
        skip: Skip this many results
        limit: Return this many results
    
    Returns:
        Jobs and total count
    """
    query = session.query(Job)
    
    # Apply filters
    if platform:
        query = query.filter(Job.platform == platform)
    
    if status:
        query = query.filter(Job.status == status)
    
    if hard_filter_pass is not None:
        query = query.filter(Job.hard_filter_pass == hard_filter_pass)
    
    if min_fit_score is not None:
        query = query.filter(Job.fit_score >= min_fit_score)
    
    # Count total
    total = query.count()

    # Paginate and order
    jobs = query.order_by(Job.fit_score.desc()).offset(skip).limit(limit).all()

    # How far each posting has got. Without this a job with a tailored resume
    # and a filled application waiting for review looks exactly like one the
    # agent has never touched, and the work done on it is invisible until the
    # user thinks to look in another section.
    from job_agent.models.database import Application, DocumentVersion

    job_ids = [j.id for j in jobs]

    documented = {
        job_id
        for (job_id,) in session.query(DocumentVersion.job_id)
        .filter(DocumentVersion.job_id.in_(job_ids))
        .distinct()
    } if job_ids else set()

    applications = {
        application.job_id: application
        for application in session.query(Application)
        .filter(Application.job_id.in_(job_ids))
        .all()
    } if job_ids else {}

    def _stage(job) -> dict:
        application = applications.get(job.id)

        if application is not None:
            status = (
                application.submission_status.value
                if hasattr(application.submission_status, "value")
                else application.submission_status
            )
            return {"stage": status, "application_id": application.id}

        if job.id in documented:
            return {"stage": "documents_ready", "application_id": None}

        return {"stage": "found", "application_id": None}

    return {
        "total": total,
        "returned": len(jobs),
        "jobs": [
            {
                "id": j.id,
                "platform": j.platform,
                "title": j.title,
                "company": j.company,
                "location": j.location,
                "salary": j.salary,
                "fit_score": j.fit_score,
                "hard_filter_pass": j.hard_filter_pass,
                "status": j.status,
                "first_seen_at": j.first_seen_at.isoformat(),
                **_stage(j),
            }
            for j in jobs
        ]
    }


@jobs_router.get("/{job_id}")
async def get_job_details(
    job_id: int,
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Get full details of a job posting.
    
    Args:
        job_id: Job ID
    
    Returns:
        Full job details
    """
    job = session.query(Job).filter(Job.id == job_id).first()
    
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    return {
        "id": job.id,
        "platform": job.platform,
        "external_id": job.external_id,
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "job_type": job.job_type,
        "description": job.description,
        "requirements": job.requirements,
        "salary": job.salary,
        "apply_method": job.apply_method,
        "recruiter_contact": job.recruiter_contact,
        "fit_score": job.fit_score,
        "hard_filter_pass": job.hard_filter_pass,
        "status": job.status,
        "first_seen_at": job.first_seen_at.isoformat(),
        "raw_data": job.raw_data,
    }


# ============================================================================
# Statistics Routes
# ============================================================================

@router.get("/stats")
async def search_statistics(
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Get search statistics.
    
    Returns:
        Overall metrics for discovered jobs
    """
    # Total jobs by platform
    jobs_by_platform = session.query(
        Job.platform,
        func.count(Job.id).label("count")
    ).group_by(Job.platform).all()
    
    # Jobs by status
    jobs_by_status = session.query(
        Job.status,
        func.count(Job.id).label("count")
    ).group_by(Job.status).all()
    
    # Hard filter stats
    hard_filter_pass_count = session.query(func.count(Job.id)).filter(
        Job.hard_filter_pass == True
    ).scalar() or 0
    
    hard_filter_fail_count = session.query(func.count(Job.id)).filter(
        Job.hard_filter_pass == False
    ).scalar() or 0
    
    # Fit score distribution
    avg_fit_score = session.query(func.avg(Job.fit_score)).scalar() or 0.0
    
    # Recent search audits
    recent_searches = session.query(AuditLog).filter(
        AuditLog.action == AuditAction.SEARCH_RUN
    ).order_by(AuditLog.timestamp.desc()).limit(10).all()

    # Today's run totals (jobs_found / new_jobs / duplicates_skipped)
    midnight = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    todays_runs = session.query(AuditLog).filter(
        AuditLog.action == AuditAction.SEARCH_RUN,
        AuditLog.timestamp >= midnight,
    ).all()

    today = {
        "searches_run": len(todays_runs),
        "jobs_found": 0,
        "new_jobs": 0,
        "duplicates_skipped": 0,
        "hard_filters_failed": 0,
    }

    for run in todays_runs:
        detail = run.detail_json or {}
        for key in ("jobs_found", "new_jobs", "duplicates_skipped", "hard_filters_failed"):
            today[key] += int(detail.get(key, 0) or 0)

    return {
        "total_jobs": session.query(func.count(Job.id)).scalar() or 0,
        "jobs_by_platform": {p: c for p, c in jobs_by_platform},
        "jobs_by_status": {s: c for s, c in jobs_by_status},
        "hard_filter_pass": hard_filter_pass_count,
        "hard_filter_fail": hard_filter_fail_count,
        "avg_fit_score": float(avg_fit_score),
        "today": today,
        "recent_searches": [
            {
                "platform": s.platform,
                "timestamp": s.timestamp.isoformat(),
                "detail": s.detail,
                "detail_json": s.detail_json,
                "result": s.result,
            }
            for s in recent_searches
        ]
    }

@jobs_router.post("/{job_id}/prepare")
async def prepare_application(
    job_id: int,
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Prepare an application for one posting the user picked themselves.

    Applications were only ever created by a run, and a run chooses jobs by fit
    score. That left no way to act on a posting the user has read and decided
    they want — they could see it on the wire and do nothing with it. This is
    the same pipeline scoped to one job: tailor the documents, open the form,
    fill what is known, defer the rest, and queue it for review.

    It never submits. The application lands in the outgoing tray like any
    other, and release stays a separate, user-initiated act.

    Args:
        job_id: The posting to prepare

    Returns:
        The queued application and how much still needs answering

    Raises:
        HTTPException: If the job, its platform, or its documents are missing
    """
    from job_agent.core.orchestrator import PlatformOutcome, RunOrchestrator
    from job_agent.models.database import Application, PlatformAccount

    job = session.query(Job).filter(Job.id == job_id).first()

    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    existing = session.query(Application).filter(Application.job_id == job_id).first()

    if existing:
        raise HTTPException(
            status_code=409,
            detail=(
                f"An application for this job already exists (#{existing.id}, "
                f"{existing.submission_status.value}) — open it in the tray."
            ),
        )

    account = session.query(PlatformAccount).filter(
        PlatformAccount.platform == job.platform
    ).first()

    if not account:
        raise HTTPException(
            status_code=409,
            detail=f"{job.platform} is not connected — connect it under Stations",
        )

    orchestrator = RunOrchestrator(session)

    # Documents first: an application with no tailored resume attaches nothing.
    if not await orchestrator._generate_documents([job]):
        raise HTTPException(
            status_code=409,
            detail=(
                "Could not tailor a resume for this job — upload a master "
                "resume on the desk and make sure one is in use."
            ),
        )

    outcome = PlatformOutcome(platform=account.platform)

    if not await orchestrator._queue_applications(account, [job], outcome):
        raise HTTPException(
            status_code=409,
            detail=(
                "The form could not be filled: "
                + ("; ".join(outcome.errors[:2]) if outcome.errors else "no reason reported")
            ),
        )

    application = (
        session.query(Application)
        .filter(Application.job_id == job_id)
        .order_by(Application.id.desc())
        .first()
    )

    outstanding = [
        question for question, detail in (application.deferred_fields or {}).items()
        if detail.get("required") and detail.get("value_entered_by_user") in (None, "")
    ]

    return {
        "status": "queued_for_review",
        "application_id": application.id,
        "job_title": job.title,
        "questions_to_answer": len(outstanding),
        "message": (
            f"Prepared and waiting in the tray — {len(outstanding)} question(s) need you."
        ),
    }
