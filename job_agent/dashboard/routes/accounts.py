"""
Dashboard API routes for account management (Phase 1).

Endpoints:
- GET /api/v1/accounts — List all connected accounts
- POST /api/v1/accounts/custom — Add a station from a job board or careers URL
- POST /api/v1/accounts/connect — Start connection flow for a platform
- POST /api/v1/accounts/{platform}/disconnect — Disconnect a platform
- GET /api/v1/accounts/{platform}/status — Check connection status
"""

import logging
import re
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, BackgroundTasks
from sqlmodel import select

from job_agent.connectors import create_connector, is_connector_registered
from job_agent.models import PlatformAccount, ConnectionStatus, AutomationMode
from job_agent.core.session_manager import get_session_manager
from job_agent.dashboard.deps import SessionDep
from job_agent.services.session_monitor import SessionMonitor
from job_agent.utils.dates import utcnow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/accounts", tags=["accounts"])


@router.get("", response_model=List[Dict[str, Any]])
async def list_accounts(session: SessionDep) -> List[Dict[str, Any]]:
    """
    List all connected platform accounts.
    
    Returns:
        List of account info dicts
    """
    try:
        statement = select(PlatformAccount)
        accounts = session.exec(statement).all()
        
        result = []
        for account in accounts:
            result.append({
                "id": account.id,
                "platform": account.platform,
                "status": account.status.value if isinstance(account.status, ConnectionStatus) else account.status,
                "automation_mode": account.automation_mode.value if isinstance(account.automation_mode, AutomationMode) else account.automation_mode,
                "daily_search_limit": account.daily_search_limit,
                "daily_apply_limit": account.daily_apply_limit,
                "clean_submissions_count": account.clean_submissions_count,
                "last_verified_at": account.last_verified_at.isoformat() if account.last_verified_at else None,
                "last_error": account.last_error,
            })
        
        return result
    except Exception as e:
        logger.error(f"Error listing accounts: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _connector_for_url(url: str) -> str:
    """
    Pick the connector that knows this board, falling back to the generic one.

    A board on a known ATS is worth recognising: the generic connector can
    read a page but cannot fill an application form, so a station pointed at
    a Greenhouse board would search happily and then refuse to prepare
    anything — having given no hint that it never could.

    Args:
        url: The listings URL the user pasted

    Returns:
        A registered connector name
    """
    from job_agent.connectors.registry import get_registry

    host = (urlparse(url).netloc or "").lower()

    for platform, connector_class in get_registry().list_connectors().items():
        template = getattr(connector_class, "BOARD_URL_TEMPLATE", None)

        if not template:
            continue

        template_host = (urlparse(template).netloc or "").lower()

        # Workday's template puts the company in the hostname, so match on
        # the part that is actually fixed.
        fixed = template_host.replace("{board}.", "")

        if fixed and host.endswith(fixed):
            return platform

    return "generic_ats"


def _slugify(name: str) -> str:
    """
    Turn a station name into a platform identifier.

    Args:
        name: What the user called it

    Returns:
        A lowercase, underscore-separated slug
    """
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug[:48]


@router.post("/custom")
async def add_custom_station(
    payload: dict,
    session: SessionDep,
) -> Dict[str, Any]:
    """
    Add a station for a job board or careers page the agent doesn't ship with.

    The generic connector drives it: give it the URL of a search results page
    or a company's job listings and it reads postings the same way it reads a
    Greenhouse board. Putting `{query}` and `{location}` in the URL lets each
    run search with the profile's own terms; a plain URL is opened as-is.

    A station added here starts out needing sign-in only if you say it does.
    Most public boards don't, and those are searchable immediately.

    Args:
        payload: {name, url, requires_signin?}

    Returns:
        The new station

    Raises:
        HTTPException: If the name or URL is unusable, or the name is taken
    """
    name = (payload.get("name") or "").strip()
    url = (payload.get("url") or "").strip()
    requires_signin = bool(payload.get("requires_signin"))

    if not name:
        raise HTTPException(status_code=400, detail="A station needs a name")

    platform = _slugify(name)

    if not platform:
        raise HTTPException(
            status_code=400, detail="That name has no letters or digits in it"
        )

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(
            status_code=400,
            detail="The URL needs to start with http:// or https://",
        )

    if is_connector_registered(platform):
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{platform}' is a built-in platform — connect it from the "
                f"list below instead of adding it by hand"
            ),
        )

    existing = session.exec(
        select(PlatformAccount).where(PlatformAccount.platform == platform)
    ).first()

    if existing:
        raise HTTPException(
            status_code=409, detail=f"A station called '{platform}' already exists"
        )

    session_manager = await get_session_manager()

    connector_kind = _connector_for_url(url)

    account = PlatformAccount(
        platform=platform,
        # Nothing is registered under this name, so another connector reads
        # the site: the one that knows this ATS where the URL gives it away,
        # and the generic reader otherwise.
        connector_kind=connector_kind,
        requires_signin=requires_signin,
        search_url=url,
        # A public board has nothing to sign into and can be searched at once.
        # One that does need an account waits until the user has signed in.
        status=(
            ConnectionStatus.NEEDS_SIGNIN
            if requires_signin
            else ConnectionStatus.CONNECTED
        ),
        profile_dir=str(session_manager.get_profile_dir(platform)),
        automation_mode=AutomationMode.SEARCH_AND_ANALYZE,
    )

    session.add(account)
    session.commit()
    session.refresh(account)

    logger.info(f"Added custom station {platform} -> {url}")

    return {
        "status": "added",
        "platform": account.platform,
        "search_url": account.search_url,
        "connector_kind": connector_kind,
        "requires_signin": requires_signin,
        "connection_status": account.status.value,
        "message": (
            f"'{platform}' added — connect it to sign in, then run a search"
            if requires_signin
            else f"'{platform}' added — it will be searched on the next run"
        ),
    }


def _signin_landing_url(
    platform: str,
    account: Optional[PlatformAccount],
) -> Optional[str]:
    """
    Where to send the browser so the user can sign in.

    Guessing `https://{platform}.com` lands on a marketing site for half the
    platforms and nowhere at all for a station the user named themselves. The
    station's own URL is the best answer, then whatever the connector declares.

    Args:
        platform: Platform name
        account: Its existing account, if it has one

    Returns:
        A URL to open, or None to leave the browser on its blank page
    """
    if account and account.search_url and "{" not in account.search_url:
        return account.search_url

    if platform == "test_connector":
        return "http://localhost:8001"

    connector = create_connector(platform)

    if connector is None:
        return None

    for candidate in (
        getattr(connector, "search_url", None),
        getattr(connector, "SEARCH_URL_TEMPLATE", None),
        getattr(connector, "BOARD_URL_TEMPLATE", None),
    ):
        if not candidate:
            continue

        if "{" in candidate:
            # A template needs a query or a board name that only a run (or the
            # user's own station URL) can supply. Its origin is still the right
            # place to sign in.
            parsed = urlparse(candidate)
            if parsed.scheme and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}"
            continue

        return candidate

    return None


@router.post("/connect")
async def connect_platform(
    platform: str,
    background_tasks: BackgroundTasks,
    session: SessionDep
) -> Dict[str, Any]:
    """
    Start connection flow for a platform.
    
    1. Launches visible browser window
    2. User signs in manually
    3. Agent detects authenticated state
    4. Stores connection metadata to Keychain + database
    
    Args:
        platform: Platform name (e.g., "linkedin", "test_connector")
    
    Returns:
        Status info
    """
    try:
        logger.info(f"Starting connection flow for {platform}")
        
        # For now, check if already connected
        statement = select(PlatformAccount).where(PlatformAccount.platform == platform)
        existing = session.exec(statement).first()
        
        if existing and existing.status == ConnectionStatus.CONNECTED:
            return {
                "status": "already_connected",
                "platform": platform,
                "message": f"{platform} is already connected"
            }
        
        # Launch browser for connection
        session_manager = await get_session_manager()
        
        # Launch browser (user will sign in manually)
        try:
            context = await session_manager.launch_browser_for_connection(platform)
            
            page = context.pages[0] if context.pages else None
            landing = _signin_landing_url(platform, existing)

            if page and landing:
                await page.goto(landing, timeout=30000)

            return {
                "status": "browser_opened",
                "platform": platform,
                "landing_url": landing,
                "message": f"Browser opened for {platform}. Please sign in.",
                "action": "WAIT_FOR_USER_SIGNIN"
            }
        except Exception as e:
            logger.error(f"Error launching browser for {platform}: {e}")
            raise HTTPException(status_code=400, detail=f"Failed to launch browser: {str(e)}")
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in connect_platform: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{platform}/check-status")
async def check_platform_status(
    platform: str,
    session: SessionDep
) -> Dict[str, Any]:
    """
    Check the authentication status of a platform.
    
    Called after user completes login in the browser.

    The connector is asked to look at the live page rather than being taken at
    its word. A platform recorded as connected when nobody is signed in causes
    runs that search a logged-out page and come back empty, which is a much
    harder thing to diagnose than being told to finish signing in.

    Args:
        platform: Platform name

    Returns:
        Connection status
    """
    try:
        logger.info(f"Checking connection status for {platform}")

        session_manager = await get_session_manager()

        statement = select(PlatformAccount).where(PlatformAccount.platform == platform)
        account = session.exec(statement).first()

        profile_dir = session_manager.get_profile_dir(platform)

        # SessionMonitor works from an account, and on a first connection there
        # isn't one yet. This stand-in is never added to the session — it exists
        # to name the platform and the connector that should judge it.
        probe = account or PlatformAccount(
            platform=platform,
            status=ConnectionStatus.NEEDS_SIGNIN,
            profile_dir=str(profile_dir),
            automation_mode=AutomationMode.SEARCH_AND_ANALYZE,
        )

        check = await SessionMonitor(session).check_session(probe)

        if not check["healthy"]:
            # A CAPTCHA or MFA prompt is its own kind of "not yet" and carries
            # its own guidance; anything else is simply not signed in.
            guidance = (
                check["interruption"].guidance
                if check.get("interruption")
                else "Finish signing in in the browser window, then check again."
            )

            if account:
                account.status = ConnectionStatus.NEEDS_SIGNIN
                account.last_error = check["reason"]
                session.commit()

            return {
                "status": "not_connected",
                "platform": platform,
                "reason": check["reason"],
                "message": guidance,
            }

        if not account:
            account = PlatformAccount(
                platform=platform,
                status=ConnectionStatus.CONNECTED,
                profile_dir=str(profile_dir),
                automation_mode=AutomationMode.SEARCH_AND_ANALYZE,  # Default mode
            )
            session.add(account)
        else:
            account.status = ConnectionStatus.CONNECTED

        account.last_error = None
        account.last_verified_at = utcnow()

        session.commit()

        # Save to Keychain
        await session_manager.save_connection_metadata(platform, profile_dir)

        return {
            "status": "connected",
            "platform": platform,
            "message": f"{platform} is now connected"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error checking status for {platform}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{platform}/status", response_model=Dict[str, Any])
async def get_platform_status(
    platform: str,
    session: SessionDep
) -> Dict[str, Any]:
    """
    Get connection status for a platform.
    
    Args:
        platform: Platform name
    
    Returns:
        Status info
    """
    try:
        statement = select(PlatformAccount).where(PlatformAccount.platform == platform)
        account = session.exec(statement).first()
        
        if not account:
            return {
                "status": "not_connected",
                "platform": platform,
                "message": "Not connected"
            }
        
        return {
            "status": account.status.value if isinstance(account.status, ConnectionStatus) else account.status,
            "platform": platform,
            "automation_mode": account.automation_mode.value if isinstance(account.automation_mode, AutomationMode) else account.automation_mode,
            "daily_search_limit": account.daily_search_limit,
            "daily_apply_limit": account.daily_apply_limit,
            "clean_submissions_count": account.clean_submissions_count,
            "last_verified_at": account.last_verified_at.isoformat() if account.last_verified_at else None,
            "last_error": account.last_error,
        }
    
    except Exception as e:
        logger.error(f"Error getting status for {platform}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{platform}/disconnect")
async def disconnect_platform(
    platform: str,
    session: SessionDep
) -> Dict[str, Any]:
    """
    Disconnect a platform.
    
    1. Closes browser context
    2. Deletes profile directory
    3. Deletes Keychain metadata
    4. Updates database
    
    Args:
        platform: Platform name
    
    Returns:
        Status info
    """
    try:
        logger.info(f"Disconnecting {platform}")
        
        session_manager = await get_session_manager()
        
        # Disconnect and delete all local data
        success = await session_manager.disconnect_platform(platform)
        
        if success:
            # Update database
            statement = select(PlatformAccount).where(PlatformAccount.platform == platform)
            account = session.exec(statement).first()
            
            if account:
                session.delete(account)
                session.commit()
            
            return {
                "status": "disconnected",
                "platform": platform,
                "message": f"{platform} is now disconnected. All local data deleted."
            }
        else:
            raise HTTPException(status_code=400, detail="Failed to disconnect")
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error disconnecting {platform}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/{platform}/automation-mode")
async def update_automation_mode(
    platform: str,
    mode: str,
    session: SessionDep
) -> Dict[str, Any]:
    """
    Update the automation mode for a platform.
    
    Args:
        platform: Platform name
        mode: New automation mode (search_only, search_and_analyze, etc.)
    
    Returns:
        Updated account info
    """
    try:
        statement = select(PlatformAccount).where(PlatformAccount.platform == platform)
        account = session.exec(statement).first()
        
        if not account:
            raise HTTPException(status_code=404, detail=f"{platform} not connected")
        
        # Validate mode
        try:
            account.automation_mode = AutomationMode(mode)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid automation mode: {mode}")
        
        session.add(account)
        session.commit()
        
        logger.info(f"Updated {platform} automation mode to {mode}")
        
        return {
            "platform": platform,
            "automation_mode": account.automation_mode.value,
            "message": f"Automation mode updated to {mode}"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating automation mode: {e}")
        raise HTTPException(status_code=500, detail=str(e))
