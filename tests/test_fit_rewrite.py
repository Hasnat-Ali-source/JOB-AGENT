"""
Rewriting a resume at a posting, and the line that rewrite may not cross.

This is the most aggressive tailoring in the project: it reframes the summary
and rewords every passage into the employer's language. What it may never do
is state something the resume does not support — these documents go to real
employers under the user's name, and a resume claiming a technology they have
never used is a false statement they carry.
"""

import pytest

from job_agent.services.fit_rewrite import (
    FitRewriteResult,
    _find_summary,
    _qualifications_in,
    _summary_is_honest,
)


SUMMARY = (
    "Full-Stack Developer with hands-on experience across the React ecosystem "
    "and Node.js back ends. Currently completing a research-based Master's "
    "degree in Information Technology, building on a Bachelor's foundation in "
    "Computer Science. Comfortable working independently on end-to-end product "
    "builds across 3 production systems."
)

RESUME = f"""HASNAT TAHIR
hasnat@example.com
PROFILE SUMMARY
{SUMMARY}
TECHNICAL SKILLS
Frontend: React, Vite
"""


class TestFindingTheSummary:
    """The section the reframe operates on."""

    def test_the_summary_section_is_located(self):
        found = _find_summary(RESUME)

        assert found is not None
        assert "Full-Stack Developer" in found[2]

    def test_a_profile_summary_heading_is_recognised(self):
        """
        "PROFILE SUMMARY" was not in the parser's heading vocabulary, so the
        section could not be found at all — which silently disabled the one
        rewrite with the most to gain.
        """
        assert _find_summary(RESUME) is not None

    def test_a_resume_with_no_summary_returns_nothing(self):
        assert _find_summary("Jane Doe\nEXPERIENCE\nDid things.") is None


class TestQualifications:
    """What the reframe is told to repeat back word for word."""

    def test_degrees_are_extracted(self):
        found = " ".join(_qualifications_in(SUMMARY)).lower()

        assert "master" in found
        assert "bachelor" in found

    def test_nothing_is_invented_when_there_are_none(self):
        assert _qualifications_in("A developer who builds things.") == []


class TestSummaryHonesty:
    """What a reframed summary may and may not become."""

    def test_a_genuine_reframing_is_accepted(self):
        candidate = (
            "React and Node.js developer who builds production web products "
            "end to end, from database and API design through to the "
            "interface. Currently completing a research-based Master's degree "
            "in Information Technology, building on a Bachelor's foundation in "
            "Computer Science, with 3 production systems delivered."
        )

        assert _summary_is_honest(SUMMARY, candidate, "Northwind") is None

    def test_a_dropped_degree_is_refused(self):
        """
        The failure this catches in practice: compressing to a word budget,
        the model drops the degree first, every time.
        """
        candidate = (
            "React and Node.js developer who builds production web products "
            "end to end, from database and API design through to the "
            "interface, across 3 production systems delivered to date."
        )

        refusal = _summary_is_honest(SUMMARY, candidate, "Northwind")

        assert refusal is not None
        assert "qualification" in refusal

    def test_an_invented_figure_is_refused(self):
        candidate = SUMMARY.replace("3 production systems", "40 production systems")

        refusal = _summary_is_honest(SUMMARY, candidate, "Northwind")

        assert refusal is not None
        assert "invented a figure" in refusal

    def test_claiming_the_hiring_company_is_refused(self):
        candidate = SUMMARY + " Previously delivered projects for Northwind."

        refusal = _summary_is_honest(SUMMARY, candidate, "Northwind")

        assert refusal == "claims a connection to Northwind"

    def test_a_different_candidate_is_refused(self):
        """
        The whole point of the overlap floor: reframing changes how a career
        is introduced, not whose career it is.
        """
        # Keeps the degrees and the figure, so the earlier checks pass and
        # this isolates the overlap floor: the subject matter is what changed.
        candidate = (
            "Customer support leader running distributed escalation teams "
            "across enterprise accounts, building service-level frameworks "
            "and coaching 3 regional squads through them. Holds a Master's "
            "degree and a Bachelor's degree."
        )

        refusal = _summary_is_honest(SUMMARY, candidate, "Northwind")

        assert refusal is not None
        assert "different candidate" in refusal

    def test_a_thin_summary_is_refused(self):
        refusal = _summary_is_honest(SUMMARY, "A developer.", "Northwind")

        assert refusal is not None
        assert "thinner" in refusal


class TestReporting:
    """What the user is told the rewrite was worth."""

    def test_a_gain_is_stated_plainly(self):
        result = FitRewriteResult(content_text="", score_before=54, score_after=91)

        assert "54% → 91%" in result.describe()
        assert result.gain == 37

    def test_an_unmeetable_requirement_is_named_rather_than_hidden(self):
        """
        The honest answer to "make this fit" when the posting wants Ruby and
        the resume has never mentioned it. Naming the gap is what keeps the
        real advice — a closer posting — visible.
        """
        result = FitRewriteResult(
            content_text="",
            score_before=75,
            score_after=75,
            blocking_technologies=["rails", "ruby"],
        )

        message = result.describe()

        assert "rails, ruby" in message
        assert "cannot be written in" in message

    def test_no_gain_is_not_dressed_up(self):
        result = FitRewriteResult(content_text="", score_before=88, score_after=88)

        assert "Nothing could be reframed further" in result.describe()


class TestFabricationCheckSurvivesReframing:
    """
    Reframing recombines the master's own words, and the check must not read
    that as an invented organisation.

    Both of these blocked a genuinely honest document from being released.
    """

    MASTER = (
        "Hasnat Tahir\n"
        "Full-Stack Developer with React and Node.js. Bachelor of Science in "
        "Computer Science. Worked at SimpleX Technology."
    )

    def test_a_sentence_boundary_is_not_an_entity(self):
        """
        "…in Computer Science. Skilled in React…" produced the phantom name
        "Computer Science. Skilled", which appears in no document anywhere.
        """
        from job_agent.services.fabrication_check import verify_no_fabrication

        flags = verify_no_fabrication(
            self.MASTER,
            "Bachelor of Science in Computer Science. Skilled in React.",
        )

        assert flags == []

    def test_the_master_s_own_words_may_be_recombined(self):
        from job_agent.services.fabrication_check import verify_no_fabrication

        flags = verify_no_fabrication(
            self.MASTER, "Experienced Full-Stack Developer skilled in React."
        )

        assert flags == []

    def test_an_invented_employer_is_still_caught(self):
        from job_agent.services.fabrication_check import verify_no_fabrication

        flags = verify_no_fabrication(self.MASTER, "Senior Engineer at Initech Corporation.")

        assert any("Initech Corporation" in flag for flag in flags)

    def test_an_invented_name_reusing_one_real_word_is_still_caught(self):
        """A majority, not merely one word — or a degree could be invented."""
        from job_agent.services.fabrication_check import verify_no_fabrication

        flags = verify_no_fabrication(self.MASTER, "Degree from SimpleX University.")

        assert any("SimpleX University" in flag for flag in flags)

    def test_an_invented_certification_is_still_caught(self):
        from job_agent.services.fabrication_check import verify_no_fabrication

        flags = verify_no_fabrication(self.MASTER, "Certified Kubernetes Administrator.")

        assert any("Kubernetes" in flag for flag in flags)
