"""
Matching a requirement to evidence by meaning.

The failure being fixed: fit was decided by counting shared words, so
"Developed responsive, component-based web interfaces using React" scored
*zero* against "Proficiency with modern frontend frameworks" — they share no
content word — and a resume that plainly answered a posting was reported as
evidencing none of it.

The live calibration test at the bottom is the one that matters most. It holds
the hand-labelled pairs the thresholds were derived from, so a change of
embedding model that quietly turns the fit score into flattery fails here
rather than in an application sent to an employer.
"""

import pytest

from job_agent.services.fit_report import (
    FitAnalyser,
    _is_section_heading,
    _named_technologies,
)
from job_agent.services.semantic_match import (
    MATCHED_AT,
    MATCHED_MARGIN,
    PARTIAL_AT,
    PARTIAL_MARGIN,
    SemanticMatcher,
    cosine,
    get_semantic_matcher,
    verdict_for,
)


RESUME = """HASNAT TAHIR
PROFILE SUMMARY
Full-Stack Developer with hands-on experience across the React ecosystem and
Node.js back ends.
TECHNICAL SKILLS
Frontend: React, Vite, Tailwind CSS
PROFESSIONAL EXPERIENCE
React Developer | SimpleX Technology Sep 2024 - Feb 2026
• Developed responsive, component-based web interfaces using React and JavaScript.
"""


class TestCosine:
    """The similarity measure itself."""

    def test_identical_vectors_are_one(self):
        assert cosine([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors_are_zero(self):
        assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_a_zero_vector_is_not_a_crash(self):
        assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0

    def test_mismatched_lengths_are_not_a_crash(self):
        assert cosine([1.0], [1.0, 2.0]) == 0.0


class TestVerdict:
    """Both an absolute score and a margin over the resume's baseline."""

    def test_a_strong_distinctive_match_is_matched(self):
        assert verdict_for(MATCHED_AT + 0.05, MATCHED_MARGIN + 0.05) == "matched"

    def test_a_high_score_with_no_margin_is_not_matched(self):
        """
        The failure absolute thresholds alone cannot catch: an embedding model
        scores any two pieces of professional English around 0.5, so a whole
        resume of unrelated work clears an absolute floor together.
        """
        assert verdict_for(MATCHED_AT + 0.05, 0.01) == "missing"

    def test_a_moderate_distinctive_match_is_partial(self):
        assert verdict_for(PARTIAL_AT + 0.01, PARTIAL_MARGIN + 0.01) == "partial"

    def test_a_low_score_is_missing(self):
        assert verdict_for(0.2, 0.5) == "missing"


class TestNamedTechnologies:
    """An embedding cannot tell you whether someone has used Ruby."""

    def test_a_named_language_is_found(self):
        found = _named_technologies("Deep experience with Ruby and Rails.")

        assert "ruby" in found
        assert "rails" in found

    def test_ordinary_words_are_not_technologies(self):
        found = _named_technologies(
            "Strong experience working with distributed teams."
        )

        assert found == set()

    def test_a_sentence_initial_capital_is_not_a_technology(self):
        """Otherwise every requirement names its own first word."""
        assert "experience" not in _named_technologies(
            "Experience building web products."
        )

    def test_an_acronym_is_found(self):
        assert "saas" in _named_technologies("Knowledge of enterprise SaaS.")

    def test_reading_a_resume_takes_capitals_anywhere(self):
        """A resume's skills line starts with the technology's own name."""
        found = _named_technologies(RESUME, anywhere=True)

        assert "react" in found
        assert "vite" in found


class TestEvidenceExtraction:
    """What the resume is read as, before anything is compared."""

    def test_a_wrapped_sentence_is_rejoined(self):
        """
        Read line by line, a PDF-exported resume yields fragments. Half a
        thought is close to everything, so fragments matched requirements they
        had nothing to do with.
        """
        sentences = FitAnalyser._sentences(RESUME)

        assert any(
            "React ecosystem and Node.js back ends" in sentence
            for sentence in sentences
        )

    def test_a_heading_never_joins_the_line_below_it(self):
        """
        "PROJECTS BotCraft Ai — In development Full-stack AI solutions
        platform…" is a blob that scores as moderately similar to everything.
        """
        sentences = FitAnalyser._sentences(RESUME)

        assert not any(sentence.startswith("PROFESSIONAL EXPERIENCE") for sentence in sentences)
        assert not any("TECHNICAL SKILLS Frontend" in sentence for sentence in sentences)

    @pytest.mark.parametrize(
        "line,expected",
        [
            ("PROFESSIONAL EXPERIENCE", True),
            ("EDUCATION", True),
            ("Frontend: React, Vite, Tailwind CSS", False),
            ("• Developed responsive interfaces.", False),
            ("React Developer | SimpleX Technology", False),
        ],
    )
    def test_headings_are_recognised(self, line, expected):
        assert _is_section_heading(line) is expected


class TestDegradation:
    """No model must never mean no report."""

    pytestmark = pytest.mark.asyncio

    async def test_no_model_returns_none_rather_than_raising(self, monkeypatch):
        matcher = SemanticMatcher(host="http://127.0.0.1:1")

        assert await matcher.best_matches(["anything"], ["anything else"]) is None

    async def test_the_fit_report_falls_back_to_words(self, monkeypatch):
        """A fit report is always produced, model or no model."""
        async def no_matches(requirements, evidence):
            return None

        monkeypatch.setattr(
            get_semantic_matcher(), "best_matches", no_matches
        )

        report = await FitAnalyser.run_async(
            "What you'll bring\nStrong experience with React and JavaScript.\n",
            RESUME,
        )

        assert report.matches


class TestScoring:
    """How the verdicts add up."""

    def test_an_unmet_named_technology_counts_double(self):
        """
        A resume with no Ruby scored 86% against a Ruby role, because the one
        requirement it could not meet was one line among seven. That is not
        the order a screen applies them in.
        """
        from job_agent.services.fit_report import FitReport, RequirementMatch

        without = FitReport(
            matches=[
                RequirementMatch(requirement="a", verdict="matched"),
                RequirementMatch(requirement="b", verdict="missing"),
            ]
        )
        with_blocker = FitReport(
            matches=[
                RequirementMatch(requirement="a", verdict="matched"),
                RequirementMatch(
                    requirement="b",
                    verdict="missing",
                    missing_technologies=["ruby"],
                ),
            ]
        )

        assert without.score == 50
        assert with_blocker.score < without.score

    def test_blocking_technologies_are_named_for_the_dashboard(self):
        from job_agent.services.fit_report import FitReport, RequirementMatch

        report = FitReport(
            matches=[
                RequirementMatch(
                    requirement="Deep experience with Ruby",
                    verdict="missing",
                    missing_technologies=["ruby", "rails"],
                )
            ]
        )

        assert report.to_dict()["blocking_technologies"] == ["rails", "ruby"]


# ==========================================================================
# Calibration, against a real embedding model
# ==========================================================================

# (should_match, requirement, evidence) — hand-labelled from this project's
# own resume and the postings it was actually run against.
CALIBRATION = [
    (True, "Proficiency with modern frontend frameworks",
     "Developed responsive, component-based web interfaces using React, HTML5, CSS, and JavaScript."),
    (True, "Experience building REST APIs and backend services",
     "Full-stack AI solutions platform built with React/Vite and Express/MongoDB."),
    (True, "Comfortable working autonomously in a remote team",
     "Comfortable working independently on end-to-end product builds."),
    (True, "Experience with real-time communication technologies",
     "Real-Time Communication: WebSockets"),
    (True, "Familiarity with large language models in production",
     "Features an AI voice assistant powered by local Ollama models."),
    (False, "Experience managing technical support engineering teams",
     "Developed responsive, component-based web interfaces using React, HTML5, CSS, and JavaScript."),
    (False, "Deep experience with Ruby and Rails in a large monolith",
     "Built desktop applications using Windows Forms and WPF."),
    (False, "12+ years of progressive data leadership experience",
     "Mentored final-year students through the planning of their capstone projects."),
    (False, "Experience establishing a global delivery hub",
     "Full-featured WordPress website built for a corporate training company."),
    (False, "Expertise in Kubernetes and cloud infrastructure",
     "Managed computer labs and coursework delivery for Semesters 1 through 4."),
    (False, "A record of scaling a support organization",
     "Implemented responsive, cross-browser layouts with client testimonials and FAQs."),
]


@pytest.mark.asyncio
async def test_the_thresholds_still_separate_real_matches_from_noise():
    """
    The measurement the thresholds were set from, kept runnable.

    Skipped where no embedding model is installed — the product works without
    one, and CI should not require Ollama. Where one *is* present, a model or
    threshold change that stops separating genuine evidence from unrelated
    work fails here, rather than showing up as a flattering fit score on an
    application to a real employer.
    """
    matcher = get_semantic_matcher()

    if not await matcher.available():
        pytest.skip("no local embedding model installed")

    vectors = await matcher.embed(
        [pair[1] for pair in CALIBRATION] + [pair[2] for pair in CALIBRATION]
    )

    assert vectors, "an available model must return vectors"

    half = len(CALIBRATION)
    genuine, unrelated = [], []

    for index, (should_match, _, _) in enumerate(CALIBRATION):
        score = cosine(vectors[index], vectors[half + index])
        (genuine if should_match else unrelated).append(score)

    assert min(genuine) > max(unrelated), (
        f"genuine matches ({min(genuine):.3f}) no longer separate from "
        f"unrelated ones ({max(unrelated):.3f})"
    )
    assert min(genuine) >= PARTIAL_AT, (
        f"a genuine match scores {min(genuine):.3f}, below the {PARTIAL_AT} "
        f"floor — real evidence would be reported as missing"
    )
    assert max(unrelated) < PARTIAL_AT, (
        f"unrelated work scores {max(unrelated):.3f}, at or above the "
        f"{PARTIAL_AT} floor — the fit score would become flattery"
    )


class TestSoftRequirementsAreNotTechnologies:
    """
    A posting writes "Strong Judgment" and "Ownership" with capitals.

    Read as named technologies the candidate lacked, these became
    double-weighted blockers — so a soft requirement anyone could meet pulled
    the score down as hard as a missing database.
    """

    @pytest.mark.parametrize(
        "requirement",
        [
            "Strong Judgment: you decide well with incomplete information.",
            "Ownership: you spot what needs doing and do it.",
            "Excellent Written and Verbal communication.",
            "Attention to Detail across everything you ship.",
        ],
    )
    def test_a_quality_is_not_a_blocking_technology(self, requirement):
        assert _named_technologies(requirement) == set()

    @pytest.mark.parametrize(
        "requirement,expected",
        [
            ("Experience with Kubernetes at scale.", "kubernetes"),
            ("Deep knowledge of ClickHouse internals.", "clickhouse"),
            ("Comfortable in Ruby.", "ruby"),
        ],
    )
    def test_a_real_technology_still_is(self, requirement, expected):
        assert expected in _named_technologies(requirement)
