#!/usr/bin/env python3
"""
Tests for verifying a reviewed value after it is written back to a form.

Submission reopens the form and replays what the user approved. Every value is
then read back off the page, because writing without checking is how "restored
27 of 27" was once reported onto a form that was still empty.

Reading back has its own failure mode, and it is the one that stopped a real
submission: the check compared a 40-character slice of the answer against what
the control displayed. That works for every short label and fails for EEO
options, which are a category followed by its legal definition — the form shows
the category, the slice lands mid-definition, and a selection that took was
reported as lost.
"""

import pytest

from job_agent.dashboard.routes.review import _control_shows, _label_head

# The option as the form lists it, non-breaking space and all.
RACE_OPTION = (
    "Asian (Not Hispanic or Latino):\xa0A person having origins in any of the "
    "original peoples of the Far East, Southeast Asia, or the Indian "
    "Subcontinent including, for example, Cambodia, China, India, Japan, "
    "Korea, Malaysia, Pakistan, the Philippine Islands, Thailand, and Vietnam."
)


class FakePage:
    """A page that displays one fixed value, whatever is asked of it."""

    def __init__(self, shown: str):
        self.shown = shown

    async def evaluate(self, _script, *_args):
        return self.shown


class TestLabelHead:
    """What a control still shows once a long option is chosen."""

    def test_a_definition_is_trimmed_off(self):
        assert _label_head(RACE_OPTION.lower().replace("\xa0", " ")).startswith(
            "asian (not hispanic or latino)"
        )

    def test_a_short_label_is_left_alone(self):
        assert _label_head("male") == "male"

    def test_an_ambiguous_head_is_not_used(self):
        # "no" would match the wrong option's text in the same shell, so the
        # original slice is kept instead.
        assert _label_head("no: I have never been employed here") != "no"


@pytest.mark.asyncio
class TestControlShows:
    """Whether a control is displaying the answer that was written to it."""

    async def test_a_long_eeo_label_counts_as_shown(self):
        # What Greenhouse displays after the option is picked.
        page = FakePage("What is your Race/Ethnicity? Asian (Not Hispanic or Latino)")

        assert await _control_shows(page, "#race", RACE_OPTION)

    async def test_the_full_label_still_counts(self):
        page = FakePage(RACE_OPTION)

        assert await _control_shows(page, "#race", RACE_OPTION)

    async def test_a_different_answer_does_not_count(self):
        page = FakePage("What is your Race/Ethnicity? White (Not Hispanic or Latino)")

        assert not await _control_shows(page, "#race", RACE_OPTION)

    async def test_an_empty_control_does_not_count(self):
        assert not await _control_shows(FakePage(""), "#race", RACE_OPTION)

    async def test_a_reformatted_phone_number_counts(self):
        assert await _control_shows(FakePage("010-421 5890"), "#phone", "0104215890")

    async def test_a_short_choice_still_matches_inside_its_shell(self):
        page = FakePage("What gender do you identify with? Male")

        assert await _control_shows(page, "#gender", "Male")
