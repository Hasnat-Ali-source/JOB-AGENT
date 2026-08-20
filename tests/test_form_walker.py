"""
Walking a multi-step application form.

The failure being fixed: the filler read the screen in front of it and stopped.
That is a whole application on a Greenhouse board and about a fifth of one on
Workday, iCIMS or an in-house portal — so an application was reported as
filled when four screens of it had never been seen, and the user found out at
the point of release.

The browser is faked here. Playwright's own behaviour is not what these tests
are about; what matters is that the walk advances, stops in the right places,
and never presses submit.
"""

import pytest

from job_agent.services.form_walker import FormWalker, WalkOutcome


pytestmark = pytest.mark.asyncio


class FakeChallenge:
    """A CAPTCHA widget big enough for a person to interact with."""

    def __init__(self, width=400, height=580, visible=True):
        self.width = width
        self.height = height
        self.visible = visible

    async def is_visible(self):
        return self.visible

    async def bounding_box(self):
        return {"x": 0, "y": 0, "width": self.width, "height": self.height}


class FakeControl:
    """One button on a fake page."""

    def __init__(self, label, page=None, enabled=True, visible=True):
        self.label = label
        self.page = page
        self.enabled = enabled
        self.visible = visible
        self.clicks = 0

    async def is_visible(self):
        return self.visible

    async def is_enabled(self):
        return self.enabled

    async def inner_text(self):
        return self.label

    async def get_attribute(self, name):
        return None

    async def click(self, timeout=None):
        self.clicks += 1

        if self.page:
            self.page.advance()


class FakeLocator:
    def __init__(self, items):
        self._items = items

    async def all(self):
        return self._items

    async def count(self):
        return len(self._items)


class FakePage:
    """
    A scripted wizard.

    Each step is (field names, button labels). Clicking a forward control moves
    to the next step, unless `stuck` is set — which is how a real form behaves
    when it is showing a validation message the agent cannot read.
    """

    def __init__(self, steps, body_text="", stuck=False, captcha_on=None):
        self.steps = steps
        self.index = 0
        self.url = "https://acme.test/apply"
        self.body_text = body_text
        self.stuck = stuck
        self.captcha_on = captcha_on

    def advance(self):
        if not self.stuck and self.index < len(self.steps) - 1:
            self.index += 1

    @property
    def _step(self):
        return self.steps[self.index]

    async def evaluate(self, script):
        return "|".join(sorted(self._step[0]))

    def locator(self, selector):
        if "captcha" in selector or "challenge" in selector:
            hit = self.captcha_on is not None and self.index == self.captcha_on
            return FakeLocator([FakeChallenge()] if hit else [])

        return FakeLocator(
            [FakeControl(label, page=self) for label in self._step[1]]
        )

    async def inner_text(self, selector, timeout=None):
        return self.body_text

    async def wait_for_load_state(self, state, timeout=None):
        return None

    async def wait_for_timeout(self, ms):
        return None

    async def screenshot(self, **kwargs):
        return b""


class FakeFill:
    """What a step's fill returned."""

    def __init__(self, filled=None, deferred=None, required=None):
        self.filled_fields = filled or {}
        self.deferred_fields = deferred or {}
        self.required_deferred = required or []


def filler(required_on=None):
    """A fill_step callable that answers everything, or defers on one step."""
    calls = {"n": 0}

    async def fill(page):
        calls["n"] += 1
        step = page.index

        if required_on is not None and step == required_on:
            return FakeFill(
                deferred={"Why us?": {"required": True}},
                required=["Why us?"],
            )

        return FakeFill(filled={f"Field {step}": {"value": "x"}})

    fill.calls = calls

    return fill


class TestWalking:
    """The happy path: read a step, fill it, press Next, repeat."""

    async def test_a_single_page_form_is_one_step(self):
        page = FakePage([(["name", "email"], ["Submit application"])])

        outcome = await FormWalker().walk(page, filler())

        assert outcome.step_count == 1
        assert outcome.final_control == "Submit application"

    async def test_a_wizard_is_walked_to_the_end(self):
        page = FakePage([
            (["name"], ["Next"]),
            (["history"], ["Continue"]),
            (["eligibility"], ["Submit application"]),
        ])

        outcome = await FormWalker().walk(page, filler())

        assert outcome.step_count == 3
        assert [step.advanced_by for step in outcome.steps] == ["Next", "Continue", ""]

    async def test_every_step_is_filled(self):
        page = FakePage([
            (["a"], ["Next"]),
            (["b"], ["Next"]),
            (["c"], ["Submit"]),
        ])
        fill = filler()

        await FormWalker().walk(page, fill)

        assert fill.calls["n"] == 3

    async def test_the_submit_control_is_found_but_never_pressed(self):
        """The one thing this project does not do on its own."""
        submit = FakeControl("Submit application")
        page = FakePage([(["a"], ["Submit application"])])
        page.locator = lambda selector: FakeLocator(
            [] if "captcha" in selector or "challenge" in selector else [submit]
        )

        outcome = await FormWalker().walk(page, filler())

        assert submit.clicks == 0
        assert outcome.final_control == "Submit application"
        assert "yours to press" in outcome.stopped_because


class TestStopping:
    """Every stop is deliberate."""

    async def test_it_stops_when_a_required_question_needs_the_user(self):
        page = FakePage([
            (["a"], ["Next"]),
            (["b"], ["Next"]),
            (["c"], ["Submit"]),
        ])

        outcome = await FormWalker().walk(page, filler(required_on=1))

        assert outcome.step_count == 2
        assert outcome.needs_user
        assert "Why us?" in outcome.stopped_because

    async def test_it_stops_when_the_form_does_not_advance(self):
        """
        A Next that does nothing means a validation message the agent cannot
        see. Retrying is how a filler submits the same broken step forty times.
        """
        page = FakePage([(["a"], ["Next"]), (["b"], ["Submit"])], stuck=True)

        outcome = await FormWalker().walk(page, filler())

        assert outcome.needs_user
        assert "did not move on" in outcome.stopped_because

    async def test_a_captcha_hands_over_rather_than_being_solved(self):
        page = FakePage([(["a"], ["Next"]), (["b"], ["Submit"])], captcha_on=1)

        outcome = await FormWalker().walk(page, filler())

        assert outcome.needs_user
        assert "CAPTCHA" in outcome.stopped_because

    async def test_an_invisible_recaptcha_badge_is_not_a_challenge(self):
        """
        Greenhouse embeds an invisible reCAPTCHA on every application form.
        Its badge is a visible 256x60 iframe in the corner, and reading that
        as a challenge stopped the walk on step one of a form that had none.
        """
        page = FakePage([(["a"], ["Submit application"])], captcha_on=0)
        page.locator = lambda selector: FakeLocator(
            [FakeChallenge(width=256, height=60)]
            if ("captcha" in selector or "challenge" in selector)
            else [FakeControl("Submit application")]
        )

        outcome = await FormWalker().walk(page, filler())

        assert not outcome.needs_user
        assert outcome.final_control == "Submit application"

    async def test_a_sign_in_wall_hands_over(self):
        page = FakePage(
            [(["a"], ["Next"])],
            body_text="Please sign in to continue with your application.",
        )

        outcome = await FormWalker().walk(page, filler())

        assert outcome.needs_user
        assert "signed in" in outcome.stopped_because

    async def test_a_runaway_wizard_is_capped(self):
        page = FakePage([([f"f{i}"], ["Next"]) for i in range(30)])

        outcome = await FormWalker(max_steps=5).walk(page, filler())

        assert outcome.step_count == 5
        assert outcome.needs_user
        assert "longer than expected" in outcome.stopped_because


class TestChoosingAControl:
    """Which button counts as forward."""

    @pytest.mark.parametrize(
        "label", ["Next", "Continue", "Save and continue", "Next step", "Proceed"]
    )
    async def test_forward_labels_are_followed(self, label):
        page = FakePage([(["a"], [label]), (["b"], ["Submit"])])

        outcome = await FormWalker().walk(page, filler())

        assert outcome.step_count == 2

    @pytest.mark.parametrize(
        "label",
        ["Back", "Previous", "Cancel", "Sign out", "Create an account", "Upload"],
    )
    async def test_controls_that_lose_work_are_never_pressed(self, label):
        """
        Back and Cancel throw away what has been typed; Create an account is a
        different flow entirely.
        """
        page = FakePage([(["a"], [label]), (["b"], ["Submit"])])

        outcome = await FormWalker().walk(page, filler())

        assert outcome.step_count == 1

    async def test_a_disabled_next_is_not_pressed(self):
        control = FakeControl("Next", enabled=False)
        page = FakePage([(["a"], ["Next"])])
        page.locator = lambda selector: FakeLocator(
            [] if "captcha" in selector or "challenge" in selector else [control]
        )

        outcome = await FormWalker().walk(page, filler())

        assert control.clicks == 0
        assert outcome.step_count == 1


@pytest.mark.filterwarnings("ignore")
class TestReporting:
    """What the tray is told. Synchronous — no browser involved."""

    pytestmark = []

    def test_a_single_page_form_says_so(self):
        outcome = WalkOutcome(stopped_because="reached the last step.")
        outcome.steps = [object()]

        assert outcome.describe().startswith("Single-page form")

    def test_a_wizard_reports_its_length(self):
        outcome = WalkOutcome(stopped_because="reached the last step.")
        outcome.steps = [object()] * 4

        assert "Walked 4 steps" in outcome.describe()
