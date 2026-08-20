"""
Session Manager for Playwright persistent contexts (Phase 1).

Manages per-platform authenticated browser sessions using Playwright's
persistent context feature. Each platform gets its own profile directory
where cookies, local storage, and other session data are preserved across runs.

No credentials are stored — only Keychain metadata (platform, timestamp, profile path).
Playwright handles the actual browser session.
"""

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Optional, Dict
import shutil

from playwright.async_api import async_playwright, BrowserContext
from job_agent.config import settings
from job_agent.utils.keychain import (
    store_platform_metadata,
    delete_platform_metadata,
)
from job_agent.models import ConnectionStatus
from job_agent.utils.dates import utcnow

logger = logging.getLogger(__name__)

# What the agent's browser says it is. Kept current-ish: a user agent naming a
# long-obsolete Chrome is itself a signal that trips front-door protection.
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


# Chromium writes the port it is listening on into the profile directory when
# started with --remote-debugging-port. It is how a later process finds a
# browser it did not start.
DEVTOOLS_PORT_FILE = "DevToolsActivePort"

# How long to wait for a browser we just spawned to start listening.
BROWSER_START_TIMEOUT_S = 20.0
BROWSER_START_POLL_S = 0.5


class SessionManager:
    """
    Manages persistent browser contexts per platform.
    
    One context per platform, stored in:
    ~/Library/Application Support/job-agent/profiles/{platform}/
    
    Supports:
    - Opening a new context (user signs in manually)
    - Checking session status (logged-in vs. logged-out)
    - Closing context
    - Disconnecting (deletes profile directory + Keychain metadata)
    """
    
    def __init__(self):
        """Initialize session manager."""
        # A persistent context is its own browser, so there is nothing else
        # to track: closing the context closes the window.
        self.contexts: Dict[str, BrowserContext] = {}
        # Browsers this process connected to rather than launched. They belong
        # to nobody: shutting down here must leave them running.
        self.adopted: Dict[str, object] = {}
        self.playwright_instance = None
    
    @property
    def profile_root(self) -> Path:
        """Root directory for all platform profiles."""
        root = settings.profile_dir
        root.mkdir(parents=True, exist_ok=True)
        return root
    
    def get_profile_dir(self, platform: str) -> Path:
        """Get profile directory for a specific platform."""
        profile_dir = self.profile_root / platform
        profile_dir.mkdir(parents=True, exist_ok=True)
        return profile_dir
    
    async def start_playwright(self):
        """Start Playwright instance (call once at app startup)."""
        if self.playwright_instance is None:
            self.playwright_instance = await async_playwright().start()
            logger.info("Playwright instance started")
    
    async def stop_playwright(self):
        """Stop Playwright instance and close all contexts (call at app shutdown)."""
        # Close all open contexts — except the ones this process merely
        # connected to. A window the user is working in must survive the
        # dashboard restarting: closing it here is what threw away a
        # half-finished application, and the answers typed into it, every time
        # the server came back up.
        for platform, context in self.contexts.items():
            if platform in self.adopted:
                logger.info(
                    f"Leaving {platform}'s browser running — this process did "
                    f"not start it"
                )
                continue

            try:
                await context.close()
                logger.info(f"Closed context for {platform}")
            except Exception as e:
                logger.error(f"Error closing context for {platform}: {e}")

        self.contexts.clear()
        self.adopted.clear()

        # Stop Playwright
        if self.playwright_instance:
            await self.playwright_instance.stop()
            self.playwright_instance = None
            logger.info("Playwright instance stopped")
    
    async def launch_browser_for_connection(self, platform: str) -> BrowserContext:
        """
        Launch a visible browser window for the user to sign in.
        
        This is called when the user clicks "Connect <Platform>".
        The browser window is visible and stays in the foreground.
        
        Args:
            platform: Platform name (e.g., "linkedin", "greenhouse")
        
        Returns:
            BrowserContext (persistent, for this platform)
        
        Raises:
            Exception: If Playwright is not started or browser launch fails
        """
        return await self._open_persistent_context(platform, headless=False)

    async def _open_persistent_context(
        self,
        platform: str,
        headless: bool,
    ) -> BrowserContext:
        """
        Open (or reuse) the platform's persistent browser context.

        The profile directory is the whole point: it is what carries the
        sign-in from the connect flow into later runs, including runs in a
        different process. That requires `launch_persistent_context` —
        `browser.new_context()` takes no user_data_dir and would start a
        blank session every time, so a signed-in platform would come back
        signed out.

        Args:
            platform: Platform name
            headless: Whether the window is hidden. Sign-in must be visible;
                a background search need not be.

        Returns:
            BrowserContext bound to the platform's profile directory

        Raises:
            RuntimeError: If Playwright has not been started
        """
        if self.playwright_instance is None:
            raise RuntimeError("Playwright not started. Call start_playwright() first.")

        existing = self.contexts.get(platform)

        if existing is not None:
            if await self._is_alive(existing):
                return existing

            # Reusing a dead context is how "Connect" opened no window at all.
            self.contexts.pop(platform, None)

        profile_dir = self.get_profile_dir(platform)

        # A window the user will work in outlives this process. Started as its
        # own browser and reached over the debugging port, it survives the
        # dashboard restarting — so a restart reconnects to the application
        # the user is halfway through instead of closing it on them.
        #
        # Headless runs stay owned: nothing is watching them, there is no work
        # in progress to lose, and a stray browser nobody closes is worse.
        if not headless:
            context = await self._adopt_or_spawn(platform, profile_dir)

            if context is not None:
                self.contexts[platform] = context
                return context

            logger.warning(
                f"Could not start a standalone browser for {platform}; "
                f"falling back to one this process owns"
            )

        try:
            # A persistent context IS the browser — there is no separate
            # Browser object to track, and closing the context closes it.
            context = await self.playwright_instance.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=headless,
                # Playwright's default headless user agent contains
                # "HeadlessChrome", and a great many job boards' front-door
                # protection returns 403 to it. SimplyHired serves a Cloudflare
                # block page to the default and the full 3,500-posting listing
                # to an ordinary Chrome string — the agent reported "no jobs
                # found" for a board the user could see was full of them.
                #
                # This is not evasion of a login or a CAPTCHA: the page is
                # public, the user can see it in their own browser, and the
                # agent is asking for exactly the same page. Where a site does
                # put up a CAPTCHA, the connector still stops and hands it to
                # the user rather than trying to get past it.
                user_agent=DESKTOP_USER_AGENT,
                # A window the user has to work in must be the size of their
                # window, not a number chosen here. With a fixed viewport the
                # page stays 1440x900 however large the window is opened, so a
                # tall overlay is simply cut off — a reCAPTCHA image challenge
                # rendered with its Verify button below the fold, and the user
                # could neither scroll to it nor resize their way out. Headless
                # runs keep a defined size, because there is no window to take
                # one from.
                **(
                    {"viewport": {"width": 1440, "height": 900}}
                    if headless
                    else {"no_viewport": True}
                ),
                locale="en-US",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                },
                args=[
                    # Removes the navigator.webdriver flag that marks the
                    # browser as automated. Same reasoning as the user agent.
                    "--disable-blink-features=AutomationControlled",
                ] + ([] if headless else ["--start-maximized"]),
                # Playwright's bundled Chromium, deliberately not the user's
                # installed Chrome. Chrome refuses to open a second instance
                # against a profile while the user's own window is running —
                # "profile is already in use by another instance of Chromium" —
                # so the agent could only work when the user's browser was
                # closed. A separate binary also keeps the agent's sessions
                # out of the browser the user reads their email in.
            )

            # A persistent context opens with one blank page; connectors expect
            # a page to be there.
            if not context.pages:
                await context.new_page()

            self.contexts[platform] = context

            logger.info(
                f"Opened persistent context for {platform} "
                f"(profile: {profile_dir}, headless={headless})"
            )

            return context
        except Exception as e:
            logger.error(f"Failed to launch browser for {platform}: {e}")
            raise
    
    async def _adopt_or_spawn(
        self,
        platform: str,
        profile_dir: Path,
    ) -> Optional[BrowserContext]:
        """
        Reach the platform's standalone browser, starting one if need be.

        Args:
            platform: Platform name
            profile_dir: Its profile directory

        Returns:
            The browser's context, or None if one could not be reached
        """
        context = await self._connect_to_running(platform, profile_dir)

        if context is not None:
            logger.info(f"Adopted the browser already running for {platform}")
            return context

        if not self._spawn_browser(profile_dir):
            return None

        waited = 0.0

        while waited < BROWSER_START_TIMEOUT_S:
            await asyncio.sleep(BROWSER_START_POLL_S)
            waited += BROWSER_START_POLL_S

            context = await self._connect_to_running(platform, profile_dir)

            if context is not None:
                logger.info(f"Started a standalone browser for {platform}")
                return context

        return None

    async def _connect_to_running(
        self,
        platform: str,
        profile_dir: Path,
    ) -> Optional[BrowserContext]:
        """
        Connect to a browser already running on this profile, if there is one.

        Args:
            platform: Platform name
            profile_dir: Its profile directory

        Returns:
            Its context, or None when nothing is listening
        """
        port_file = profile_dir / DEVTOOLS_PORT_FILE

        if not port_file.exists():
            return None

        try:
            port = int(port_file.read_text().splitlines()[0].strip())
        except Exception:
            # Left behind by a browser that has since exited.
            return None

        try:
            browser = await self.playwright_instance.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}", timeout=5000
            )
        except Exception as e:
            logger.debug(f"Nothing answering on port {port} for {platform}: {e}")
            return None

        context = (
            browser.contexts[0] if browser.contexts else await browser.new_context()
        )

        if not context.pages:
            await context.new_page()

        self.adopted[platform] = browser

        return context

    def _spawn_browser(self, profile_dir: Path) -> bool:
        """
        Start a browser on this profile as its own process.

        Detached deliberately (`start_new_session`): a child of the dashboard
        would be taken down with it, which is the whole thing this avoids.

        Args:
            profile_dir: The profile to open

        Returns:
            True if the process was started
        """
        # A port file from a browser that has since exited would send the next
        # connection attempt at a dead port for as long as it sits there.
        stale = profile_dir / DEVTOOLS_PORT_FILE
        stale.unlink(missing_ok=True)

        try:
            executable = self.playwright_instance.chromium.executable_path
        except Exception as e:
            logger.error(f"No browser executable available: {e}")
            return False

        try:
            subprocess.Popen(
                [
                    executable,
                    f"--user-data-dir={profile_dir}",
                    # Port 0: Chromium picks a free one and writes it into the
                    # profile, so two platforms never collide.
                    "--remote-debugging-port=0",
                    f"--user-agent={DESKTOP_USER_AGENT}",
                    "--lang=en-US",
                    "--accept-lang=en-US,en;q=0.9",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-blink-features=AutomationControlled",
                    "--start-maximized",
                    # Playwright launches with these two, and cookies are
                    # encrypted with whichever store was in use when they were
                    # written. Spawning without them opens the same profile
                    # against the real macOS Keychain, cannot decrypt a single
                    # cookie Playwright saved, and every connected platform
                    # comes back signed out.
                    "--password-store=basic",
                    "--use-mock-keychain",
                    "about:blank",
                ],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            return True
        except Exception as e:
            logger.error(f"Could not start a browser on {profile_dir}: {e}")
            return False

    @staticmethod
    async def _is_alive(context: BrowserContext) -> bool:
        """
        Whether a cached context still has a browser behind it.

        There is no reliable "is this closed" flag on a persistent context, and
        a dead one answers `.pages` with an empty list rather than raising — the
        same answer a live context gives just after its last tab was closed. So
        liveness is established by using it: opening a page succeeds on one and
        raises on the other, and the page it opens is one the caller needed
        anyway.

        Args:
            context: The cached context

        Returns:
            True if the context can still be used
        """
        try:
            if any(not page.is_closed() for page in context.pages):
                return True

            await context.new_page()

            return True
        except Exception as e:
            logger.debug(f"Cached context is no longer usable: {type(e).__name__}: {e}")
            return False

    def _has_saved_profile(self, platform: str) -> bool:
        """
        Whether this platform has a signed-in profile on disk.

        An empty directory doesn't count: `get_profile_dir` creates one as a
        side effect, so its mere existence proves nothing about sign-in.

        Args:
            platform: Platform name

        Returns:
            True if a non-empty profile directory exists
        """
        profile_dir = self.profile_root / platform
        return profile_dir.is_dir() and any(profile_dir.iterdir())

    async def get_context(
        self,
        platform: str,
        needs_signin: bool = True,
    ) -> Optional[BrowserContext]:
        """
        Get the context for a platform, reopening its saved profile if needed.

        Contexts live in memory only for as long as the process does, but the
        sign-in lives in the profile directory. Reopening from there is what
        lets a scheduled run — or any run after a restart — use a platform the
        user connected days ago, instead of reporting it as expired.

        Args:
            platform: Platform name
            needs_signin: Whether this platform requires a signed-in session.
                A public job board does not, so it gets a browser on request
                even though nobody has ever signed into it.

        Returns:
            BrowserContext, or None if the platform needs a sign-in it hasn't had
        """
        context = self.contexts.get(platform)

        if context is not None:
            if await self._is_alive(context):
                return context

            # The window was closed, or the browser died. The entry outlived
            # it, and every later call was handed a context with no pages —
            # reported to the user as "not connected", on a platform whose
            # sign-in was sitting on disk the whole time. Nothing reopened it,
            # so the platform stayed broken until the dashboard restarted.
            logger.info(
                f"The browser for {platform} is gone — reopening it from the "
                f"saved profile"
            )
            self.contexts.pop(platform, None)

        if needs_signin and not self._has_saved_profile(platform):
            logger.debug(f"No saved profile for {platform}; not connected")
            return None

        try:
            # Reopening for a run, not for sign-in: honour the headless
            # setting rather than forcing a window in front of the user.
            return await self._open_persistent_context(
                platform, headless=settings.headless
            )
        except Exception as e:
            logger.error(f"Could not reopen saved session for {platform}: {e}")
            return None
    
    async def check_session_status(
        self,
        platform: str,
        logged_in_indicator: str = None,
        logged_out_indicator: str = None
    ) -> ConnectionStatus:
        """
        Check if a session is still authenticated.
        
        Looks for logged-in/logged-out indicators on the page.
        Never reads credential fields — only checks public page elements.
        
        Args:
            platform: Platform name
            logged_in_indicator: CSS selector for logged-in element (e.g., ".user-menu")
            logged_out_indicator: CSS selector for logged-out element (e.g., ".login-form")
        
        Returns:
            ConnectionStatus.CONNECTED if authenticated
            ConnectionStatus.SESSION_EXPIRED if indicators show logged-out state
            ConnectionStatus.NOT_CONNECTED if no context exists
        """
        context = await self.get_context(platform)
        if context is None:
            logger.debug(f"No context for {platform}")
            return ConnectionStatus.NEEDS_SIGNIN
        
        try:
            # Try to get the main page
            pages = context.pages
            if not pages:
                logger.debug(f"No pages open for {platform}")
                return ConnectionStatus.NEEDS_SIGNIN
            
            page = pages[0]
            
            # Navigate to a simple page to check auth state
            # Most platforms' homepage or profile page will do
            try:
                await page.goto("about:blank")  # Neutral page to avoid errors
            except Exception:
                pass
            
            # If we have logged-in indicator, check for it
            if logged_in_indicator:
                try:
                    element = await page.query_selector(logged_in_indicator)
                    if element:
                        logger.info(f"{platform}: Logged-in indicator found")
                        return ConnectionStatus.CONNECTED
                except Exception:
                    pass
            
            # If we have logged-out indicator, check for it
            if logged_out_indicator:
                try:
                    element = await page.query_selector(logged_out_indicator)
                    if element:
                        logger.info(f"{platform}: Logged-out indicator found")
                        return ConnectionStatus.SESSION_EXPIRED
                except Exception:
                    pass
            
            # If no indicators provided, assume connected if context exists
            if not logged_in_indicator and not logged_out_indicator:
                logger.info(f"{platform}: Context exists, assuming connected (no indicators)")
                return ConnectionStatus.CONNECTED
            
            # Indicators not found
            logger.warning(f"{platform}: Session status indicators not found")
            return ConnectionStatus.NEEDS_SIGNIN
        
        except Exception as e:
            logger.error(f"Error checking session for {platform}: {e}")
            return ConnectionStatus.ERROR
    
    async def save_connection_metadata(
        self,
        platform: str,
        profile_dir: Path
    ) -> bool:
        """
        Save connection metadata to Keychain and database after successful connection.
        
        Called after user successfully signs in.
        
        Args:
            platform: Platform name
            profile_dir: Path to profile directory
        
        Returns:
            True if successful, False otherwise
        """
        try:
            metadata = {
                "platform": platform,
                "profile_dir": str(profile_dir),
                "connected_at": utcnow().isoformat(),
                "last_verified_at": utcnow().isoformat(),
            }
            
            if store_platform_metadata(platform, metadata):
                logger.info(f"Connection metadata saved for {platform}")
                return True
            else:
                logger.error(f"Failed to save metadata for {platform}")
                return False
        except Exception as e:
            logger.error(f"Error saving connection metadata for {platform}: {e}")
            return False
    
    async def disconnect_platform(self, platform: str) -> bool:
        """
        Disconnect a platform: close browser, delete profile dir, delete Keychain metadata.
        
        Called when user clicks "Disconnect" for a platform.
        
        Args:
            platform: Platform name
        
        Returns:
            True if successful, False otherwise
        """
        try:
            # Close browser context. Disconnecting is the one case where a
            # standalone browser *is* closed: its profile is about to be
            # deleted underneath it, and leaving it running would leave a
            # window signed into an account the user just disconnected.
            browser = self.adopted.pop(platform, None)

            if browser is not None:
                try:
                    await browser.close()
                    logger.info(f"Closed the standalone browser for {platform}")
                except Exception as e:
                    logger.error(f"Error closing browser for {platform}: {e}")

                self.contexts.pop(platform, None)
            elif platform in self.contexts:
                try:
                    await self.contexts[platform].close()
                    del self.contexts[platform]
                    logger.info(f"Closed context for {platform}")
                except Exception as e:
                    logger.error(f"Error closing context for {platform}: {e}")

            # Delete profile directory
            profile_dir = self.get_profile_dir(platform)
            if profile_dir.exists():
                try:
                    shutil.rmtree(profile_dir)
                    logger.info(f"Deleted profile directory for {platform}")
                except Exception as e:
                    logger.error(f"Error deleting profile directory for {platform}: {e}")
            
            # Delete Keychain metadata
            if delete_platform_metadata(platform):
                logger.info(f"Deleted Keychain metadata for {platform}")
            
            return True
        except Exception as e:
            logger.error(f"Error disconnecting {platform}: {e}")
            return False
    
    async def get_page(self, platform: str, needs_signin: bool = True):
        """
        Get the current page from a platform's context.

        Useful for connectors to interact with the page.

        Args:
            platform: Platform name
            needs_signin: Whether this platform requires a signed-in session

        Returns:
            Page object, or None if context doesn't exist
        """
        context = await self.get_context(platform, needs_signin=needs_signin)
        if context and context.pages:
            return context.pages[0]
        return None
    
    def _is_chrome_installed(self) -> bool:
        """Check if Google Chrome is installed on the system."""
        # Simple check — in production, could be more sophisticated
        chrome_paths = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            Path.home() / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome",
        ]
        return any(Path(p).exists() for p in chrome_paths)


# Singleton instance
_session_manager = None


async def get_session_manager() -> SessionManager:
    """Get or create the singleton SessionManager."""
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
        await _session_manager.start_playwright()
    return _session_manager


async def close_session_manager():
    """Close the singleton SessionManager (call at app shutdown)."""
    global _session_manager
    if _session_manager is not None:
        await _session_manager.stop_playwright()
        _session_manager = None
