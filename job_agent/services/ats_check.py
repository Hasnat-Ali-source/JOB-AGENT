"""
ATS readiness check.

Scores a resume on what an applicant tracking system can actually do with it,
and says exactly what to change. Every check here is mechanical — something a
parser either can or cannot do — so the score means something rather than
being a number a model felt like giving.

**What this deliberately does not measure.** Whether the candidate is any good,
or whether they suit a particular job. A resume can be perfectly parseable and
still be the wrong resume; that is what the fit report is for. Keeping the two
apart matters, because an ATS score that quietly rewards stuffing a resume with
a job's keywords is a score that rewards lying.

The improver that acts on these findings may only rearrange, relabel and
resurface what the resume already says. Nothing in this module suggests adding
experience, and the fabrication check runs over anything it produces.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)

# The headings parsers look for. A resume that calls its work history
# "What I've been up to" is readable by a person and invisible to a parser.
STANDARD_SECTIONS = {
    "experience": (
        r"\b(work|professional|employment)?\s*experience\b",
        r"\bemployment history\b",
        r"\bwork history\b",
    ),
    "education": (r"\beducation\b", r"\bacademic\b", r"\bqualifications\b"),
    "skills": (r"\b(technical\s+)?skills\b", r"\bcompetenc", r"\bproficienc"),
    "summary": (r"\bsummary\b", r"\bprofile\b", r"\bobjective\b", r"\babout me\b"),
}

# Characters that survive a PDF but confuse naive parsers, and layout habits
# that break text extraction order.
PARSE_HOSTILE = {
    "••": "doubled bullet glyphs",
    "\t": "tab-aligned columns",
}

ACTION_VERBS = {
    "achieved", "built", "created", "delivered", "designed", "developed",
    "drove", "handled", "implemented", "improved", "led", "managed",
    "mentored", "negotiated", "operated", "owned", "reduced", "resolved",
    "supported", "trained", "increased", "launched", "maintained", "coordinated",
}

CONTACT_PATTERNS = {
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    "phone": re.compile(r"(\+?\d[\d\s().-]{7,}\d)"),
}

# A resume shorter than this is too thin for a parser to rank; longer than this
# and the relevant half is buried.
MIN_WORDS = 180
MAX_WORDS = 1200

PASSING_SCORE = 80


@dataclass
class Finding:
    """One thing an ATS will struggle with, and what to do about it."""

    check: str
    passed: bool
    weight: int
    detail: str
    fix: str = ""

    def to_dict(self) -> dict:
        """Serialize for the dashboard."""
        return {
            "check": self.check,
            "passed": self.passed,
            "weight": self.weight,
            "detail": self.detail,
            "fix": self.fix,
        }


@dataclass
class ATSReport:
    """How well a resume survives an applicant tracking system."""

    score: int = 0
    findings: List[Finding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Whether the resume is ready to send as it stands."""
        return self.score >= PASSING_SCORE

    @property
    def failures(self) -> List[Finding]:
        """Only what needs fixing, worst first."""
        return sorted(
            (f for f in self.findings if not f.passed),
            key=lambda f: f.weight,
            reverse=True,
        )

    def to_dict(self) -> dict:
        """Serialize for the dashboard."""
        return {
            "score": self.score,
            "passed": self.passed,
            "passing_score": PASSING_SCORE,
            "findings": [f.to_dict() for f in self.findings],
            "failures": [f.to_dict() for f in self.failures],
        }


class ATSCheck:
    """Scores a resume on machine readability."""

    @staticmethod
    def run(text: str) -> ATSReport:
        """
        Score a resume and list what would improve it.

        Args:
            text: The resume's plain text, as extracted from its file

        Returns:
            An ATSReport scored out of 100
        """
        text = text or ""
        lower = text.lower()
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        words = re.findall(r"[A-Za-z']+", text)

        findings: List[Finding] = [
            ATSCheck._check_contact(text),
            ATSCheck._check_sections(lower),
            ATSCheck._check_dates(text),
            ATSCheck._check_bullets(lines),
            ATSCheck._check_action_verbs(text, lines),
            ATSCheck._check_measurables(text),
            ATSCheck._check_length(words),
            ATSCheck._check_parse_hazards(text),
        ]

        earned = sum(f.weight for f in findings if f.passed)
        possible = sum(f.weight for f in findings) or 1

        report = ATSReport(score=round(100 * earned / possible), findings=findings)

        logger.info(
            f"ATS check: {report.score}/100 "
            f"({len(report.failures)} finding(s) to address)"
        )

        return report

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    @staticmethod
    def _check_contact(text: str) -> Finding:
        """An unreachable candidate is filtered before a human sees them."""
        found = [name for name, pattern in CONTACT_PATTERNS.items() if pattern.search(text)]

        return Finding(
            check="Contact details",
            passed=len(found) == len(CONTACT_PATTERNS),
            weight=15,
            detail=(
                "Email and phone are both present"
                if len(found) == len(CONTACT_PATTERNS)
                else f"Missing: {', '.join(set(CONTACT_PATTERNS) - set(found))}"
            ),
            fix="Put an email address and phone number in the header, as plain text.",
        )

    @staticmethod
    def _check_sections(lower: str) -> Finding:
        """Parsers route content by heading; unusual ones lose whole sections."""
        missing = [
            name for name, patterns in STANDARD_SECTIONS.items()
            if not any(re.search(p, lower) for p in patterns)
        ]

        return Finding(
            check="Standard section headings",
            passed=not missing,
            weight=20,
            detail=(
                "Experience, education, skills and summary are all labelled"
                if not missing
                else f"No heading a parser recognises for: {', '.join(missing)}"
            ),
            fix=(
                "Use the plain words a parser expects — Summary, Skills, "
                "Experience, Education — as their own lines."
            ),
        )

    @staticmethod
    def _check_dates(text: str) -> Finding:
        """Employment gaps and tenure are read from date ranges."""
        ranges = re.findall(
            r"(19|20)\d{2}\s*[–—-]\s*((19|20)\d{2}|present|current)",
            text,
            re.IGNORECASE,
        )

        return Finding(
            check="Dated roles",
            passed=len(ranges) >= 1,
            weight=15,
            detail=(
                f"{len(ranges)} role(s) carry a date range"
                if ranges
                else "No role has a start–end date a parser can read"
            ),
            fix="Give every role a range like 'Mar 2023 – Feb 2026'.",
        )

    @staticmethod
    def _check_bullets(lines: List[str]) -> Finding:
        """Achievements in prose paragraphs are ranked far worse than bullets."""
        bullets = [line for line in lines if re.match(r"^[-•*·]\s+", line)]

        return Finding(
            check="Bulleted achievements",
            passed=len(bullets) >= 4,
            weight=10,
            detail=f"{len(bullets)} bulleted line(s)",
            fix="Break responsibilities into single-line bullets, one claim each.",
        )

    @staticmethod
    def _check_action_verbs(text: str, lines: List[str]) -> Finding:
        """
        A bullet that opens with a verb reads as a result, not a duty.

        Only experience bullets are judged. A skills list is supposed to be
        noun phrases — "CRM Tools & Ticketing Systems" is correct, and marking
        it down taught the improver to break a perfectly good resume.
        """
        bullets = ATSCheck._experience_bullets(text) or [
            line for line in lines if re.match(r"^[-•*·]\s+", line)
        ]

        if not bullets:
            return Finding(
                check="Bullets open with an action",
                passed=False,
                weight=10,
                detail="There are no bullets to check",
                fix="Write bullets that begin with what you did: Led, Resolved, Trained.",
            )

        opening = [
            re.sub(r"^[-•*·]\s+", "", line).split(" ")[0].lower().strip(",.")
            for line in bullets
        ]
        strong = [word for word in opening if word in ACTION_VERBS]
        ratio = len(strong) / len(bullets)

        return Finding(
            check="Bullets open with an action",
            passed=ratio >= 0.5,
            weight=10,
            detail=f"{len(strong)} of {len(bullets)} bullets start with an action verb",
            fix="Start each bullet with the verb: Led, Resolved, Trained, Reduced.",
        )

    @staticmethod
    def _experience_bullets(text: str) -> List[str]:
        """
        The bullets under the experience heading, where verbs belong.

        Args:
            text: The whole resume

        Returns:
            Bullet lines from the experience section, empty if it can't be found
        """
        lines = text.splitlines()
        start = None

        for index, line in enumerate(lines):
            if re.match(r"^\s*(work|professional|employment)?\s*experience\s*:?\s*$",
                        line.strip(), re.IGNORECASE):
                start = index + 1
                break

        if start is None:
            return []

        collected = []

        for line in lines[start:]:
            stripped = line.strip()

            # Stop at the next section heading.
            if re.match(r"^(education|skills|certifications?|projects?|languages?"
                        r"|interests?|references?)\s*:?$", stripped, re.IGNORECASE):
                break

            if re.match(r"^[-•*·]\s+", stripped):
                collected.append(stripped)

        return collected

    @staticmethod
    def _check_measurables(text: str) -> Finding:
        """Numbers are what a screener remembers and a parser can rank."""
        numbers = re.findall(r"\b\d+(?:\.\d+)?%|\b\d{2,}\b|\b\d+\+", text)

        return Finding(
            check="Measured results",
            passed=len(numbers) >= 3,
            weight=10,
            detail=f"{len(numbers)} figure(s) in the text",
            fix=(
                "Attach real numbers you already know to existing claims — "
                "team sizes, ticket volumes, satisfaction scores."
            ),
        )

    @staticmethod
    def _check_length(words: List[str]) -> Finding:
        """Too short is unrankable; too long buries the relevant half."""
        count = len(words)
        ok = MIN_WORDS <= count <= MAX_WORDS

        return Finding(
            check="Length",
            passed=ok,
            weight=10,
            detail=f"{count} words",
            fix=(
                f"Aim for {MIN_WORDS}–{MAX_WORDS} words; "
                f"{'expand the experience section' if count < MIN_WORDS else 'cut the least relevant roles'}."
            ),
        )

    @staticmethod
    def _check_parse_hazards(text: str) -> Finding:
        """Layout tricks that survive a PDF and scramble extracted text."""
        hazards = [label for token, label in PARSE_HOSTILE.items() if token in text]

        # A line with several runs of spaces is a column laid out by hand.
        if re.search(r"\S {4,}\S {4,}\S", text):
            hazards.append("space-aligned columns")

        # Pipes are not a hazard at all. "Role | Company | Dates" and
        # "City | Email | Phone" are the standard single-line convention every
        # parser handles; flagging them marked a correctly formatted resume as
        # broken, which is worse than not checking. Real column layout shows up
        # as the space alignment caught above.

        return Finding(
            check="Machine-readable layout",
            passed=not hazards,
            weight=10,
            detail=("No layout hazards found" if not hazards else f"Found: {', '.join(hazards)}"),
            fix="Use single-column text; no tables, tabs or hand-aligned columns.",
        )


def check_ats(text: str) -> ATSReport:
    """
    Convenience wrapper around ATSCheck.run.

    Args:
        text: Resume text

    Returns:
        An ATSReport
    """
    return ATSCheck.run(text)
