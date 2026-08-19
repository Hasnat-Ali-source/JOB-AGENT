"""
A posting's description must reach the agent as text, not as markup.

JSON-LD carries the description as an HTML fragment and it was stored
verbatim: "<div><p>Career paths start between $14.50…". That reached three
places it had no business being — the fit analyser, which scored `<b>` and
`<br>` as content words; the tailoring prompt, where it spent a small model's
context on markup; and the posting the user reads in the tray.
"""

import pytest

from job_agent.connectors.base import JobPosting
from job_agent.connectors.generic_ats import _as_plain_text


HTML = (
    "<div><p>Career paths start between $14.50 and $15/hr plus bonus.<br> </p>"
    "<p><b>Why start building your career here?</b></p>"
    "<ul><li>Paid training</li><li>Health insurance</li></ul></div>"
)


class TestPlainText:
    """What the description becomes."""

    def test_tags_are_removed(self):
        text = _as_plain_text(HTML)

        assert "<" not in text
        assert "div" not in text.lower().split()

    def test_the_words_survive(self):
        text = _as_plain_text(HTML)

        assert "Career paths start between $14.50" in text
        assert "Health insurance" in text

    def test_list_items_stay_a_list(self):
        """The requirement extractor finds requirements by their bullets."""
        text = _as_plain_text(HTML)

        assert "• Paid training" in text
        assert "• Health insurance" in text

    def test_blocks_do_not_run_together(self):
        text = _as_plain_text(HTML)

        assert "bonus.Why" not in text
        assert "training• Health" not in text

    def test_entities_are_decoded(self):
        assert _as_plain_text("<p>R&amp;D and 5 &lt; 10</p>") == "R&D and 5 < 10"

    def test_plain_text_passes_through_untouched(self):
        assert _as_plain_text("Just a description.") == "Just a description."

    def test_nothing_is_not_a_crash(self):
        assert _as_plain_text(None) == ""
        assert _as_plain_text("") == ""

    def test_whitespace_is_collapsed_but_paragraphs_kept(self):
        """
        A run of blank lines collapses to one break, and a paragraph boundary
        survives as one — a posting's structure is what the requirement
        extractor reads.
        """
        text = _as_plain_text("<p>One</p>\n\n\n<p>Two</p>")

        assert text == "One\n\nTwo"


class TestJobPostingDefaults:
    """
    A posting with no stated location must not raise.

    Requiring one meant every fallback path that omitted it raised TypeError —
    including the exception handler, so a failure to read one SimplyHired
    posting surfaced as "missing 1 required positional argument: 'location'"
    and the real cause was never logged.
    """

    def test_location_is_optional(self):
        posting = JobPosting(
            platform="simply_hired",
            external_id="abc",
            title="Customer Service Representative",
            company="Afni, Inc.",
        )

        assert posting.location == "Not specified"

    def test_a_stated_location_is_kept(self):
        posting = JobPosting(
            platform="simply_hired",
            external_id="abc",
            title="CSR",
            company="Afni",
            location="Austin, TX",
        )

        assert posting.location == "Austin, TX"
