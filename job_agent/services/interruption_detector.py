"""
Interruption Detector (pulled forward from Phase 9 for Phase 8).

Recognizes when a page has stopped being a job board and become something the
agent must not push through: a CAPTCHA, an MFA prompt, a sign-in wall, or a
rate-limit block.

This exists because Phase 8 runs unattended. An agent that doesn't notice a
CAPTCHA will keep clicking, keep reloading, and keep tripping the same
detection — which is precisely the behaviour that gets a user's account
suspended. Detecting the wall and stopping is the difference between "the run
paused, here's why" and "your LinkedIn account is restricted".

The agent never attempts to solve a challenge. CAPTCHAs exist to establish that
a human is present; defeating one would be both a terms violation and a lie
told on the user's behalf. The only supported resolution is the user completing
it themselves.
"""

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class InterruptionKind(str, Enum):
    """What kind of wall the run hit."""

    CAPTCHA = "captcha"
    MFA = "mfa"
    SIGNIN_REQUIRED = "signin_required"
    RATE_LIMITED = "rate_limited"
    BLOCKED = "blocked"


# Selectors that are near-conclusive on their own
STRUCTURAL_SIGNALS = {
    InterruptionKind.CAPTCHA: [
        "iframe[src*='recaptcha']",
        "iframe[src*='hcaptcha']",
        "iframe[title*='captcha' i]",
        "div.g-recaptcha",
        "div.h-captcha",
        "#px-captcha",
        "[data-testid*='captcha' i]",
        "form#challenge-form",  # Cloudflare
        # Cloudflare and friends show an interstitial that verifies the browser
        # before any content loads. It is not a puzzle to solve — it resolves
        # on its own for a person and never for an unattended run — so the
        # agent has to recognise it and stop rather than read an empty page.
        "#cf-challenge-running",
        "#challenge-running",
        "div.cf-browser-verification",
        "#cf-please-wait",
        "iframe[src*='challenges.cloudflare.com']",
        "iframe[title*='challenge' i]",
    ],
    InterruptionKind.MFA: [
        "input[autocomplete='one-time-code']",
        "input[name*='otp' i]",
        "input[name*='verification_code' i]",
        "input[id*='two-step' i]",
    ],
    InterruptionKind.SIGNIN_REQUIRED: [
        "input[type='password']",
    ],
}

# Wording that indicates a wall, checked against visible text
TEXT_SIGNALS = {
    InterruptionKind.CAPTCHA: [
        r"i'?m not a robot",
        r"verify (you'?re|you are) (a )?human",
        r"complete the (security )?(check|challenge)",
        r"unusual traffic",
        r"prove you'?re not a robot",
        # The wording bot-protection services use while they vet the browser.
        r"security service to protect",
        r"verif(y|ies|ying) (that )?you are (not a bot|human)",
        r"verif(y|ies|ying) (you'?re|that you'?re) (not a bot|human)",
        r"checking your browser",
        r"enable javascript and cookies to continue",
        r"ray id",
    ],
    InterruptionKind.MFA: [
        r"two.?(factor|step) (authentication|verification)",
        r"enter the (\d+.?digit )?code",
        r"verification code (we )?sent",
        r"authenticator app",
        r"one.?time (pass)?code",
    ],
    InterruptionKind.SIGNIN_REQUIRED: [
        r"sign in to continue",
        r"log ?in to (continue|view|see)",
        r"session (has )?expired",
        r"please (sign|log) ?in",
        r"you'?re signed out",
    ],
    InterruptionKind.RATE_LIMITED: [
        r"too many requests",
        r"rate limit",
        r"slow down",
        r"try again (in a few|later)",
    ],
    InterruptionKind.BLOCKED: [
        r"access denied",
        r"you (have been|were) blocked",
        r"suspicious activity",
        r"your account (has been )?(restricted|suspended)",
    ],
}

# What the user is told to do, per kind
GUIDANCE = {
    InterruptionKind.CAPTCHA: (
        "Complete the challenge yourself in the open browser window, then mark it "
        "resolved. The agent will never solve a CAPTCHA for you. If the same site "
        "challenges you every visit, it does not permit automated access — read it "
        "yourself and use a company's own job board as a station instead."
    ),
    InterruptionKind.MFA: (
        "Enter your verification code in the open browser window, then mark it "
        "resolved."
    ),
    InterruptionKind.SIGNIN_REQUIRED: (
        "Your session has expired. Reconnect the platform and sign in again."
    ),
    InterruptionKind.RATE_LIMITED: (
        "The platform is asking us to slow down. This platform is paused; lower its "
        "daily limits before resuming."
    ),
    InterruptionKind.BLOCKED: (
        "The platform has blocked this session. Do not retry automatically — check "
        "the account in a normal browser first."
    ),
}

# How severe each kind is for the run
HALTS_PLATFORM = {
    InterruptionKind.CAPTCHA: True,
    InterruptionKind.MFA: True,
    InterruptionKind.SIGNIN_REQUIRED: True,
    InterruptionKind.RATE_LIMITED: True,
    InterruptionKind.BLOCKED: True,
}


@dataclass
class Interruption:
    """A detected wall."""

    kind: InterruptionKind
    url: str
    evidence: str
    guidance: str
    screenshot_path: Optional[str] = None

    @property
    def halts_platform(self) -> bool:
        """True when this platform must stop for the rest of the run."""
        return HALTS_PLATFORM.get(self.kind, True)

    def to_dict(self) -> dict:
        """Serialize for API responses and audit entries."""
        return {
            "kind": self.kind.value,
            "url": self.url,
            "evidence": self.evidence,
            "guidance": self.guidance,
            "screenshot_path": self.screenshot_path,
            "halts_platform": self.halts_platform,
        }


class InterruptionDetector:
    """Detects CAPTCHAs, MFA prompts, sign-in walls and blocks."""

    @staticmethod
    async def detect(page: Any) -> Optional[Interruption]:
        """
        Check whether the current page is a wall rather than content.

        Args:
            page: Playwright page

        Returns:
            An Interruption, or None if the page looks normal
        """
        if page is None:
            return None

        url = getattr(page, "url", "")

        # Structural signals first — an embedded CAPTCHA widget is unambiguous,
        # whereas page text can mention "verify you're human" in a help article.
        for kind, selectors in STRUCTURAL_SIGNALS.items():
            for selector in selectors:
                try:
                    if await page.locator(selector).count() > 0:
                        # A password field on a page full of jobs is a login
                        # widget, not a wall; require corroborating text
                        if kind == InterruptionKind.SIGNIN_REQUIRED:
                            break

                        return InterruptionDetector._build(kind, url, f"matched {selector}")
                except Exception as e:
                    logger.debug(f"Interruption selector {selector} unusable: {e}")
                    continue

        try:
            text = await page.inner_text("body")
        except Exception:
            return None

        lowered = " ".join(text.split())[:5000].lower()

        for kind, patterns in TEXT_SIGNALS.items():
            for pattern in patterns:
                match = re.search(pattern, lowered)
                if match:
                    snippet = lowered[max(0, match.start() - 40):match.start() + 120]
                    return InterruptionDetector._build(kind, url, snippet.strip())

        return None

    @staticmethod
    def _build(kind: InterruptionKind, url: str, evidence: str) -> Interruption:
        """Build an Interruption with its guidance."""
        interruption = Interruption(
            kind=kind,
            url=url,
            evidence=evidence,
            guidance=GUIDANCE.get(kind, "Resolve this in the browser, then mark it resolved."),
        )

        logger.warning(
            f"Run interrupted by {kind.value} at {url}: {evidence[:120]}"
        )

        return interruption

    @staticmethod
    async def capture(page: Any, interruption: Interruption, directory) -> Interruption:
        """
        Screenshot the wall so the user can see what stopped the run.

        Args:
            page: Playwright page
            interruption: The detected interruption
            directory: Where to write the screenshot

        Returns:
            The interruption, with screenshot_path set when capture worked
        """
        from pathlib import Path

        from job_agent.utils.dates import utcnow

        path = Path(directory) / (
            f"interruption_{interruption.kind.value}_"
            f"{utcnow().strftime('%Y%m%d_%H%M%S')}.png"
        )
        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            await page.screenshot(path=str(path), full_page=False)
            interruption.screenshot_path = str(path)
        except Exception as e:
            logger.warning(f"Could not capture the interruption screenshot: {e}")

        return interruption


async def detect_interruption(page: Any) -> Optional[Interruption]:
    """
    Convenience wrapper around InterruptionDetector.detect().

    Args:
        page: Playwright page

    Returns:
        An Interruption, or None
    """
    return await InterruptionDetector.detect(page)
