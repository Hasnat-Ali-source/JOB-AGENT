"""
Playwright browser helper utilities (Phase 1).

Utility functions for common browser interactions:
- Waiting for navigation
- Taking screenshots
- Detecting elements
- Waiting for login indicators
"""

import logging
from typing import Optional, List
from pathlib import Path

from playwright.async_api import Page, Locator, TimeoutError as PlaywrightTimeoutError
from job_agent.utils.dates import utcnow

logger = logging.getLogger(__name__)


async def wait_for_navigation(
    page: Page,
    timeout: int = 30000,
    url_pattern: Optional[str] = None
) -> bool:
    """
    Wait for page navigation to complete.
    
    Useful after clicking a link or submitting a form.
    
    Args:
        page: Playwright Page object
        timeout: Max time to wait (ms)
        url_pattern: Optional URL pattern to wait for (e.g., "*/dashboard")
    
    Returns:
        True if navigation completed, False if timeout
    """
    try:
        if url_pattern:
            await page.wait_for_url(url_pattern, timeout=timeout)
        else:
            await page.wait_for_load_state("networkidle", timeout=timeout)
        
        logger.debug("Navigation completed successfully")
        return True
    except PlaywrightTimeoutError:
        logger.warning(f"Navigation timeout after {timeout}ms")
        return False
    except Exception as e:
        logger.error(f"Error waiting for navigation: {e}")
        return False


async def take_screenshot(
    page: Page,
    output_dir: Path,
    filename: Optional[str] = None
) -> Optional[Path]:
    """
    Take a screenshot of the current page.
    
    Used for capturing filled application forms before submission.
    
    Args:
        page: Playwright Page object
        output_dir: Directory to save screenshot
        filename: Filename (auto-generated if not provided)
    
    Returns:
        Path to saved screenshot, or None if failed
    """
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        
        if filename is None:
            timestamp = utcnow().strftime("%Y%m%d_%H%M%S")
            filename = f"screenshot_{timestamp}.png"
        
        filepath = output_dir / filename
        
        await page.screenshot(path=str(filepath), full_page=True)
        logger.info(f"Screenshot saved to {filepath}")
        
        return filepath
    except Exception as e:
        logger.error(f"Error taking screenshot: {e}")
        return None


async def wait_for_element(
    page: Page,
    selector: str,
    timeout: int = 30000,
    visible: bool = True
) -> bool:
    """
    Wait for an element to appear on the page.
    
    Useful for detecting page load completion or form appearance.
    
    Args:
        page: Playwright Page object
        selector: CSS selector
        timeout: Max time to wait (ms)
        visible: If True, wait for element to be visible (not just DOM)
    
    Returns:
        True if element found, False if timeout
    """
    try:
        if visible:
            await page.locator(selector).wait_for(state="visible", timeout=timeout)
        else:
            await page.locator(selector).wait_for(state="attached", timeout=timeout)
        
        logger.debug(f"Element found: {selector}")
        return True
    except PlaywrightTimeoutError:
        logger.warning(f"Element not found within {timeout}ms: {selector}")
        return False
    except Exception as e:
        logger.error(f"Error waiting for element {selector}: {e}")
        return False


async def find_elements(
    page: Page,
    selector: str
) -> List[Locator]:
    """
    Find all elements matching a selector.
    
    Args:
        page: Playwright Page object
        selector: CSS selector
    
    Returns:
        List of Locator objects
    """
    try:
        locators = await page.locator(selector).all()
        logger.debug(f"Found {len(locators)} elements matching {selector}")
        return locators
    except Exception as e:
        logger.error(f"Error finding elements {selector}: {e}")
        return []


async def get_text_content(
    page: Page,
    selector: str
) -> Optional[str]:
    """
    Get text content of an element.
    
    Args:
        page: Playwright Page object
        selector: CSS selector
    
    Returns:
        Text content, or None if not found
    """
    try:
        content = await page.locator(selector).text_content()
        return content.strip() if content else None
    except Exception as e:
        logger.error(f"Error getting text content from {selector}: {e}")
        return None


async def get_input_value(
    page: Page,
    selector: str
) -> Optional[str]:
    """
    Get the value of an input field.
    
    Args:
        page: Playwright Page object
        selector: CSS selector for input
    
    Returns:
        Input value, or None if not found
    """
    try:
        value = await page.locator(selector).input_value()
        return value if value else None
    except Exception as e:
        logger.error(f"Error getting input value from {selector}: {e}")
        return None


async def fill_input(
    page: Page,
    selector: str,
    value: str,
    clear_first: bool = True
) -> bool:
    """
    Fill an input field with a value.
    
    Args:
        page: Playwright Page object
        selector: CSS selector for input
        value: Value to fill
        clear_first: Clear existing value first
    
    Returns:
        True if successful, False otherwise
    """
    try:
        locator = page.locator(selector)
        
        if clear_first:
            await locator.clear()
        
        await locator.fill(value)
        logger.debug(f"Filled {selector} with value")
        return True
    except Exception as e:
        logger.error(f"Error filling input {selector}: {e}")
        return False


async def click_element(
    page: Page,
    selector: str,
    wait_for_navigation: bool = False,
    timeout: int = 30000
) -> bool:
    """
    Click an element.
    
    Args:
        page: Playwright Page object
        selector: CSS selector
        wait_for_navigation: If True, wait for page navigation after click
        timeout: Timeout for navigation wait (ms)
    
    Returns:
        True if successful, False otherwise
    """
    try:
        if wait_for_navigation:
            async with page.expect_navigation(timeout=timeout):
                await page.locator(selector).click()
        else:
            await page.locator(selector).click()
        
        logger.debug(f"Clicked {selector}")
        return True
    except Exception as e:
        logger.error(f"Error clicking {selector}: {e}")
        return False


async def detect_login_page(page: Page) -> bool:
    """
    Detect if the current page is a login/sign-in page.
    
    Looks for common login indicators.
    
    Args:
        page: Playwright Page object
    
    Returns:
        True if login page detected, False otherwise
    """
    try:
        login_indicators = [
            'input[type="password"]',
            'input[name*="password"]',
            'button:has-text("Sign in")',
            'button:has-text("Login")',
            'button:has-text("Log in")',
            'a[href*="login"]',
            'a[href*="signin"]',
        ]
        
        for indicator in login_indicators:
            try:
                element = await page.query_selector(indicator)
                if element:
                    logger.info("Login page detected")
                    return True
            except Exception:
                pass
        
        logger.debug("Login page not detected")
        return False
    except Exception as e:
        logger.error(f"Error detecting login page: {e}")
        return False


async def detect_authenticated_state(page: Page) -> bool:
    """
    Detect if user appears to be authenticated.
    
    Looks for common authenticated state indicators.
    
    Args:
        page: Playwright Page object
    
    Returns:
        True if authenticated state detected, False otherwise
    """
    try:
        auth_indicators = [
            'a[href*="profile"]',
            'a[href*="account"]',
            'button:has-text("Profile")',
            'button:has-text("Account")',
            'div[class*="user"]',
            'div[class*="avatar"]',
            '[data-testid*="user"]',
            '[data-testid*="account"]',
        ]
        
        for indicator in auth_indicators:
            try:
                element = await page.query_selector(indicator)
                if element:
                    logger.info("Authenticated state detected")
                    return True
            except Exception:
                pass
        
        logger.debug("Authenticated state not clearly detected")
        return False
    except Exception as e:
        logger.error(f"Error detecting authenticated state: {e}")
        return False
