"""
Per-requirement fit report.

Reads what a posting actually asks for, line by line, and says which of those
the candidate's real resume can answer — with the sentence from the resume that
answers it. Where nothing answers it, it says so.

**Why this exists.** A single fit score tells you a resume is a 0.58 and
nothing about what to do. This says "they ask for five things, you can evidence
three, and here is the wording in your own resume that proves them" — which is
what makes a truthful reframing possible, and what makes it obvious when a
posting is not worth an application at all.

**What it will not do.** Close a gap. A requirement with no evidence stays
missing; the honest response to that is a different posting, not a different
resume. Everything downstream of this report may reorder and reword what the
resume says, never add to it.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)

# Words that carry no signal when matching a requirement to experience.
STOPWORDS = {
    "the", "and", "for", "with", "you", "your", "our", "are", "will", "have",
    "has", "this", "that", "from", "they", "their", "them", "who", "why",
    "how", "what", "when", "where", "able", "ability", "work", "working",
    "experience", "experienced", "years", "year", "strong", "excellent",
    "good", "great", "team", "role", "job", "position", "must", "should",
    "would", "can", "plus", "nice", "bonus", "preferred", "required", "skills",
    "knowledge", "understanding", "familiarity", "proven", "track", "record",
    "including", "such", "etc", "other", "others", "all", "any", "some",
    "well", "highly", "very", "self", "high", "using", "use", "used", "new",
    "across", "within", "into", "over", "more", "than", "also", "both",
}

# Terms that mean the same thing on a resume and in a posting. Without this a
# candidate who "handled escalations in Zendesk" reads as having nothing to do
# with a posting asking for "ticketing systems".
SYNONYMS = {
    "ticketing": {"zendesk", "freshdesk", "jira", "ticket", "tickets", "crm"},
    "crm": {"salesforce", "hubspot", "zendesk", "ticketing"},
    "escalation": {"escalations", "escalated", "complaints", "resolution"},
    "stakeholder": {"client", "clients", "customer", "customers", "employee"},
    "onboarding": {"induction", "training", "orientation", "onboard"},
    "training": {"mentored", "mentoring", "coaching", "trained", "onboarding"},
    "documentation": {"sop", "sops", "procedures", "documented", "knowledge"},
    "communication": {"communications", "correspondence", "liaison"},
    "operations": {"operational", "ops", "process", "processes"},
    "payroll": {"compensation", "benefits", "hris"},
    "compliance": {"regulatory", "policy", "policies", "gdpr"},
    "analysis": {"analytics", "reporting", "reports", "data"},
    "remote": {"distributed", "asynchronous", "async"},
}

# A requirement line shorter than this is a heading, not a requirement.
MIN_REQUIREMENT_WORDS = 4
MAX_REQUIREMENTS = 20

# Lines every posting carries that ask nothing of the candidate. Scored as
# requirements they drag the fit down and tell the user nothing.
BOILERPLATE = re.compile(
    r"privacy polic|recruitment privacy|equal opportunit|we encourage every"
    r"|apply now|talent acquisition|please review|cookie|about (remote|us)\b"
    r"|our mission|we.re always looking|by expressing your interest"
    r"|this is not an active job|future opening|evergreen pipeline"
    r"|report to our|you will report",
    re.IGNORECASE,
)


@dataclass
class RequirementMatch:
    """One thing the posting asks for, and what the resume says about it."""

    requirement: str
    verdict: str  # "matched" | "partial" | "missing"
    evidence: Optional[str] = None
    matched_terms: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize for the dashboard."""
        return {
            "requirement": self.requirement,
            "verdict": self.verdict,
            "evidence": self.evidence,
            "matched_terms": self.matched_terms,
        }


@dataclass
class FitReport:
    """What a posting asks for, against what the candidate can evidence."""

    matches: List[RequirementMatch] = field(default_factory=list)

    @property
    def matched(self) -> List[RequirementMatch]:
        """Requirements the resume can evidence outright."""
        return [m for m in self.matches if m.verdict == "matched"]

    @property
    def partial(self) -> List[RequirementMatch]:
        """Requirements with adjacent, arguable evidence."""
        return [m for m in self.matches if m.verdict == "partial"]

    @property
    def missing(self) -> List[RequirementMatch]:
        """Requirements nothing in the resume speaks to."""
        return [m for m in self.matches if m.verdict == "missing"]

    @property
    def score(self) -> int:
        """Share of requirements evidenced, counting partials at half."""
        if not self.matches:
            return 0

        earned = len(self.matched) + 0.5 * len(self.partial)

        return round(100 * earned / len(self.matches))

    @property
    def worth_applying(self) -> bool:
        """
        Whether an application here is worth the candidate's name.

        Not a rule about quality — a judgement about whether a truthful
        application can compete. Below this, the honest move is a different
        posting rather than a more imaginative resume.
        """
        return self.score >= 40

    def to_dict(self) -> dict:
        """Serialize for the dashboard."""
        return {
            "score": self.score,
            "worth_applying": self.worth_applying,
            "counts": {
                "matched": len(self.matched),
                "partial": len(self.partial),
                "missing": len(self.missing),
            },
            "matched": [m.to_dict() for m in self.matched],
            "partial": [m.to_dict() for m in self.partial],
            "missing": [m.to_dict() for m in self.missing],
        }


class FitAnalyser:
    """Compares a posting's requirements against a resume."""

    @staticmethod
    def run(job_text: str, resume_text: str) -> FitReport:
        """
        Build a per-requirement fit report.

        Args:
            job_text: The posting's description and requirements
            resume_text: The candidate's real resume

        Returns:
            A FitReport
        """
        requirements = FitAnalyser._requirements(job_text or "")
        sentences = FitAnalyser._sentences(resume_text or "")

        report = FitReport(
            matches=[
                FitAnalyser._assess(requirement, sentences)
                for requirement in requirements
            ]
        )

        logger.info(
            f"Fit report: {report.score}% — {len(report.matched)} matched, "
            f"{len(report.partial)} partial, {len(report.missing)} missing"
        )

        return report

    # ------------------------------------------------------------------
    # Reading the posting
    # ------------------------------------------------------------------

    @staticmethod
    def _requirements(job_text: str) -> List[str]:
        """
        Pull the lines that state what the job asks for.

        Postings put their requirements under a heading — "What you bring",
        "Requirements", "Qualifications" — and everything else is company
        boilerplate. Searching the whole text for the word "requirements"
        matched "we have built AI capabilities into the requirements for every
        role" and returned the marketing copy instead.

        Args:
            job_text: The posting text

        Returns:
            Requirement lines, deduplicated and capped
        """
        lines = [line.strip(" \t-•*·") for line in job_text.splitlines()]
        lines = [line for line in lines if line]

        # Anchored to the whole line: a heading is the only thing on its own
        # line. Matching a prefix meant "Benefits plans enrolment and
        # unenrollment" — an actual requirement — read as the start of the
        # compensation section and closed the list after one item.
        opens = re.compile(
            r"^(what you bring|what we.re looking for|requirements"
            r"|qualifications|who you are|about you|what you.ll need"
            r"|skills and experience)\s*:?$",
            re.IGNORECASE,
        )

        # Sections that follow the requirements and are not requirements.
        closes = re.compile(
            r"^(practicals|benefits|what.s next|how you.ll[^:]{0,40}"
            r"|application process|about remote|compensation|perks|our values"
            r"|equal opportunity)\s*:?$",
            re.IGNORECASE,
        )

        collected: List[str] = []
        inside = False

        for line in lines:
            if opens.match(line):
                inside = True
                continue

            if inside and closes.match(line):
                break

            if inside and FitAnalyser._is_requirement(line):
                collected.append(line)

        # No labelled section: fall back to every substantive line that is not
        # obviously company boilerplate.
        if len(collected) < 3:
            collected = [
                line for line in lines
                if FitAnalyser._is_requirement(line)
                and not BOILERPLATE.search(line)
            ]

        return list(dict.fromkeys(collected))[:MAX_REQUIREMENTS]

    @staticmethod
    def _is_requirement(line: str) -> bool:
        """
        Whether a line states something the candidate must be or have.

        Args:
            line: A line from the posting

        Returns:
            True if it reads as a requirement rather than a heading or notice
        """
        words = line.split()

        if not (MIN_REQUIREMENT_WORDS <= len(words) <= 45):
            return False

        if line.endswith(":"):
            return False

        return not BOILERPLATE.search(line)

    @staticmethod
    def _sentences(resume_text: str) -> List[str]:
        """Resume lines and sentences, as candidate evidence."""
        parts: List[str] = []

        for line in resume_text.splitlines():
            line = line.strip(" \t-•*·")

            if len(line.split()) >= 3:
                parts.extend(
                    piece.strip() for piece in re.split(r"(?<=[.;])\s+", line)
                    if len(piece.split()) >= 3
                )

        return parts

    # ------------------------------------------------------------------
    # Matching
    # ------------------------------------------------------------------

    @staticmethod
    def _terms(text: str) -> set:
        """Meaningful lowercase terms in a line."""
        # Split on slashes and dots so "Zendesk/e-mail" and "HR/Payroll" yield
        # the terms a resume would actually use, rather than one token that
        # matches nothing.
        cleaned = re.sub(r"[/\\]", " ", text.lower())
        words = re.findall(r"[A-Za-z][A-Za-z+#-]{2,}", cleaned)

        return {word.strip(".-") for word in words if word not in STOPWORDS}

    @staticmethod
    def _expand(terms: set) -> set:
        """Add the words that mean the same thing on a resume."""
        expanded = set(terms)

        for term in terms:
            expanded |= SYNONYMS.get(term, set())

            for key, group in SYNONYMS.items():
                if term in group:
                    expanded.add(key)
                    expanded |= group

        return expanded

    @staticmethod
    def _assess(requirement: str, sentences: List[str]) -> RequirementMatch:
        """
        Decide whether the resume evidences one requirement.

        Args:
            requirement: The line from the posting
            sentences: Candidate evidence from the resume

        Returns:
            A RequirementMatch carrying the evidence, when there is any
        """
        wanted = FitAnalyser._terms(requirement)

        if not wanted:
            return RequirementMatch(requirement=requirement, verdict="missing")

        best_sentence, best_overlap = None, set()

        for sentence in sentences:
            overlap = wanted & FitAnalyser._expand(FitAnalyser._terms(sentence))

            if len(overlap) > len(best_overlap):
                best_sentence, best_overlap = sentence, overlap

        share = len(best_overlap) / len(wanted)

        if share >= 0.5:
            verdict = "matched"
        elif share >= 0.25:
            verdict = "partial"
        else:
            return RequirementMatch(requirement=requirement, verdict="missing")

        return RequirementMatch(
            requirement=requirement,
            verdict=verdict,
            evidence=best_sentence,
            matched_terms=sorted(best_overlap),
        )


def analyse_fit(job_text: str, resume_text: str) -> FitReport:
    """
    Convenience wrapper around FitAnalyser.run.

    Args:
        job_text: Posting description and requirements
        resume_text: The candidate's resume

    Returns:
        A FitReport
    """
    return FitAnalyser.run(job_text, resume_text)
