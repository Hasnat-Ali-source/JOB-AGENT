"""
Finding the way from a posting to its application form.

**What the agent was doing.** It opened a posting, looked for a form on that
page, and gave up when there wasn't one. On an aggregator there never is: the
posting page is a description with an *Apply* control on it, and the form is
one or more hops away.

**What was observed, on SimplyHired, in the user's own signed-in profile.**
The route is three hops and none of them is a form:

1. The posting page has no applicant fields at all — only the board's search
   box in its header.
2. A control labelled **Quick Apply** (which reads *Apply Now* when signed
   out — the label itself changes with the session) goes to
   `smartapply.indeed.com`.
3. That page spends **eighteen seconds** rendering "Preparing review" before
   it shows *Review your application* and a **Submit your application**
   button, with the application already filled in from the profile.

Three things in that sequence break a naive follower, and all three are why
this module exists rather than a couple of extra selectors:

- **The control must be clicked, not navigated to.** Its href is a tracking
  redirect (`/out?r=…`) that bounces to the homepage when opened directly,
  because the click is what validates it.
- **It may open a new tab.** Following the original page after the click
  leaves the agent reading the posting it started on.
- **It may take twenty seconds to become anything.** A fixed wait finds a
  spinner and concludes the page is empty.

**How a route is chosen.** By what the control says it does, not by where it
is or what it is called in the markup — so a board nobody has written a
connector for works the same way. Anything that would leave the application
or start a different flow (Exit, Cancel, Create an account, Sign in) is never
followed.

**Where it stops.** At a form, or at a page whose forward control submits an
application. It does not submit: that stays the user's act, which is the
promise the whole product rests on.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

# How many hops to follow. SimplyHired -> Indeed SmartApply is two; an
# employer's own board behind an aggregator is three. Past that it is a
# redirect chain, not an application.
MAX_HOPS = 4

# A third-party apply flow can spend twenty seconds saying "Preparing review".
# Polled rather than slept: most pages settle in two.
SETTLE_POLL_MS = 1500
SETTLE_MAX_MS = 30000

# Read nothing until the page has had a moment to paint. Without this the
# first read lands on a shell that has none of the page's own controls yet —
# SimplyHired's "Quick Apply" is client-rendered, so the walk saw a posting
# with no way to apply and gave up before the button existed.
SETTLE_MIN_MS = 4000
POPUP_TIMEOUT_MS = 6000

# What a control that leads towards applying says. Ordered most specific
# first, so "Apply on company site" is preferred over a bare "Apply" when a
# page offers both.
ROUTE_LABELS = (
    r"^\s*(quick|easy|1.click|one.click)\s*apply\s*$",
    r"^\s*apply\s+(on|at|via|through)\s+.{0,30}$",
    r"^\s*apply\s+(now|here|for this (job|role|position))\s*$",
    r"^\s*apply\s*$",
    r"^\s*(start|begin|continue)\s+(your\s+)?application\s*$",
    r"^\s*i'?m interested\s*$",
    r"^\s*submit (a|your) (resume|cv|application)\s*$",
)

# Controls that end the application rather than advance it. Never followed —
# and `submit` is here because reaching a submit is the *goal*, not a hop.
NEVER_FOLLOW = re.compile(
    r"exit|cancel|close|back|previous|report an issue|report a problem"
    r"|sign ?in|log ?in|sign ?up|create (an )?account|register|forgot"
    r"|privacy|terms|cookie|help|contact|share|save (this )?job|bookmark"
    r"|not interested|dismiss|skip",
    re.IGNORECASE,
)

# A posting that is no longer open. Worth its own answer: "no way to apply was
# offered on this page" sends the user hunting for a bug in the agent, when
# the truth is that the job is gone and the wire is showing a stale copy.
# Aggregators keep expired postings addressable for weeks.
EXPIRED = re.compile(
    r"(job|position|posting|vacancy)[^.]{0,40}(has )?expired"
    r"|no longer (be )?(available|accepting|open|active|posted)"
    r"|this (job|posting|position) (has been|was) (removed|closed|filled)"
    r"|applications are closed|we are no longer accepting"
    r"|perform a new search to find current jobs"
    r"|this posting has closed"
    # SimplyHired's own wording for a posting that is gone. It says
    # "temporarily", but the page it serves has no apply control at all, so to
    # a walk it is indistinguishable from expired — and reporting "no way to
    # apply was offered" for it reads as a broken agent rather than a closed
    # job.
    r"|this (job )?(posting|listing|position) is (temporarily |currently )?unavailable",
    re.IGNORECASE,
)

# A control that would send the application. Found so the walk knows it has
# arrived; never pressed here.
SUBMITS = re.compile(
    r"^\s*submit( your| my)? application\s*$"
    r"|^\s*(send|finish|complete)( your)? application\s*$"
    r"|^\s*submit\s*$",
    re.IGNORECASE,
)


@dataclass
class Hop:
    """One step along the route, and what it led to."""

    label: str
    url: str
    opened_new_tab: bool = False

    def to_dict(self) -> dict:
        """Serialize for the register."""
        return {
            "followed": self.label,
            "landed_on": self.url,
            "new_tab": self.opened_new_tab,
        }


@dataclass
class RouteResult:
    """Where the route ended, and how it got there."""

    page: Any = None
    hops: List[Hop] = field(default_factory=list)
    found_form: bool = False
    found_submit: str = ""
    expired: bool = False
    interruption: Any = None
    reason: str = ""

    @property
    def reached_an_application(self) -> bool:
        """True when there is something here to fill in or send."""
        return self.found_form or bool(self.found_submit)

    def describe(self) -> str:
        """One sentence for the tray and the register."""
        if not self.hops:
            return self.reason or "The application form was already on the page."

        route = " → ".join(hop.label for hop in self.hops)

        if self.found_form:
            return f"Followed {route} to the application form."

        if self.found_submit:
            return (
                f"Followed {route} to a completed application. "
                f"'{self.found_submit}' is the control that sends it, and that "
                f"is yours to press."
            )

        return f"Followed {route}, and {self.reason}"

    def to_dict(self) -> dict:
        """Serialize for the register and the tray."""
        return {
            "hops": [hop.to_dict() for hop in self.hops],
            "found_form": self.found_form,
            "found_submit": self.found_submit,
            "reached_an_application": self.reached_an_application,
            "expired": self.expired,
            "interruption": self.interruption.to_dict() if self.interruption else None,
            "reason": self.reason,
            "message": self.describe(),
        }


class ApplyRouteFinder:
    """Walks from a posting to whatever will accept an application."""

    def __init__(self, max_hops: int = MAX_HOPS) -> None:
        self.max_hops = max_hops

    async def find(self, page: Any, context: Any = None) -> RouteResult:
        """
        Follow the page's own apply controls until a form or a submit appears.

        Args:
            page: Playwright page on the posting
            context: The browser context, so a control that opens a new tab
                can be followed into it. Without one, a popup is lost and the
                agent carries on reading the page it started from.

        Returns:
            A RouteResult whose `page` is wherever the walk ended — the caller
            fills and reviews from there.
        """
        result = RouteResult(page=page)
        context = context or getattr(page, "context", None)

        for hop_number in range(1, self.max_hops + 1):
            await self._settle(page)

            # Checked before anything else. An expired posting has no apply
            # control, so every other check would report "no way to apply was
            # offered" — which reads as a broken agent rather than a job that
            # is gone.
            if await self._is_expired(page):
                result.page = page
                result.expired = True
                result.reason = (
                    "this posting has expired — the board still serves the "
                    "page, but the job is no longer open. It has been marked "
                    "closed on the wire."
                )
                break

            if await self._has_applicant_fields(page):
                result.page = page
                result.found_form = True
                result.reason = "found the application form"
                break

            # Once a hop has been taken we are *inside* the apply flow, and
            # its first step need not look like a form: Indeed's SmartApply
            # opens on "profile-location" at 33% with four fields and a
            # Continue button, and none of those fields is named anything a
            # generic "does this ask for an applicant" test recognises.
            # Arriving somewhere with fields and a way forward is arrival —
            # walking the rest of the wizard is the form walker's job.
            if result.hops and await self._is_a_form_step(page):
                result.page = page
                result.found_form = True
                result.reason = "reached the application flow"
                break

            submit = await self._submit_label(page)

            if submit:
                result.page = page
                result.found_submit = submit
                result.reason = (
                    "the application is already complete and waiting to be sent"
                )
                break

            route = await self._next_route(page)

            if not route:
                # Before blaming the page for offering no way to apply, ask
                # whether it was allowed to show one. Cloudflare answers an
                # apply link with "Additional Verification Required" and a Ray
                # ID, which has nothing to do with the posting — and telling
                # the user their agent found no apply button sends them to
                # look for a fault that is not there.
                wall = await self._wall(page)

                if wall:
                    result.interruption = wall
                    result.reason = f"{wall.evidence.strip()} — {wall.guidance}"
                    break

                result.reason = (
                    "no way to apply was offered on this page. The board may "
                    "keep its apply flow behind a sign-in — connect this "
                    "station — or the posting may have expired."
                )
                break

            label, control = route
            logger.info(f"Apply route hop {hop_number}: following '{label}'")

            landed, new_tab = await self._follow(page, control, context)

            if landed is None:
                result.reason = f"could not follow '{label}'"
                break

            page = landed
            result.page = page
            result.hops.append(
                Hop(label=label, url=getattr(page, "url", ""), opened_new_tab=new_tab)
            )
        else:
            result.reason = (
                f"followed {self.max_hops} links without reaching a form — "
                f"this looks like a redirect chain rather than an application"
            )

        logger.info(f"Apply route: {result.describe()}")

        return result

    async def _settle(self, page: Any) -> None:
        """
        Wait until the page shows something worth acting on.

        Deliberately goal-directed rather than "wait until it stops changing".
        Indeed's SmartApply is stable for several seconds on a "Preparing
        review" screen that carries only Exit and Report an issue — waiting
        for stability returned *there*, at about twelve seconds, and the
        Submit button it was after appears at eighteen. Stability is not
        arrival.

        So it polls for the things that end a walk — applicant fields, a
        submit control, or another apply route — and only falls back to the
        budget when none of them ever appears.

        Args:
            page: Playwright page
        """
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=SETTLE_MAX_MS)
        except Exception:
            pass

        try:
            await page.wait_for_timeout(SETTLE_MIN_MS)
        except Exception:
            return

        waited = SETTLE_MIN_MS

        while waited < SETTLE_MAX_MS:
            try:
                if await page.evaluate(_SOMETHING_TO_ACT_ON_JS):
                    return
            except Exception:
                # Almost always "execution context was destroyed": the page is
                # mid-navigation. That is the *normal* state of an apply flow
                # bouncing through its redirects, and treating it as a reason
                # to stop waiting was why every hop landed on nothing —
                # SmartApply redirects twice before it renders.
                pass

            try:
                await page.wait_for_timeout(SETTLE_POLL_MS)
            except Exception:
                return

            waited += SETTLE_POLL_MS

    async def _is_expired(self, page: Any) -> bool:
        """
        Whether the board is saying this posting is closed.

        Args:
            page: Playwright page

        Returns:
            True when the page says the job is gone
        """
        try:
            text = await page.inner_text("body", timeout=POPUP_TIMEOUT_MS)
        except Exception:
            return False

        return bool(EXPIRED.search(text or ""))

    async def _wall(self, page: Any):
        """
        Whether this page is a verification wall rather than a dead end.

        Wording only — see `InterruptionDetector.from_text`. An ATS embeds an
        invisible reCAPTCHA on every application form, so its markup is not
        evidence of a challenge.

        Args:
            page: Playwright page

        Returns:
            An Interruption, or None
        """
        from job_agent.services.interruption_detector import InterruptionDetector

        try:
            text = await page.inner_text("body", timeout=POPUP_TIMEOUT_MS)
        except Exception:
            return None

        return InterruptionDetector.from_text(text or "", getattr(page, "url", ""))

    async def _has_applicant_fields(self, page: Any) -> bool:
        """
        Whether this page asks for an applicant.

        Args:
            page: Playwright page

        Returns:
            True when at least two fields ask for something only an applicant
            supplies. Two, because a board's header search box alone must not
            read as an application form — that mistake queued an application
            whose one filled field was "City, State, ZIP or Remote".
        """
        try:
            return bool(
                await page.evaluate(
                    """() => {
                        const APPLICANT = /first.?name|last.?name|full.?name|your name|e-?mail|phone|resume|cv\\b|cover.?letter|linkedin|portfolio|address/i;
                        const SEARCHY = /search|keyword|city, state|zip|job title, skills/i;
                        const vis = e => {
                            const b = e.getBoundingClientRect();
                            return b.width > 0 && b.height > 0;
                        };
                        const describe = e => [
                            e.name, e.id, e.placeholder, e.getAttribute('aria-label'),
                            (e.labels && e.labels[0] && e.labels[0].textContent) || ''
                        ].filter(Boolean).join(' ');
                        return [...document.querySelectorAll(
                            'input:not([type=hidden]):not([type=submit]),select,textarea'
                        )].filter(vis).filter(e => {
                            const t = describe(e);
                            return APPLICANT.test(t) && !SEARCHY.test(t);
                        }).length >= 2;
                    }"""
                )
            )
        except Exception:
            return False

    async def _is_a_form_step(self, page: Any) -> bool:
        """
        Whether this page is a step of an application, rather than a landing.

        A control counts when *the user can see it*, which is not the same as
        the input element having a size. SmartApply's first step for most
        postings is "Add a resume": one radio group, styled the way modern
        forms style radios — the `input` itself is collapsed to nothing and
        the thing on screen is its label. Requiring the input to have a
        bounding box found no fields there, so the step read as a dead end and
        the walk reported "no way to apply was offered" while standing on the
        application. That single miss accounted for most of the postings the
        route was thought to be failing to reach.

        Args:
            page: Playwright page

        Returns:
            True when it has fields to fill and a control that carries on
        """
        try:
            return bool(
                await page.evaluate(
                    """() => {
                        const vis = e => {
                            if (!e) return false;
                            const b = e.getBoundingClientRect();
                            return b.width > 0 && b.height > 0;
                        };
                        // A visually-hidden input whose label is on screen is
                        // a field the user can see and click.
                        const shown = e => vis(e)
                            || (e.labels && [...e.labels].some(vis))
                            || vis(e.closest('label'))
                            || (e.id && vis(document.querySelector(
                                `label[for="${CSS.escape(e.id)}"]`)));
                        const fields = [...document.querySelectorAll(
                            'input:not([type=hidden]):not([type=submit]),select,textarea'
                        )].filter(shown);

                        if (!fields.length) return false;

                        const FORWARD = /^(continue|next|save and continue|submit|apply|review)/i;
                        return [...document.querySelectorAll(
                            'button,[role=button],input[type=submit]'
                        )].filter(vis)
                          .map(e => (e.innerText || e.value || '').trim())
                          .some(t => FORWARD.test(t));
                    }"""
                )
            )
        except Exception:
            return False

    async def _submit_label(self, page: Any) -> str:
        """
        The label of a control that would send an application, if present.

        Args:
            page: Playwright page

        Returns:
            The label, or empty
        """
        for label, _ in await self._controls(page):
            if NEVER_FOLLOW.search(label):
                continue

            if SUBMITS.match(label):
                return label

        return ""

    async def _next_route(self, page: Any):
        """
        The most promising control leading towards an application.

        Args:
            page: Playwright page

        Returns:
            (label, locator) or None
        """
        controls = await self._controls(page)

        for pattern in ROUTE_LABELS:
            for label, control in controls:
                if NEVER_FOLLOW.search(label) or SUBMITS.match(label):
                    continue

                if re.match(pattern, label, re.IGNORECASE):
                    return label, control

        return None

    async def _controls(self, page: Any):
        """
        Every visible, enabled clickable on the page, with its label.

        Args:
            page: Playwright page

        Returns:
            (label, locator) pairs
        """
        found = []

        try:
            handles = await page.locator(
                "a, button, [role=button], input[type=submit], input[type=button]"
            ).all()
        except Exception:
            return found

        for handle in handles:
            try:
                if not await handle.is_visible():
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

    async def _follow(self, page: Any, control: Any, context: Any):
        """
        Click a route control, following it into a new tab if it opens one.

        Clicking rather than navigating is not incidental. These controls are
        tracking redirects, and opening the href directly bounces to the
        board's homepage — the click is what validates it.

        Args:
            page: The page the control is on
            control: The control
            context: Browser context, for catching a popup

        Returns:
            (landed page, whether a new tab opened), or (None, False)
        """
        if context is not None:
            try:
                async with context.expect_page(timeout=POPUP_TIMEOUT_MS) as popup:
                    await control.click()

                return await popup.value, True
            except Exception:
                # No popup: either the click navigated in place, or it failed.
                # Distinguished below by whether the page still responds.
                pass
        else:
            try:
                await control.click()
            except Exception as e:
                logger.info(f"Could not click the apply control: {e}")
                return None, False

        try:
            await page.wait_for_load_state("domcontentloaded", timeout=SETTLE_MAX_MS)
        except Exception:
            pass

        return page, False


# Whether the page yet shows anything a walk could act on: a field asking for
# an applicant, a control that submits, or another way forward. Used to wait
# for a slow apply flow without waiting a fixed twenty seconds on every page.
_SOMETHING_TO_ACT_ON_JS = """
() => {
  const vis = e => { const b = e.getBoundingClientRect(); return b.width > 0 && b.height > 0; };
  const APPLICANT = /first.?name|last.?name|full.?name|your name|e-?mail|phone|resume|cv\\b|cover.?letter|linkedin|portfolio|address/i;
  const SEARCHY = /search|keyword|city, state|zip|job title, skills/i;
  const FORWARD = /apply|submit|continue|next|start .{0,12}application/i;
  const SKIP = /exit|cancel|report an issue|privacy|terms|sign ?in|log ?in|create (an )?account|share|save/i;

  const describe = e => [
    e.name, e.id, e.placeholder, e.getAttribute('aria-label'),
    (e.labels && e.labels[0] && e.labels[0].textContent) || ''
  ].filter(Boolean).join(' ');

  const applicant = [...document.querySelectorAll(
    'input:not([type=hidden]):not([type=submit]),select,textarea'
  )].filter(vis).filter(e => {
    const t = describe(e);
    return APPLICANT.test(t) && !SEARCHY.test(t);
  });

  if (applicant.length >= 2) return true;

  return [...document.querySelectorAll('a,button,[role=button],input[type=submit]')]
    .filter(vis)
    .map(e => (e.innerText || e.value || '').trim())
    .filter(Boolean)
    .some(t => FORWARD.test(t) && !SKIP.test(t));
}
"""
