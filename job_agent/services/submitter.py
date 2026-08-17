"""
Application Submitter (Phase 6).

Performs the irreversible step: clicks submit on a filled application form,
then tries to prove it worked.

Confirmation matters more than the click. A click that silently failed
validation looks identical to one that succeeded unless the page is inspected
afterwards, and an application the user believes was sent — but wasn't — is
worse than an obvious error. So the submitter:

1. Screenshots the form immediately before submitting (what was sent)
2. Clicks the submit control
3. Waits for the page to settle
4. Looks for confirmation: success wording, a reference number, a URL change,
   or the form disappearing
5. Screenshots again (what came back)

When confirmation can't be found, the submission is recorded as
`confirmed=False` with the evidence gathered. That application does **not**
count toward the platform's clean-submission track record — an unverified
submission is not a clean one.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

from job_agent.utils.dates import utcnow

logger = logging.getLogger(__name__)

# Controls that submit an application form
SUBMIT_SELECTORS = [
    "button[type='submit']",
    "input[type='submit']",
    "button:has-text('Submit application')",
    "button:has-text('Submit Application')",
    "button:has-text('Submit')",
    "button:has-text('Send application')",
    "button:has-text('Apply')",
]

# Wording that indicates the application was received
CONFIRMATION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"application (has been )?(received|submitted|sent)",
        r"thank you for (your )?appl",
        r"we('| ha)ve received your application",
        r"successfully (submitted|applied)",
        r"your application is (in|complete)",
        r"applied successfully",
    )
]

# A reference the user can quote back to the employer
REFERENCE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"(?:reference|confirmation|application)\s*(?:number|id|#|:)\s*([A-Z0-9][A-Z0-9\-_]{3,})",
        r"\b([A-Z]{2,}-\d{3,})\b",
    )
]

# Wording that means the form rejected the submission
VALIDATION_ERROR_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"(this field|these fields) (is|are) required",
        r"please (complete|fill|correct|enter)",
        r"required field",
        r"is not valid",
        r"there (was|were) .{0,20}error",
    )
]


@dataclass
class SubmissionOutcome:
    """Result of clicking submit on a form."""

    submitted: bool = False  # The submit control was clicked
    confirmed: bool = False  # Confirmation was found afterwards
    confirmation_ref: Optional[str] = None
    confirmation_url: Optional[str] = None
    confirmation_message: Optional[str] = None
    validation_errors: List[str] = field(default_factory=list)
    before_screenshot: Optional[str] = None
    after_screenshot: Optional[str] = None
    error_message: Optional[str] = None

    @property
    def is_clean(self) -> bool:
        """
        True only for a submission that completed and proved it.

        Only clean submissions advance a platform's track record.
        """
        return self.submitted and self.confirmed and not self.validation_errors

    def to_dict(self) -> dict:
        """Serialize for API responses and audit entries."""
        return {
            "submitted": self.submitted,
            "confirmed": self.confirmed,
            "clean": self.is_clean,
            "confirmation_ref": self.confirmation_ref,
            "confirmation_url": self.confirmation_url,
            "confirmation_message": self.confirmation_message,
            "validation_errors": self.validation_errors,
            "error_message": self.error_message,
        }


class ApplicationSubmitter:
    """Clicks submit and verifies the result."""

    def __init__(self, screenshots_dir: Optional[Path] = None, settle_ms: int = 8000):
        """
        Initialize the submitter.

        Args:
            screenshots_dir: Where before/after screenshots are written
            settle_ms: How long to wait for the page after clicking
        """
        from job_agent.config import settings

        self.screenshots_dir = screenshots_dir or (settings.documents_dir / "screenshots")
        self.settle_ms = settle_ms

    async def submit(self, page: Any) -> SubmissionOutcome:
        """
        Submit the form currently open, then verify it.

        Args:
            page: Playwright page on the filled application form

        Returns:
            SubmissionOutcome with whatever evidence could be gathered
        """
        outcome = SubmissionOutcome()

        url_before = page.url

        outcome.before_screenshot = await self._screenshot(page, "before_submit")

        control = await self._find_submit_control(page)

        if control is None:
            outcome.error_message = "No submit control found on the form"
            logger.error(f"{outcome.error_message} at {url_before}")
            return outcome

        try:
            await control.click()
            outcome.submitted = True
            logger.info(f"Clicked submit on {url_before}")
        except Exception as e:
            outcome.error_message = f"Clicking submit failed: {e}"
            logger.error(outcome.error_message)
            return outcome

        await self._settle(page, url_before)

        await self._verify(page, url_before, outcome)

        # Captured after verification, not before: the confirmation page can
        # arrive a moment after the submit control disappears, and a shot taken
        # too early files the pre-submit page as the evidence of submitting.
        outcome.after_screenshot = await self._screenshot(page, "after_submit")

        if outcome.is_clean:
            logger.info(
                "Submission confirmed"
                + (f" (ref {outcome.confirmation_ref})" if outcome.confirmation_ref else "")
            )
        else:
            logger.warning(
                f"Submission could not be confirmed at {page.url}: "
                f"{outcome.validation_errors or 'no confirmation wording found'}"
            )

        return outcome

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    async def _verify(self, page: Any, url_before: str, outcome: SubmissionOutcome) -> None:
        """
        Look for evidence the application was accepted.

        Args:
            page: Playwright page after clicking
            url_before: URL before the click
            outcome: Outcome to populate
        """
        try:
            body_text = await page.inner_text("body")
        except Exception as e:
            outcome.error_message = f"Could not read the page after submitting: {e}"
            return

        # Validation errors mean the form did not go through — but only when
        # the page says so in an error element. Scanning the whole body flagged
        # the posting's own "Please fill out the form below" and the legend
        # "* indicates a required field" on every single submission.
        try:
            flagged = await page.evaluate(
                """() => Array.from(document.querySelectorAll(
                    '[role="alert"], [aria-invalid="true"], [class*="error" i],'
                    + '[class*="invalid" i], [id*="error" i], [id="err"], [data-error]'
                ))
                    .filter(el => el.offsetParent !== null)
                    .map(el => (el.innerText || '').trim())
                    .filter(t => t && t.length < 200)
                    .slice(0, 6)"""
            )
        except Exception:
            flagged = []

        for text in flagged:
            for pattern in VALIDATION_ERROR_PATTERNS:
                if pattern.search(text):
                    outcome.validation_errors.append(text)
                    break

        if outcome.validation_errors:
            outcome.confirmed = False
            return

        # Explicit confirmation wording is the strongest signal
        for pattern in CONFIRMATION_PATTERNS:
            match = pattern.search(body_text)
            if match:
                outcome.confirmed = True
                outcome.confirmation_message = self._snippet(body_text, match.start())
                break

        # A reference number is good evidence on its own
        for pattern in REFERENCE_PATTERNS:
            match = pattern.search(body_text)
            if match:
                outcome.confirmation_ref = match.group(1)
                outcome.confirmed = True
                break

        # Navigating to a new page is weaker evidence, accepted only when the
        # form itself is gone
        if not outcome.confirmed and page.url != url_before:
            if not await self._has_form_fields(page):
                outcome.confirmed = True
                outcome.confirmation_message = (
                    f"Redirected to {page.url} and the form is no longer present"
                )

        outcome.confirmation_url = page.url

    @staticmethod
    async def _has_form_fields(page: Any) -> bool:
        """True if fillable inputs are still on the page."""
        try:
            return await page.locator(
                "input:not([type=hidden]):not([type=submit]), select, textarea"
            ).count() > 0
        except Exception:
            return False

    @staticmethod
    def _snippet(text: str, position: int, width: int = 140) -> str:
        """Extract readable context around a match."""
        start = max(0, position - 20)
        return " ".join(text[start:start + width].split())

    # ------------------------------------------------------------------
    # Page helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _find_submit_control(page: Any):
        """
        Locate the submit control.

        Returns:
            A Playwright locator, or None
        """
        for selector in SUBMIT_SELECTORS:
            try:
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    return locator
            except Exception as e:
                logger.debug(f"Submit selector {selector} unusable: {e}")
                continue

        return None

    async def _settle(self, page: Any, url_before: str = "") -> None:
        """
        Wait for the submission to actually resolve.

        `networkidle` alone returns almost immediately on a page that keeps a
        reCAPTCHA and analytics chattering, so verification ran while the form
        was still mid-submit and every send looked unconfirmed. Wait for
        something that means the submission moved: the page navigating, the
        submit control going away, or confirmation wording appearing.

        Args:
            page: Playwright page
            url_before: The URL at the moment of the click
        """
        deadline = max(self.settle_ms, 20000)
        waited = 0

        while waited < deadline:
            await page.wait_for_timeout(500)
            waited += 500

            try:
                if url_before and page.url != url_before:
                    logger.info(f"Page navigated after submit to {page.url}")
                    break

                # Nothing read out of the page body can end this wait. The
                # submit control gets relabelled while sending, and the
                # posting itself contains the words a confirmation would use —
                # "thank you" appears in the job description, which ended the
                # wait 1.2 seconds after the click every time. Navigation is
                # the only signal the page cannot fake, so anything else waits
                # out the deadline and lets _verify judge the result.
                pass
            except Exception:
                break

        try:
            await page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            logger.debug("networkidle wait timed out after submit; continuing")

    async def _screenshot(self, page: Any, label: str) -> Optional[str]:
        """
        Capture a screenshot for the audit record.

        Args:
            page: Playwright page
            label: Filename label

        Returns:
            Path written, or None
        """
        path = self.screenshots_dir / f"{label}_{utcnow().strftime('%Y%m%d_%H%M%S_%f')}.png"
        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            await page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception as e:
            logger.warning(f"Could not capture the {label} screenshot: {e}")
            return None
