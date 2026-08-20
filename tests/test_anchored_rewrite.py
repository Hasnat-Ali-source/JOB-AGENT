"""
Anchored rewriting: the resume must come back reworded, never rewritten.

Every test here corresponds to something a local model actually did to a real
master resume. The failures are recorded in the module docstring of
`anchored_rewrite`; these hold the fixes in place.
"""

import pytest

from job_agent.services.anchored_rewrite import (
    AnchoredRewriter,
    accept_rewrite,
    content_words,
    plan_blocks,
    posting_vocabulary,
)


MASTER = """HASNAT TAHIR
Petaling Jaya, Selangor, Malaysia | +60 10-421 5890 | hasnata223@gmail.com
PROFILE SUMMARY
Full-Stack Developer with hands-on experience across the React ecosystem and
Node.js back ends. Currently completing a research-based Master's degree in
Information Technology, building on a Bachelor's foundation in Computer Science.
TECHNICAL SKILLS
Frontend: React, Vite, Tailwind CSS
Backend: Node.js, Express
PROFESSIONAL EXPERIENCE
React Developer | SimpleX Technology Sep 2024 - Feb 2026
• Built desktop applications using Windows Forms and WPF, delivering modern interfaces.
• Applied asynchronous programming patterns to improve application performance.
PROJECTS
MavaEvents
Full-featured WordPress website built for a corporate training company serving
the oil and gas sectors across 50+ countries.
EDUCATION
Bachelor of Science in Computer Science 2017 - 2021
University of Lahore
"""


class _Job:
    title = "Senior Support Engineer"
    company = "GitLab"
    location = "Remote"
    description = (
        "You will own customer escalations end to end, working with "
        "distributed teams to resolve production incidents. Strong "
        "troubleshooting and clear written communication are essential. "
        "We use Zendesk for ticketing and expect ownership of the "
        "customer experience across every interaction."
    )
    requirements = "Experience with ticketing systems and escalation handling."


# ==========================================================================
# Planning: what may be touched at all
# ==========================================================================

class TestBlockPlanning:
    """The master is divided before a model sees any of it."""

    def test_blocks_reassemble_into_the_master_exactly(self):
        """Nothing may be lost or reordered by the split itself."""
        blocks = plan_blocks(MASTER)

        assert "\n".join(block.output for block in blocks) == MASTER

    @pytest.mark.parametrize(
        "line",
        [
            "HASNAT TAHIR",
            "Petaling Jaya, Selangor, Malaysia | +60 10-421 5890 | hasnata223@gmail.com",
            "React Developer | SimpleX Technology Sep 2024 - Feb 2026",
            "Bachelor of Science in Computer Science 2017 - 2021",
            "University of Lahore",
            "Frontend: React, Vite, Tailwind CSS",
            "MavaEvents",
        ],
    )
    def test_facts_are_never_offered_for_rewriting(self, line):
        """Contact details, employers, dates, skills and names stay verbatim."""
        blocks = plan_blocks(MASTER)
        match = next(b for b in blocks if b.text.strip() == line)

        assert not match.rewritable

    def test_the_skills_list_stays_a_list(self):
        """
        Grouping every run of consecutive lines swallowed the whole skills
        list into one block, and the model returned it as a single sentence.
        """
        blocks = plan_blocks(MASTER)
        skills = [b for b in blocks if b.text.strip().startswith(("Frontend:", "Backend:"))]

        assert len(skills) == 2
        assert all(not b.rewritable for b in skills)

    def test_each_bullet_is_its_own_block(self):
        """A bullet must not be merged with the one after it."""
        blocks = plan_blocks(MASTER)
        bullets = [b for b in blocks if b.text.strip().startswith("•")]

        assert len(bullets) == 2
        assert all(b.rewritable for b in bullets)

    def test_a_wrapped_paragraph_is_one_block(self):
        """
        A resume exported from a PDF hard-wraps prose. Rewriting each fragment
        separately produces sentences stitched from unrelated halves.
        """
        blocks = plan_blocks(MASTER)
        summary = next(b for b in blocks if b.text.startswith("Full-Stack Developer"))

        assert "Bachelor's foundation" in summary.text
        assert summary.rewritable


# ==========================================================================
# Acceptance: what a rewrite is allowed to be
# ==========================================================================

class TestAcceptRewrite:
    """Each rewrite is judged against the block it came from, and only that."""

    SOURCE = "• Applied asynchronous programming patterns to improve application performance."

    def test_a_genuine_rewording_is_accepted(self):
        assert accept_rewrite(
            self.SOURCE,
            "• Used asynchronous programming patterns to improve application "
            "performance and responsiveness.",
        ) is None

    def test_the_postings_responsibilities_are_refused(self):
        """The forgery this module exists to prevent."""
        refusal = accept_rewrite(
            self.SOURCE,
            "• Owned customer escalations end to end and resolved production "
            "incidents with distributed teams.",
        )

        assert refusal is not None
        assert "says something else" in refusal

    def test_claiming_the_hiring_company_is_refused(self):
        refusal = accept_rewrite(
            self.SOURCE,
            "• Applied asynchronous programming patterns to improve "
            "application performance at GitLab.",
            company="GitLab",
        )

        assert refusal == "claims work at GitLab"

    def test_an_invented_figure_is_refused(self):
        refusal = accept_rewrite(
            self.SOURCE,
            "• Applied asynchronous programming patterns to improve "
            "application performance by 40 percent.",
        )

        assert refusal is not None
        assert "invented a figure" in refusal

    def test_a_dropped_figure_is_refused(self):
        refusal = accept_rewrite(
            "Served customers across 50+ countries with a documented process.",
            "Served customers across many countries with a documented process.",
        )

        assert refusal == "dropped a figure the master states"

    def test_a_dropped_qualification_is_refused(self):
        """A summary is where a degree goes missing unnoticed."""
        refusal = accept_rewrite(
            "Completing a research-based Master's degree in Information "
            "Technology, building on a Bachelor's foundation in Computer Science.",
            "Completing research-based postgraduate study in Information "
            "Technology, building on a foundation in Computer Science.",
        )

        assert refusal is not None
        assert "qualification" in refusal

    def test_a_summarised_block_is_refused(self):
        refusal = accept_rewrite(self.SOURCE, "• Improved performance.")

        assert refusal is not None
        assert "summarised away" in refusal

    def test_padding_beyond_the_master_is_refused(self):
        """
        "providing strategic access to resources and expertise", bolted onto a
        line about running computer labs. Every word of it was invented.
        """
        source = "• Managed computer labs and coursework delivery for Semesters 1 through 4."
        supported = content_words(MASTER) | set(posting_vocabulary(_Job(), limit=120))

        refusal = accept_rewrite(
            source,
            "• Managed computer labs and coursework delivery for Semesters 1 "
            "through 4, providing strategic guidance, mentorship, thought "
            "leadership and pastoral oversight.",
            supported=supported,
        )

        assert refusal is not None
        assert "does not support" in refusal

    def test_reaching_for_the_postings_vocabulary_is_allowed(self):
        """
        Adopting the employer's words is the entire point of tailoring.

        The line still has to be recognisably the same line: a rewrite may
        put the posting's vocabulary around what the candidate did, not swap
        the subject matter out from under it.
        """
        supported = content_words(MASTER) | set(posting_vocabulary(_Job(), limit=120))

        assert accept_rewrite(
            "• Handled difficult customer issues in production and saw each one "
            "through to a documented fix.",
            "• Owned difficult customer escalations in production and saw each "
            "issue through to a documented resolution.",
            supported=supported,
        ) is None


# ==========================================================================
# The whole pass
# ==========================================================================

class TestRewritePass:
    """End to end, with the model replaced by a scripted one."""

    pytestmark = pytest.mark.asyncio

    async def test_output_is_never_shorter_than_the_master_in_substance(self):
        """Every block the model fails on falls back to the master's own text."""
        async def refuses_everything(system, user):
            return ""

        outcome = await AnchoredRewriter(refuses_everything).rewrite(MASTER, _Job())

        assert outcome.text.strip() == MASTER.strip()
        assert outcome.rewritten_blocks == 0

    async def test_accepted_rewordings_replace_only_their_own_block(self):
        async def rewords_bullets(system, user):
            lines = []
            for index in range(user.count("[") - user.count("[The")):
                lines.append(f"[{index + 1}] placeholder")
            return "\n".join(lines)

        outcome = await AnchoredRewriter(rewords_bullets).rewrite(MASTER, _Job())

        # "placeholder" fails every check, so nothing may be adopted.
        assert "placeholder" not in outcome.text
        assert "SimpleX Technology" in outcome.text

    async def test_a_model_that_errors_leaves_the_master_intact(self):
        async def explodes(system, user):
            raise RuntimeError("ollama is not running")

        outcome = await AnchoredRewriter(explodes).rewrite(MASTER, _Job())

        assert outcome.text.strip() == MASTER.strip()
        assert not outcome.changed_anything

    async def test_bullet_markers_survive_a_rewrite(self):
        async def rewords(system, user):
            return (
                "[1] Constructed desktop applications using Windows Forms and "
                "WPF, delivering modern interfaces.\n"
                "[2] Used asynchronous programming patterns to improve "
                "application performance.\n"
                "[3] Full-Stack Developer with hands-on practice across the "
                "React ecosystem and Node.js back ends. Currently completing a "
                "research-based Master's degree in Information Technology, "
                "building on a Bachelor's foundation in Computer Science.\n"
                "[4] Full-featured WordPress website delivered for a corporate "
                "training company serving the oil and gas sectors across 50+ "
                "countries."
            )

        outcome = await AnchoredRewriter(rewords).rewrite(MASTER, _Job())

        for line in outcome.text.split("\n"):
            if "desktop applications" in line or "asynchronous" in line:
                assert line.strip().startswith("•")


class TestLongBlocks:
    """Where clipping hides: a paragraph keeps its opening and loses its list."""

    SOURCE = (
        "Full-stack AI solutions platform built with React/Vite and "
        "Express/MongoDB. Features an AI voice assistant powered by local "
        "Ollama models, live information retrieval, speech-to-text and "
        "text-to-speech, a client portal secured with one-time-code "
        "authentication, an admin quote-management system, transactional "
        "email, and real-time WebSocket communication."
    )

    def test_a_paragraph_that_loses_its_second_half_is_refused(self):
        """
        Passed the general 50% bar by keeping its opening intact — while the
        client portal, the admin system and the email integration were gone.
        """
        refusal = accept_rewrite(
            self.SOURCE,
            "A high-consumption AI solutions platform was built on top of "
            "React/Vite and Express/MongoDB. The platform features a "
            "self-powered AI voice assistant based on local Ollama models, "
            "live information retrieval, speech-to-text, and text-to-speech "
            "capabilities.",
        )

        assert refusal is not None
        assert "says something else" in refusal

    def test_a_faithful_rewording_of_a_long_paragraph_is_accepted(self):
        assert accept_rewrite(
            self.SOURCE,
            "Full-stack AI solutions platform delivered with React/Vite and "
            "Express/MongoDB. Provides an AI voice assistant powered by local "
            "Ollama models, live information retrieval, speech-to-text and "
            "text-to-speech, a client portal secured with one-time-code "
            "authentication, an admin quote-management system, transactional "
            "email, and real-time WebSocket communication.",
        ) is None
