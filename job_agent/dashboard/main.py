"""Dashboard package for Job Agent (Phase 3).

FastAPI-based web dashboard for:
- Connected account management (Phase 1)
- Search profile configuration & job search pipeline (Phase 3)
- Job history and filtering (Phase 3)
- Review queue for applications (Phase 5)
- Audit log view (Phase 10)
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from job_agent.core.session_manager import get_session_manager, close_session_manager

logger = logging.getLogger(__name__)

# Built by `npm run build --prefix frontend`; absent in a source checkout that
# hasn't built the UI, which the routes below handle rather than crashing.
STATIC_DIR = Path(__file__).parent / "static"


# Lifespan events
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle app startup and shutdown."""
    # Startup
    logger.info("Dashboard starting up")
    await get_session_manager()  # Warm the singleton so the first request is fast
    logger.info("Session manager initialized")
    
    yield
    
    # Shutdown
    logger.info("Dashboard shutting down")
    await close_session_manager()
    logger.info("Session manager closed")


# Create FastAPI app with lifespan
app = FastAPI(
    title="Job Agent Dashboard",
    description="Local macOS job search and application agent",
    version="0.1.0",
    lifespan=lifespan,
)

# Allow localhost frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:8080"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Import and include routes
from job_agent.dashboard.routes import (
    accounts, audit, connectors, documents, email, health, review, runs, search,
)

app.include_router(accounts.router)
app.include_router(search.router)
app.include_router(search.jobs_router)
app.include_router(documents.router)
app.include_router(review.router)
app.include_router(email.router)
app.include_router(connectors.router)
app.include_router(runs.router)
app.include_router(health.router)
app.include_router(audit.router)


# ============================================================================
# The operator's desk (built UI)
# ============================================================================

if (STATIC_DIR / "assets").is_dir():
    # Vite emits hashed filenames under /app/assets/, so these can be cached hard.
    app.mount(
        "/app/assets",
        StaticFiles(directory=STATIC_DIR / "assets"),
        name="ui-assets",
    )


@app.get("/", include_in_schema=False)
async def root():
    """Send a browser hitting the bare host to the UI."""
    return RedirectResponse(url="/app/")


@app.get("/app", include_in_schema=False)
@app.get("/app/{path:path}", include_in_schema=False)
async def operator_desk(path: str = ""):
    """
    Serve the single-page UI.

    Every /app/* path returns index.html so the client router owns the URL. When
    the UI hasn't been built, say exactly how to build it rather than 404ing —
    a source checkout is a normal state, not an error.
    """
    index = STATIC_DIR / "index.html"

    if not index.exists():
        return FileResponse(
            path=str(Path(__file__).parent / "unbuilt.html"),
            status_code=503,
            media_type="text/html",
        )

    # index.html names the hashed asset bundles, so a cached copy of it pins
    # the browser to an old build: the UI silently stays several versions
    # behind while the API moves on, and features appear to be missing. The
    # assets it points at are content-hashed and still cache hard.
    return FileResponse(
        str(index),
        media_type="text/html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "version": "0.1.0", "phase": 10}


@app.get("/api/v1/status")
async def api_status():
    """API status endpoint."""
    return {
        "phase": 10,
        "status": "Phase 10 - Audit Log & Export",
        "message": "All 10 phases complete; audit log, CSV export and settings available",
        "endpoints": {
            "GET /api/v1/accounts": "List connected accounts",
            "POST /api/v1/accounts/connect": "Start connection flow",
            "GET /api/v1/accounts/{platform}/status": "Check connection status",
            "POST /api/v1/accounts/{platform}/disconnect": "Disconnect platform",
            "PATCH /api/v1/accounts/{platform}/automation-mode": "Update automation mode",
            "GET /api/v1/search/profiles": "List search profiles",
            "POST /api/v1/search/profiles": "Create a search profile",
            "POST /api/v1/search/run": "Run a search on a platform",
            "GET /api/v1/search/stats": "Search statistics",
            "GET /api/v1/jobs": "List discovered jobs",
            "GET /api/v1/jobs/{job_id}": "Full job details",
            "POST /api/v1/documents/masters": "Upload a master resume or cover letter",
            "GET /api/v1/documents/masters": "List master documents",
            "POST /api/v1/documents/generate": "Generate tailored documents for a job",
            "GET /api/v1/documents/versions": "List generated variants",
            "GET /api/v1/documents/versions/{id}/pdf": "Download a rendered PDF",
            "GET/POST /api/v1/review/profile": "Candidate profile used to fill forms",
            "GET /api/v1/review": "Applications awaiting review",
            "GET /api/v1/review/{id}": "Filled form + source job posting",
            "POST /api/v1/review/{id}/answers": "Answer deferred questions",
            "POST /api/v1/review/{id}/approve": "Mark reviewed and ready to send",
            "POST /api/v1/review/{id}/discard": "Discard the application",
            "GET /api/v1/review/{id}/eligibility": "Why an application can or cannot be submitted",
            "POST /api/v1/review/{id}/submit": "Submit a reviewed application",
            "GET /api/v1/email/detect": "Which address a posting asks applications to go to",
            "POST /api/v1/email/drafts": "Compose an application email",
            "GET /api/v1/email/drafts": "List email drafts",
            "POST /api/v1/email/drafts/{id}/approve": "Approve sending",
            "POST /api/v1/email/drafts/{id}/send": "Send an approved email",
            "GET /api/v1/email/threads": "Sent applications and replies",
            "POST /api/v1/email/check-replies": "Poll the inbox for replies",
            "GET /api/v1/connectors": "Every connector, its capabilities and ToS risk notes",
            "GET /api/v1/connectors/{platform}": "One connector in detail",
            "POST /api/v1/runs": "Run the pipeline now across connected platforms",
            "GET /api/v1/runs": "Run history with summaries",
            "GET /api/v1/schedule": "Whether scheduled runs are installed and enabled",
            "POST /api/v1/schedule": "Install a schedule (does not enable it)",
            "POST /api/v1/schedule/enable": "Start running on schedule",
            "POST /api/v1/schedule/disable": "Stop running on schedule",
            "GET /api/v1/health": "Platform health and what needs your attention",
            "GET /api/v1/interruptions": "CAPTCHAs, MFA prompts and expired sessions",
            "POST /api/v1/interruptions/{id}/resolve": "Mark an interruption handled",
            "POST /api/v1/platforms/{platform}/reconnect": "Open a browser to sign in again",
            "POST /api/v1/platforms/{platform}/resume": "Bring a platform back into service",
            "GET /api/v1/audit": "Audit log, filterable by platform, action and date",
            "GET /api/v1/audit/summary": "Recent activity totals",
            "GET /api/v1/exports": "What can be exported as CSV",
            "GET /api/v1/exports/{name}": "Download a CSV export",
            "GET /api/v1/settings": "Thresholds and per-platform limits",
            "PATCH /api/v1/settings/platforms/{platform}": "Change a platform's limits",
        }
    }
