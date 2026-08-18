"""
Rewriting a resume *at* a posting, and reporting what that was worth.

This is the "fit my documents to this job" action. It is the most aggressive
tailoring in the project and it is still bounded by the same rule as the rest:
it may change how the candidate's experience is described, never what it is.

**What it actually does**, in the order that matters:

1. Reads the posting against the resume (`fit_report`, semantic) to find which
   requirements already have evidence and which do not.
2. Rewrites the **profile summary** as a positioning statement aimed at this
   posting. The summary is the one section whose whole job is framing, so it
   is where the largest honest gain is: the same career, introduced in the
   employer's language, against the requirements it can actually answer.
3. Runs the anchored block rewrite (`anchored_rewrite`) so every other passage
   is reworded into the posting's vocabulary, each checked against its own
   source.
4. Reorders bullets so the evidence for this posting's requirements leads.
5. Measures the fit again and reports the before and after.

**Why it will not reach 100% on every posting, and must not.** The score is
the share of the posting's requirements the resume can evidence. Where the
evidence exists, reframing surfaces it and the score rises — on a posting this
candidate genuinely fits, from the fifties to the high nineties. Where it does
not exist, no rewrite can create it: a resume that has never mentioned Ruby
cannot be made to evidence "deep experience with Ruby and Rails" without
saying something untrue, and the fit report names those separately as blocking
technologies precisely so the honest answer — a different posting — stays
visible.

That boundary is the product. A resume rewritten past it is a false statement
made to an employer under the user's name, from the user's email address, and
it is the user who carries that.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

# A summary the model returns that is longer than this is writing a new
# document rather than reframing one.
MAX_SUMMARY_WORDS = 110

# How much of the master's summary must survive. Looser than an ordinary
# block rewrite: reframing a positioning statement legitimately changes more
# than rewording a bullet does. Tight enough that "Full-Stack Developer with
# React and Node.js" cannot become "Support leader with a decade in SaaS".
MIN_SUMMARY_OVERLAP = 0.35


@dataclass
class FitRewriteResult:
    """What the rewrite changed, and what it was worth."""

    content_text: str
    score_before: int = 0
    score_after: int = 0
    summary_rewritten: bool = False
    passages_reworded: int = 0
    passages_total: int = 0
    blocking_technologies: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def gain(self) -> int:
        """Percentage points the rewrite added."""
        return self.score_after - self.score_before

    def describe(self) -> str:
        """One honest sentence about the outcome."""
        if self.blocking_technologies:
            missing = ", ".join(self.blocking_technologies[:3])

            return (
                f"Fit {self.score_before}% → {self.score_after}%. This posting "
                f"asks for {missing}, which your resume does not mention — that "
                f"gap is what is left, and it cannot be written in."
            )

        if self.gain > 0:
            return (
                f"Fit {self.score_before}% → {self.score_after}%, by reframing "
                f"what your resume already says for this posting."
            )

        return (
            f"Fit {self.score_after}%. Nothing could be reframed further "
            f"without saying something your resume does not support."
        )

    def to_dict(self) -> dict:
        """Serialize for the dashboard."""
        return {
            "score_before": self.score_before,
            "score_after": self.score_after,
            "gain": self.gain,
            "summary_rewritten": self.summary_rewritten,
            "passages_reworded": self.passages_reworded,
            "passages_total": self.passages_total,
            "blocking_technologies": self.blocking_technologies,
            "notes": self.notes,
            "message": self.describe(),
        }


class FitRewriter:
    """Rewrites a resume at one posting, as hard as honesty allows."""

    def __init__(self, tailoring_service: Optional[Any] = None) -> None:
        from job_agent.services.tailoring import get_tailoring_service

        self.tailoring = tailoring_service or get_tailoring_service()

    async def rewrite(self, master_text: str, job) -> FitRewriteResult:
        """
        Produce the best honest version of this resume for this posting.

        Args:
            master_text: The master resume
            job: The posting

        Returns:
            A FitRewriteResult. Its text is never worse than the master —
            every step falls back to the master's own wording on failure.
        """
        from job_agent.services.fit_report import analyse_fit_async

        posting_text = "\n".join(
            filter(
                None,
                [getattr(job, "description", ""), getattr(job, "requirements", "")],
            )
        )

        before = await analyse_fit_async(posting_text, master_text)

        result = FitRewriteResult(
            content_text=master_text,
            score_before=before.score,
            blocking_technologies=before.to_dict().get("blocking_technologies", []),
        )

        text = master_text
        best = before.score

        async def score(candidate: str) -> int:
            """The fit of one candidate rewrite."""
            return (await analyse_fit_async(posting_text, candidate)).score

        # Each step is measured on its own and kept only if it does not cost
        # anything. Applying them all and scoring once at the end meant a good
        # summary was thrown away because the passage rewording happened to
        # dilute a match — and the user got their untouched master back with
        # no idea which half had worked.

        # 1. The summary, reframed at this posting. The section whose whole
        #    job is framing, so the one with the most honest room to move.
        reframed = await self._reframe_summary(text, job, before)

        if reframed:
            reframed_score = await score(reframed)

            if reframed_score >= best:
                text, best = reframed, reframed_score
                result.summary_rewritten = True
                result.notes.append(
                    f"Rewrote the profile summary for this posting"
                    + (f" (+{reframed_score - before.score} points)"
                       if reframed_score > before.score else "")
                    + "."
                )
            else:
                result.notes.append(
                    "A reframed summary scored worse than your own, so yours "
                    "was kept."
                )

        # 2. Every other passage, reworded into the posting's vocabulary and
        #    checked against its own source.
        reworded_text, reworded, total = await self._reword_passages(text, job)
        result.passages_total = total

        if reworded:
            reworded_score = await score(reworded_text)

            if reworded_score >= best:
                text, best = reworded_text, reworded_score
                result.passages_reworded = reworded
                result.notes.append(
                    f"Reworded {reworded} of {total} passages into this "
                    f"employer's language."
                )
            else:
                result.notes.append(
                    f"Rewording the passages scored worse ({reworded_score}% "
                    f"against {best}%), so your own wording was kept."
                )

        # 3. Evidence for this posting leads its section. Reordering moves
        #    existing lines, so it cannot change what can be evidenced — no
        #    need to score it.
        keywords = self.tailoring._job_keywords(job)
        text, moved = self.tailoring._reorder_for_relevance(text, keywords)

        if moved:
            result.notes.append(
                f"Moved the most relevant bullets to the top of {moved} section(s)."
            )

        after = await analyse_fit_async(posting_text, text)

        result.content_text = text
        result.score_after = after.score
        result.blocking_technologies = after.to_dict().get(
            "blocking_technologies", []
        )

        logger.info(
            f"Fit rewrite for '{getattr(job, 'title', '?')}': "
            f"{before.score}% → {after.score}%"
        )

        return result

    async def _reframe_summary(self, master_text: str, job, fit) -> Optional[str]:
        """
        Rewrite the profile summary as a pitch for this posting.

        The summary is the one section whose purpose is framing rather than
        record, so it is where reframing is both legitimate and worth the
        most. It is still held to the master: the rewrite must be built from
        the same facts, must keep the qualifications, and must not name a
        technology the resume does not have.

        Args:
            master_text: The resume as it stands
            job: The posting
            fit: The fit report, used to tell the model which of the posting's
                requirements the candidate can actually speak to

        Returns:
            The resume with its summary replaced, or None if nothing usable
            came back
        """
        from job_agent.services.anchored_rewrite import posting_vocabulary

        section = _find_summary(master_text)

        if not section:
            return None

        start, end, original = section

        backend = await self.tailoring._chat_backend()

        if not backend:
            return None

        chat, _label = backend

        # Only requirements the resume can already evidence are offered as
        # angles. Handing the model the unmet ones is an invitation to claim
        # them, which is exactly what must not happen.
        evidenced = [match.requirement for match in fit.matched + fit.partial][:6]

        # Named literally rather than left to "keep every qualification".
        # Compressed to a word budget, the model drops the degree first every
        # time — and the honesty check then refuses the whole rewrite, so the
        # one section with the most to gain was never reframed at all.
        must_keep = _qualifications_in(original)
        budget = max(MAX_SUMMARY_WORDS, len(original.split()))

        prompt = f"""Rewrite this candidate's professional summary so it reads
as a strong fit for the job below.

--- The job ---
Title: {getattr(job, 'title', '')}
Company: {getattr(job, 'company', '')}
Words this employer uses: {', '.join(posting_vocabulary(job, limit=25))}

--- What this candidate can genuinely speak to ---
{chr(10).join(f'- {requirement}' for requirement in evidenced) or '- (nothing listed)'}

--- Their current summary, and the only source of facts ---
{original}

--- Rules ---
- Use only what the summary above already says. You may change every word of
  how it is said; you may not add a fact.
- Never name a technology, tool, employer, product or number that is not in
  the summary above.
- Do not claim to have worked for {getattr(job, 'company', 'this employer')}.
- Do not claim years of experience the summary does not state.
- These must appear, word for word: {'; '.join(must_keep) if must_keep else '(nothing specific)'}
- Up to {budget} words. Do not compress — a shorter summary is not a better one.

Write only the new summary. No heading, no preamble, no commentary.
"""

        system = (
            "You reframe a candidate's own professional summary for a "
            "specific job. You never invent experience, skills, employers or "
            "numbers, and you never drop a qualification. Output only the "
            "summary."
        )

        # One retry, told exactly what was wrong. A small model drops a degree
        # while compressing and fixes it when asked directly, and the
        # alternative is silently keeping the untailored summary.
        attempt = prompt

        for round_number in (1, 2):
            try:
                raw = await chat(system, attempt)
            except Exception as e:
                logger.info(f"Could not reframe the summary ({type(e).__name__}: {e})")
                return None

            candidate = self.tailoring._clean(raw or "").strip()

            if not candidate:
                return None

            refusal = _summary_is_honest(
                original, candidate, str(getattr(job, "company", "") or "")
            )

            if not refusal:
                break

            logger.info(
                f"Refused the reframed summary (attempt {round_number}): {refusal}"
            )

            if round_number == 2:
                return None

            attempt = (
                f"{prompt}\n\nYour previous attempt was rejected because it "
                f"{refusal}. Write it again, fixing exactly that. Keep every "
                f"qualification and every figure from the original."
            )

        return "\n".join(
            master_text.split("\n")[:start]
            + [candidate]
            + master_text.split("\n")[end:]
        )

    async def _reword_passages(self, text: str, job) -> tuple:
        """
        Reword every prose block into the posting's vocabulary.

        Args:
            text: The resume, possibly with an already-reframed summary
            job: The posting

        Returns:
            (text, how many blocks were reworded, how many were rewritable)
        """
        from job_agent.services.anchored_rewrite import AnchoredRewriter

        backend = await self.tailoring._chat_backend()

        if not backend:
            return text, 0, 0

        chat, _label = backend

        outcome = await AnchoredRewriter(chat).rewrite(text, job)

        return outcome.text, outcome.rewritten_blocks, outcome.rewritable_blocks


def _qualifications_in(text: str) -> List[str]:
    """
    The qualification phrases a summary states, to be repeated back verbatim.

    Args:
        text: The original summary

    Returns:
        Short phrases naming a degree or certification
    """
    import re

    from job_agent.services.anchored_rewrite import CREDENTIAL_TERMS

    found: List[str] = []

    for term in CREDENTIAL_TERMS:
        for match in re.finditer(
            rf"[\w'’]*{re.escape(term)}[\w'’]*(?:[\s\w]{{0,40}}?(?:in|of)\s+[\w\s]{{0,40}})?",
            text,
            re.IGNORECASE,
        ):
            phrase = " ".join(match.group(0).split()).strip(" ,.;")

            if phrase and phrase not in found:
                found.append(phrase)

    return found[:4]


def _find_summary(master_text: str) -> Optional[tuple]:
    """
    Locate the profile summary section.

    Args:
        master_text: The resume

    Returns:
        (first line index, line index after the section, the section's text),
        or None when the resume has no summary
    """
    from job_agent.services.document_parser import DocumentParser

    lines = master_text.split("\n")
    start = None

    for index, line in enumerate(lines):
        stripped = line.strip()

        if start is None:
            if DocumentParser.is_heading(stripped) == "summary":
                start = index + 1
            continue

        # The section ends at the next heading.
        if stripped and DocumentParser.is_heading(stripped):
            body = "\n".join(lines[start:index]).strip()

            return (start, index, body) if body else None

    if start is not None:
        body = "\n".join(lines[start:]).strip()

        return (start, len(lines), body) if body else None

    return None


def _summary_is_honest(original: str, candidate: str, company: str) -> Optional[str]:
    """
    Whether a reframed summary may replace the one the candidate wrote.

    Looser than the per-block rule that governs bullets — reframing a
    positioning statement legitimately rewrites more of it — and strict about
    the things that make a claim false rather than merely differently phrased.

    Args:
        original: The master's own summary
        candidate: What the model returned
        company: The hiring company, which a summary may not claim to have
            worked for

    Returns:
        The reason it was refused, or None if it may be used
    """
    from job_agent.services.anchored_rewrite import (
        CREDENTIAL_TERMS,
        content_words,
        numbers_in,
    )

    words = candidate.split()

    if len(words) < 15:
        return f"only {len(words)} words — the summary would be thinner, not sharper"

    if len(words) > MAX_SUMMARY_WORDS * 1.4:
        return f"{len(words)} words — it has become a new document"

    invented = numbers_in(candidate) - numbers_in(original)

    if invented:
        return f"invented a figure ({', '.join(sorted(invented)[:3])})"

    if company and len(company) > 2 and company.lower() in candidate.lower():
        if company.lower() not in original.lower():
            return f"claims a connection to {company}"

    lost = [
        term for term in CREDENTIAL_TERMS
        if term in original.lower() and term not in candidate.lower()
    ]

    if lost:
        return f"dropped a qualification the master states ({lost[0]})"

    wanted = content_words(original)

    if wanted:
        kept = len(wanted & content_words(candidate)) / len(wanted)

        if kept < MIN_SUMMARY_OVERLAP:
            return (
                f"only {int(kept * 100)}% of the original's substance survived — "
                f"this is a different candidate, not a different framing"
            )

    return None
