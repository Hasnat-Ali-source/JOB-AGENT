"""
ATS improver.

Takes a resume that scored below the passing mark and fixes what the ATS check
objected to — without adding anything the resume does not already say.

**The line this holds.** Every improvement here is a change of form: a heading
renamed to the word a parser expects, a paragraph split into the bullets it
already contained, a bullet reordered so the verb leads. Nothing invents an
employer, a date, a tool, a metric or a responsibility. The result is passed
through the same fabrication check as a tailored document, and a rewrite that
fails it is discarded rather than shown.

That constraint is what makes the score worth having. An improver that closes
the gap by writing in the keywords a posting wants produces a resume that
scores well and cannot be defended in an interview.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from job_agent.services.ats_check import ACTION_VERBS, ATSReport, check_ats
from job_agent.services.fabrication_check import verify_no_fabrication

logger = logging.getLogger(__name__)

# Headings a parser recognises, and the loose wording they replace.
HEADING_FIXES = [
    (re.compile(r"^\s*(profile summary|professional summary|about me|profile)\s*:?\s*$", re.I),
     "SUMMARY"),
    (re.compile(r"^\s*(core skills|key skills|technical skills|competencies)\s*:?\s*$", re.I),
     "SKILLS"),
    (re.compile(r"^\s*(professional experience|work history|employment history|career history)\s*:?\s*$", re.I),
     "EXPERIENCE"),
    (re.compile(r"^\s*(academics?|qualifications|educational background)\s*:?\s*$", re.I),
     "EDUCATION"),
]

# Openers that turn a duty into a claim. Mapped only where the original wording
# already says the same thing — "Responsible for training" becomes "Trained".
VERB_REWRITES = [
    (re.compile(r"^responsible for ([a-z]+)ing\b", re.I), r"\1ed"),
    (re.compile(r"^tasked with ([a-z]+)ing\b", re.I), r"\1ed"),
    (re.compile(r"^duties included ([a-z]+)ing\b", re.I), r"\1ed"),
    (re.compile(r"^worked on\b", re.I), "Delivered"),
    (re.compile(r"^helped (to )?", re.I), "Supported "),
    (re.compile(r"^involved in ([a-z]+)ing\b", re.I), r"\1ed"),
]


@dataclass
class Improvement:
    """One change made to the resume, and why."""

    change: str
    before: str = ""
    after: str = ""

    def to_dict(self) -> dict:
        """Serialize for the dashboard."""
        return {"change": self.change, "before": self.before, "after": self.after}


@dataclass
class ImprovementResult:
    """A rewritten resume, with the score before and after."""

    text: str
    score_before: int
    score_after: int
    improvements: List[Improvement] = field(default_factory=list)
    fabrication_flags: List[str] = field(default_factory=list)
    report_after: Optional[ATSReport] = None

    @property
    def is_safe(self) -> bool:
        """True when nothing unsupported by the original was introduced."""
        return not self.fabrication_flags

    @property
    def improved(self) -> bool:
        """True when the rewrite is both safe and actually better."""
        return self.is_safe and self.score_after > self.score_before

    def to_dict(self) -> dict:
        """Serialize for the dashboard."""
        return {
            "score_before": self.score_before,
            "score_after": self.score_after,
            "improved": self.improved,
            "is_safe": self.is_safe,
            "fabrication_flags": self.fabrication_flags,
            "improvements": [i.to_dict() for i in self.improvements],
            "text": self.text,
            "report_after": self.report_after.to_dict() if self.report_after else None,
        }


class ATSImprover:
    """Fixes the mechanical faults an ATS check finds."""

    @staticmethod
    def run(text: str) -> ImprovementResult:
        """
        Improve a resume's machine readability, changing form only.

        Args:
            text: The resume's plain text

        Returns:
            An ImprovementResult carrying the rewrite and both scores
        """
        original = text or ""
        before = check_ats(original)

        working = original
        improvements: List[Improvement] = []

        working, made = ATSImprover._standardise_headings(working)
        improvements.extend(made)

        working, made = ATSImprover._unwrap_paragraphs(working)
        improvements.extend(made)

        working, made = ATSImprover._bullet_list_items(working)
        improvements.extend(made)

        working, made = ATSImprover._lead_with_verbs(working)
        improvements.extend(made)

        working, made = ATSImprover._strip_layout_hazards(working)
        improvements.extend(made)

        after = check_ats(working)

        # Held to the same rule as a tailored document: form may change, facts
        # may not.
        flags = verify_no_fabrication(original, working)

        result = ImprovementResult(
            text=working,
            score_before=before.score,
            score_after=after.score,
            improvements=improvements,
            fabrication_flags=flags,
            report_after=after,
        )

        logger.info(
            f"ATS improver: {before.score} → {after.score} "
            f"({len(improvements)} change(s), "
            f"{'safe' if result.is_safe else 'FLAGGED'})"
        )

        return result

    # ------------------------------------------------------------------
    # Individual improvements
    # ------------------------------------------------------------------

    @staticmethod
    def _standardise_headings(text: str) -> Tuple[str, List[Improvement]]:
        """Rename headings to the words a parser routes on."""
        lines = text.splitlines()
        made: List[Improvement] = []

        for index, line in enumerate(lines):
            for pattern, replacement in HEADING_FIXES:
                if pattern.match(line):
                    if line.strip().upper() != replacement:
                        made.append(
                            Improvement(
                                change="Renamed a heading to the standard word",
                                before=line.strip(),
                                after=replacement,
                            )
                        )
                        lines[index] = replacement
                    break

        return "\n".join(lines), made

    @staticmethod
    def _unwrap_paragraphs(text: str) -> Tuple[str, List[Improvement]]:
        """
        Split a paragraph of achievements into the bullets it already is.

        Only sentences already separated by full stops are split — this makes
        existing content scannable, it does not write new content.
        """
        lines = text.splitlines()
        output: List[str] = []
        made: List[Improvement] = []

        for line in lines:
            stripped = line.strip()
            sentences = re.split(r"(?<=[.!?])\s+", stripped)

            is_prose_block = (
                len(stripped.split()) > 28
                and len(sentences) >= 3
                and not re.match(r"^[-•*·]", stripped)
                and not stripped.isupper()
            )

            if is_prose_block:
                for sentence in sentences:
                    if sentence.strip():
                        output.append(f"- {sentence.strip()}")

                made.append(
                    Improvement(
                        change="Split a paragraph into bullets",
                        before=stripped[:80],
                        after=f"{len(sentences)} bullets",
                    )
                )
            else:
                output.append(line)

        return "\n".join(output), made

    @staticmethod
    def _bullet_list_items(text: str) -> Tuple[str, List[Improvement]]:
        """
        Mark up lines that are already a list but carry no bullet.

        A run of short lines under Skills or Experience is a list whatever it
        looks like; giving each one a marker is what lets a parser count them
        as separate claims. The words are untouched.
        """
        lines = text.splitlines()
        made: List[Improvement] = []
        in_list_section = False
        marked = 0

        for index, line in enumerate(lines):
            stripped = line.strip()

            if not stripped:
                continue

            if re.match(r"^(SUMMARY|SKILLS|EXPERIENCE|EDUCATION|PROJECTS)$", stripped):
                in_list_section = stripped in ("SKILLS", "EXPERIENCE")
                continue

            if not in_list_section:
                continue

            already_marked = re.match(r"^[-•*·]", stripped)
            looks_like_an_entry = 2 <= len(stripped.split()) <= 20

            if not already_marked and looks_like_an_entry:
                lines[index] = f"- {stripped}"
                marked += 1

        if marked:
            made.append(
                Improvement(
                    change="Marked existing list lines as bullets",
                    before=f"{marked} unmarked line(s)",
                    after=f"{marked} bullet(s)",
                )
            )

        return "\n".join(lines), made

    @staticmethod
    def _lead_with_verbs(text: str) -> Tuple[str, List[Improvement]]:
        """Turn "Responsible for training…" into "Trained…" — same claim."""
        lines = text.splitlines()
        made: List[Improvement] = []

        for index, line in enumerate(lines):
            match = re.match(r"^(\s*[-•*·]\s*)(.+)$", line)

            if not match:
                continue

            marker, body = match.groups()
            first = body.split(" ")[0].lower().strip(",.")

            if first in ACTION_VERBS:
                continue

            for pattern, replacement in VERB_REWRITES:
                if pattern.match(body):
                    rewritten = pattern.sub(replacement, body, count=1).strip()
                    rewritten = rewritten[:1].upper() + rewritten[1:]

                    made.append(
                        Improvement(
                            change="Led the bullet with the action",
                            before=body[:70],
                            after=rewritten[:70],
                        )
                    )
                    lines[index] = f"{marker}{rewritten}"
                    break

        return "\n".join(lines), made

    @staticmethod
    def _strip_layout_hazards(text: str) -> Tuple[str, List[Improvement]]:
        """Remove the layout tricks that scramble extracted text."""
        made: List[Improvement] = []
        working = text

        if "\t" in working:
            working = re.sub(r"\t+", " ", working)
            made.append(Improvement(change="Replaced tab columns with plain spacing"))

        if re.search(r"\S {4,}\S {4,}\S", working):
            working = re.sub(r" {4,}", " ", working)
            made.append(Improvement(change="Removed hand-aligned columns"))

        if "••" in working:
            working = working.replace("••", "•")
            made.append(Improvement(change="Normalised doubled bullet glyphs"))

        return working, made


def improve_ats(text: str) -> ImprovementResult:
    """
    Convenience wrapper around ATSImprover.run.

    Args:
        text: Resume text

    Returns:
        An ImprovementResult
    """
    return ATSImprover.run(text)
