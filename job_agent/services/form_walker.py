"""
Walking a multi-step application form the way a person does.

**What was missing.** The filler read the fields on the page in front of it,
filled them, took a screenshot and stopped. That is the whole of an application
on a Greenhouse board, and about a fifth of one anywhere else. Real forms —
Workday, iCIMS, Indeed, most in-house portals — are wizards: contact details,
then work history, then eligibility questions, then review, each behind a
**Next**. Reading only the first step meant the agent reported an application
filled when four screens of it had never been seen, and the user found out at
the point of release.

So this walks. It reads whatever step is on screen, fills what it can, finds
the control that goes forward, presses it, and reads the next step — until
there is no forward control left. Nothing about the sequence is hard-coded:
each step is discovered by reading the page as it arrives, which is what makes
it work on a form nobody wrote a connector for.

**Where it stops, and why each stop is deliberate:**

- **At the final step.** The last control on an application is Submit, and
  pressing it is the one thing this project does not do on its own. The walk
  ends with the form filled and open, and the user releases it.
- **When a required field could not be answered.** Clicking Next past a
  required question either fails validation or, worse, silently submits a form
  with a blank the employer will read. The step is handed back instead.
- **When the page stops changing.** A Next that does not advance means a
  validation error the agent cannot see. Retrying it forever is how an
  automated form-filler ends up submitting the same broken step forty times.
- **At a sign-in wall or a CAPTCHA.** Both are for the user. The agent detects
  and reports them; it does not try to get past either.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# A wizard longer than this is a sign something is looping rather than a form
# with genuinely that many screens.
MAX_STEPS = 12

# How long to let a step settle after pressing Next. Long enough for a client-
# rendered step to mount, short enough that a dead form is not a minute's wait.
SETTLE_MS = 1800
CLICK_TIMEOUT_MS = 6000

# How long a step is given to render before the walk reads it, and how often
# it is asked. Generous because the ceiling is only reached by a step that
# never arrives: a step that renders in two seconds costs two seconds.
# Measured against SmartApply, whose demographic step spins for over
# twenty-five seconds before its questions exist.
STEP_RENDER_MAX_MS = 45000
STEP_RENDER_POLL_MS = 1000

# Controls that carry on to the next step. Ordered: the more specific the
# wording, the more certain it is a forward control rather than something else
# that happens to be a button.
NEXT_PATTERNS = (
    r"^\s*(save (and|&) )?continue\s*$",
    r"^\s*next(\s+step|\s+page)?\s*(→|>|»)?\s*$",
    r"^\s*(save (and|&) )?next\s*$",
    r"^\s*continue to .{0,30}$",
    r"^\s*proceed\s*$",
    # The last step of an Indeed application does not say Continue: it says
    # "Review your application", and the walk stopped one press short of the
    # review page reporting that it could find neither a next step nor a
    # submit control. It is a forward control, not a submit — the review page
    # it opens is where Submit lives.
    r"^\s*review( your)?( application)?\s*$",
    r"^\s*(go )?forward\s*$",
)

# Controls that finish the application. Never pressed here — found so the walk
# knows it has reached the end, and so it can tell the user what the last
# control says.
SUBMIT_PATTERNS = (
    # "your" is not decoration: Indeed's SmartApply review page labels its
    # final control "Submit your application", and without this the walker
    # reached the last step of a complete application and reported that it
    # could find no submit control — while the route finder, whose pattern
    # did allow it, was naming the very same button.
    r"^\s*submit( your| my)? application\s*$",
    r"^\s*submit\s*$",
    r"^\s*(send|finish|complete)( application)?\s*$",
    r"^\s*apply( now)?\s*$",
    r"^\s*i'?m done\s*$",
)

# Controls that look forward but are not: pressing one leaves the application
# or throws away what has been typed.
NEVER_CLICK = re.compile(
    r"cancel|back|previous|discard|delete|remove|sign ?out|log ?out"
    r"|create (an )?account|sign ?up|register|forgot|reset"
    r"|add another|upload|browse|choose file|clear|reset form",
    re.IGNORECASE,
)

# A step that is really a wall. Both are the user's to clear.
SIGN_IN_WALL = re.compile(
    r"sign in to (continue|apply)|log in to (continue|apply)"
    r"|create an account to (continue|apply)|please sign in|please log in",
    re.IGNORECASE,
)

# A real challenge is large enough to interact with.
MIN_CAPTCHA_SIZE = 200

# What an actual challenge looks like in the DOM.
#
# Not merely "a recaptcha iframe exists": Greenhouse — and most ATSs — embed
# an *invisible* reCAPTCHA on every application form. Its badge is a visible
# 256x60 iframe in the corner of the page, and treating that as a challenge
# stopped the walk on step one of a form that had no challenge at all.
#
# reCAPTCHA splits the two cleanly: `/anchor` is the badge or checkbox,
# `/bframe` is the challenge overlay that actually asks something of a person.
CAPTCHA_MARKERS = (
    "iframe[src*='recaptcha'][src*='bframe']",
    "iframe[src*='hcaptcha'][src*='challenge']",
    "#cf-challenge-running",
    "#challenge-form",
    "[data-testid='captcha']",
)


@dataclass
class Step:
    """One screen of an application form, and what happened on it."""

    number: int
    url: str
    fingerprint: str
    fields_read: int = 0
    fields_filled: int = 0
    fields_deferred: int = 0
    advanced_by: str = ""  # The control pressed to leave this step

    def to_dict(self) -> dict:
        """Serialize for the register and the tray."""
        return {
            "step": self.number,
            "url": self.url,
            "fields_read": self.fields_read,
            "fields_filled": self.fields_filled,
            "fields_deferred": self.fields_deferred,
            "advanced_by": self.advanced_by,
        }


@dataclass
class WalkOutcome:
    """What a walk through a form found, and where it ended."""

    steps: List[Step] = field(default_factory=list)
    stopped_because: str = ""
    # The control the user will press to send it, when the walk reached the end
    final_control: str = ""
    needs_user: bool = False

    @property
    def step_count(self) -> int:
        """How many screens were read."""
        return len(self.steps)

    def describe(self) -> str:
        """One sentence for the tray."""
        if self.step_count <= 1:
            return f"Single-page form. {self.stopped_because}"

        return (
            f"Walked {self.step_count} steps of this form. {self.stopped_because}"
        )

    def to_dict(self) -> dict:
        """Serialize for the register and the tray."""
        return {
            "steps": [step.to_dict() for step in self.steps],
            "step_count": self.step_count,
            "stopped_because": self.stopped_because,
            "final_control": self.final_control,
            "needs_user": self.needs_user,
            "message": self.describe(),
        }


class FormWalker:
    """Drives a multi-step application form, one discovered step at a time."""

    def __init__(self, max_steps: int = MAX_STEPS) -> None:
        self.max_steps = max_steps

    async def walk(
        self,
        page: Any,
        fill_step: Callable[[Any], Awaitable[Any]],
        stop_on_required: bool = True,
    ) -> WalkOutcome:
        """
        Fill every step of the form currently open, stopping before submit.

        Args:
            page: Playwright page, positioned on the first step
            fill_step: Called with the page for each step; returns a
                FillOutcome. Injected rather than imported so the walker has no
                opinion about how a field gets filled, and so a test can drive
                it without a browser.
            stop_on_required: Hand the step back when a required question could
                not be answered. Clicking past one either trips validation or
                submits a blank the employer reads.

        Returns:
            A WalkOutcome. The page is left on the step where the walk stopped,
            so the user picks up exactly where the agent did.
        """
        outcome = WalkOutcome()
        seen: Set[str] = set()

        for number in range(1, self.max_steps + 1):
            wall = await self._blocked_by(page)

            if wall:
                outcome.stopped_because = wall
                outcome.needs_user = True
                break

            fingerprint = await self._fingerprint(page)
            step = Step(
                number=number,
                url=getattr(page, "url", ""),
                fingerprint=fingerprint,
            )

            # The same screen twice means Next did not advance — a validation
            # message the agent cannot see, or a control that was never a
            # forward control. Either way, pressing it again is worse than
            # stopping.
            if fingerprint in seen:
                outcome.steps.append(step)
                outcome.stopped_because = (
                    "the form did not move on — it is probably showing a "
                    "validation message. Read it and carry on by hand."
                )
                outcome.needs_user = True
                break

            seen.add(fingerprint)

            filled = await fill_step(page)

            step.fields_read = len(getattr(filled, "filled_fields", {})) + len(
                getattr(filled, "deferred_fields", {})
            )
            step.fields_filled = len(getattr(filled, "filled_fields", {}))
            step.fields_deferred = len(getattr(filled, "deferred_fields", {}))
            outcome.steps.append(step)

            outstanding = list(getattr(filled, "required_deferred", []) or [])

            if outstanding and stop_on_required:
                outcome.stopped_because = (
                    f"{len(outstanding)} required question(s) on this step need "
                    f"you: {'; '.join(outstanding[:3])}"
                    + ("…" if len(outstanding) > 3 else "")
                )
                outcome.needs_user = True
                break

            forward = await self._find_forward(page)

            if not forward:
                submit = await self._find_submit(page)
                outcome.final_control = submit or ""
                outcome.stopped_because = (
                    f"reached the last step — '{submit}' is the control that "
                    f"sends it, and that is yours to press."
                    if submit
                    else "no further step and no submit control found; "
                    "read the form before releasing."
                )
                break

            label, locator = forward
            step.advanced_by = label

            if not await self._press(page, locator, label):
                outcome.stopped_because = (
                    f"could not press '{label}' — the form may be waiting on "
                    f"something. Carry on by hand."
                )
                outcome.needs_user = True
                break

            logger.info(f"Form walker: step {number} done, pressed '{label}'")
        else:
            outcome.stopped_because = (
                f"stopped after {self.max_steps} steps — this form is longer "
                f"than expected, or it is looping."
            )
            outcome.needs_user = True

        logger.info(
            f"Form walk finished: {outcome.step_count} step(s), "
            f"{outcome.stopped_because}"
        )

        return outcome

    async def _fingerprint(self, page: Any) -> str:
        """
        A signature for the step on screen, to notice when it does not change.

        Built from the field names rather than the URL: a wizard that swaps its
        step client-side keeps one URL throughout, and a URL carrying a session
        token changes on every load without the step changing at all.

        Args:
            page: Playwright page

        Returns:
            A stable string for this step
        """
        try:
            names = await page.evaluate(
                """() => Array.from(
                    document.querySelectorAll('input,select,textarea')
                ).filter(e => {
                    // Only what is on screen. A client-rendered wizard keeps
                    // every step in the DOM and hides all but one, so counting
                    // them all gives the same signature on every step — and
                    // the walk concluded it had not advanced when it had.
                    if (e.type === 'hidden') return false;
                    if (e.offsetParent === null &&
                        getComputedStyle(e).position !== 'fixed') return false;
                    const box = e.getBoundingClientRect();
                    return box.width > 0 && box.height > 0;
                }).map(e => (e.name || e.id || e.getAttribute('aria-label') || '')
                ).filter(Boolean).sort().join('|')"""
            )
        except Exception:
            names = ""

        url = (getattr(page, "url", "") or "").split("?")[0]

        return f"{url}#{names}"

    async def _blocked_by(self, page: Any) -> Optional[str]:
        """
        Whether this step is a wall rather than a form.

        Args:
            page: Playwright page

        Returns:
            A reason to stop, or None
        """
        # Presence is not a challenge. Greenhouse — and most ATSs — embed an
        # invisible reCAPTCHA on every form, sized 0x0 or hidden off-screen.
        # Treating the iframe's existence as a CAPTCHA stopped the walk on the
        # very first step of a form that had no challenge at all.
        for selector in CAPTCHA_MARKERS:
            try:
                frames = await page.locator(selector).all()
            except Exception:
                continue

            for frame in frames:
                try:
                    if not await frame.is_visible():
                        continue

                    box = await frame.bounding_box()
                except Exception:
                    continue

                if box and box.get("width", 0) >= MIN_CAPTCHA_SIZE and box.get(
                    "height", 0
                ) >= MIN_CAPTCHA_SIZE:
                    return (
                        "this step is showing a CAPTCHA. Solve it in the open "
                        "window and the agent will carry on from there."
                    )

        try:
            text = await page.inner_text("body", timeout=CLICK_TIMEOUT_MS)
        except Exception:
            return None

        if SIGN_IN_WALL.search(text or ""):
            return (
                "this form wants you signed in before it will accept an "
                "application. Connect the station and sign in, then try again."
            )

        return None

    async def _find_forward(self, page: Any):
        """
        The control that goes to the next step, if there is one.

        Args:
            page: Playwright page

        Returns:
            (label, locator) or None
        """
        candidates = await self._clickables(page)

        for label, locator in candidates:
            if NEVER_CLICK.search(label):
                continue

            if any(re.match(pattern, label, re.IGNORECASE) for pattern in NEXT_PATTERNS):
                return label, locator

        return None

    async def _find_submit(self, page: Any) -> Optional[str]:
        """
        The label of the control that would send the application.

        Never pressed. Found so the walk knows it is at the end, and so the
        tray can tell the user which button they are about to authorise.

        A disabled one still counts. Indeed's review page renders its Submit
        disabled for a few seconds while it finishes preparing, and a walk that
        only looked at enabled controls reported "no submit control found" on a
        complete application sitting at 100% — sending the user to look for a
        button that was in front of them.

        Args:
            page: Playwright page

        Returns:
            The label, or None
        """
        for label, _ in await self._clickables(page):
            if NEVER_CLICK.search(label):
                continue

            if any(re.match(pattern, label, re.IGNORECASE) for pattern in SUBMIT_PATTERNS):
                return label.strip()

        try:
            handles = await page.locator(
                "button:disabled, input[type=submit]:disabled, [role=button][disabled]"
            ).all()
        except Exception:
            return None

        for handle in handles:
            try:
                if not await handle.is_visible():
                    continue

                label = " ".join(((await handle.inner_text()) or "").split())
            except Exception:
                continue

            if not label or NEVER_CLICK.search(label):
                continue

            if any(re.match(pattern, label, re.IGNORECASE) for pattern in SUBMIT_PATTERNS):
                return label.strip()

        return None

    async def _clickables(self, page: Any):
        """
        Every visible, enabled button-like control, with its label.

        Args:
            page: Playwright page

        Returns:
            (label, locator) pairs in document order
        """
        found = []

        try:
            handles = await page.locator(
                "button, input[type=submit], input[type=button], "
                "a[role=button], [role=button]"
            ).all()
        except Exception:
            return found

        for handle in handles:
            try:
                if not await handle.is_visible():
                    continue

                if not await handle.is_enabled():
                    continue

                label = (
                    await handle.inner_text()
                    or await handle.get_attribute("value")
                    or await handle.get_attribute("aria-label")
                    or ""
                )
            except Exception:
                continue

            label = " ".join((label or "").split())

            if label:
                found.append((label, handle))

        return found

    async def _press(self, page: Any, locator: Any, label: str) -> bool:
        """
        Click a forward control and wait for the next step to settle.

        Args:
            page: Playwright page
            locator: The control
            label: Its label, for logging

        Returns:
            True if the click landed
        """
        # What the step looks like before the click, so the wait afterwards can
        # tell the next step from the one still on screen.
        before = await self._fingerprint(page)

        try:
            await locator.click(timeout=CLICK_TIMEOUT_MS)
        except Exception as e:
            logger.info(f"Could not click '{label}': {type(e).__name__}: {e}")
            return False

        # A wizard step may be a navigation or a client-side swap. Wait for
        # whichever happens, then let the new fields mount.
        try:
            await page.wait_for_load_state("networkidle", timeout=CLICK_TIMEOUT_MS)
        except Exception:
            pass

        await self._await_step(page, before)

        return True

    async def _await_step(self, page: Any, before: str = "") -> None:
        """
        Wait until the next step has actually rendered.

        Goal-directed, not a fixed pause. SmartApply's demographic step paints
        its heading and progress bar for the better part of a minute before its
        questions exist, and a walk that read it on a timer found a step with
        no fields and no way forward — and reported a filled application as
        having stopped for no reason. Polls for the thing it is waiting for: a
        field to answer, or a control to press.

        Arrival is judged against the *previous* step, because for a beat after
        the click the step just left is still on screen. Without that
        comparison the wait ends immediately on the outgoing step's own
        Continue button, which is the same as not waiting at all.

        Args:
            page: Playwright page
            before: The previous step's fingerprint
        """
        deadline = STEP_RENDER_MAX_MS

        while deadline > 0:
            try:
                ready = await page.evaluate(
                    """() => {
                        const vis = e => {
                            const b = e.getBoundingClientRect();
                            return b.width > 0 && b.height > 0;
                        };
                        const fields = [...document.querySelectorAll(
                            'input:not([type=hidden]):not([type=submit]),select,textarea'
                        )].some(e => vis(e) || (e.labels && [...e.labels].some(vis)));
                        if (fields) return true;
                        return [...document.querySelectorAll('button,[role=button]')]
                            .filter(vis)
                            .some(e => /continue|next|submit|apply|review|finish/i
                                .test((e.innerText || '').trim()));
                    }"""
                )
            except Exception:
                # Mid-navigation the context is destroyed. That is the normal
                # state of a wizard hopping steps, not a reason to give up.
                ready = False

            if ready and before:
                # Ready, but is it the *next* step or the one just left?
                ready = await self._fingerprint(page) != before

            if ready:
                break

            try:
                await page.wait_for_timeout(STEP_RENDER_POLL_MS)
            except Exception:
                pass

            deadline -= STEP_RENDER_POLL_MS

        # Even once something is on screen, the rest of the step lands a beat
        # later; reading on the same tick sees half a form.
        try:
            await page.wait_for_timeout(SETTLE_MS)
        except Exception:
            pass
