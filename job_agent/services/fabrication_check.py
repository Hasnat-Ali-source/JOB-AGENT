"""
Fabrication Check (Phase 4).

A tailored resume must only reorder and rephrase what the master document
already says. An LLM asked to "tailor a resume to this job" will otherwise
happily invent a metric ("reduced latency by 40%"), a technology, or an
employer — and submitting that to an employer is fraud committed in the user's
name, from their email address.

This module compares a generated variant against its master and reports every
concrete claim that the master doesn't support. It is a safety net, not a
guarantee: it catches added facts of the kinds that are checkable in text
(numbers, dates, contact details, proper nouns), which is where fabrication
actually shows up.

Terms drawn from the job posting are allowed — a cover letter naturally names
the company and role it's addressed to.
"""

import logging
import re
from typing import Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

# Numbers, percentages, money, and multipliers: "40%", "$1.2M", "10x", "2019"
_NUMERIC_PATTERN = re.compile(r"\$?\d[\d,._]*\s?(?:%|[KMB]\b|x\b)?", re.IGNORECASE)

# Two or more consecutive capitalized words: "Stanford University", "Acme Corp".
# Only spaces and tabs join the words — matching across newlines would splice
# the last word of one line onto the first word of the next ("Acme Robotics" +
# "Backend engineer..." → a phantom entity that matches nothing).
# A word ending in a full stop ends the phrase. Without that, "…a Bachelor's
# foundation in Computer Science. Skilled in React…" yields the phantom entity
# "Computer Science. Skilled", which appears in no master document and was
# reported as an invented name — blocking release of an otherwise honest
# document over a sentence boundary.
_PROPER_NOUN_PATTERN = re.compile(
    r"\b([A-Z][a-zA-Z0-9&\-]*(?:\.[a-zA-Z0-9&\-]+)*"
    r"(?:[ \t]+[A-Z][a-zA-Z0-9&\-]*(?:\.[a-zA-Z0-9&\-]+)*)+)"
)

_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_URL_PATTERN = re.compile(r"(?:https?://|www\.)[^\s,;)]+")

# Capitalized words that start sentences or head sections — not entities
_COMMON_CAPITALIZED = {
    "i", "the", "a", "an", "my", "we", "our", "this", "that", "these", "those",
    "dear", "hiring", "manager", "team", "sincerely", "regards", "best",
    "thank", "you", "thanks", "please", "as", "at", "in", "on", "for", "with",
    "and", "or", "but", "to", "of", "from", "by", "is", "are", "was", "were",
    "experience", "education", "skills", "summary", "projects", "profile",
    "work", "professional", "technical", "senior", "junior", "lead", "staff",
    "engineer", "developer", "manager", "director", "designer", "analyst",
    "present", "current", "remote", "hybrid", "onsite",
    # Status markers a resume puts beside a date. They are not names anyone
    # could fabricate a credential out of — a phrase is only skipped when
    # every one of its words is in this set, so "Progress Software" is still
    # flagged — and leaving them out flags "In Progress" as an invented
    # organisation, which blocks releasing a perfectly honest document.
    "progress", "ongoing", "expected", "anticipated", "date", "in", "to",
}


class FabricationCheck:
    """Compares a generated variant against its master document."""

    @staticmethod
    def verify(
        master_text: str,
        variant_text: str,
        allowed_terms: Optional[Iterable[str]] = None,
    ) -> List[str]:
        """
        Report claims in the variant that the master doesn't support.

        Args:
            master_text: The user's master document — the source of truth
            variant_text: The generated variant
            allowed_terms: Extra permitted text (job title, company, posting
                text), so a cover letter may name the employer it addresses

        Returns:
            Human-readable flags, empty when nothing unsupported was found
        """
        master_haystack = FabricationCheck._normalize(master_text)
        allowed_haystack = FabricationCheck._normalize(" ".join(allowed_terms or []))
        haystack = f"{master_haystack} {allowed_haystack}"

        flags: List[str] = []

        # 1. Numeric claims — invented metrics are the classic failure
        for value in FabricationCheck._numbers(variant_text):
            if value not in FabricationCheck._numbers_in(haystack):
                flags.append(f"Number '{value}' does not appear in the master document")

        # 2. Named entities — invented employers, schools, certifications
        for entity in FabricationCheck._proper_nouns(variant_text):
            if FabricationCheck._normalize(entity) in haystack:
                continue

            # An exact-phrase test flags any *recombination* of the master's
            # own words: "Experienced Full-Stack Developer" is not an invented
            # organisation, it is the master's own job title with an adjective
            # in front, and tailoring is supposed to be allowed to do that.
            # What must still be caught is a name built from words the master
            # never uses at all — "Initech Corporation".
            words = re.findall(r"[A-Za-z][\w&-]{3,}", entity)

            supported = sum(
                1 for word in words
                if FabricationCheck._normalize(word) in haystack
            )

            # A strict majority, not merely one word. "Experienced Full-Stack
            # Developer" is three-quarters the master's own words and passes;
            # "SimpleX University" is half, and a degree the candidate does
            # not hold is exactly what this check exists to stop.
            if words and supported / len(words) > 0.5:
                continue

            flags.append(f"Name '{entity}' does not appear in the master document")

        # 3. Contact details must never be altered
        for email in set(_EMAIL_PATTERN.findall(variant_text)):
            if email.lower() not in haystack:
                flags.append(f"Email '{email}' does not appear in the master document")

        for url in set(_URL_PATTERN.findall(variant_text)):
            if FabricationCheck._normalize(url) not in haystack:
                flags.append(f"Link '{url}' does not appear in the master document")

        # Stable order, no duplicates
        deduped = sorted(set(flags))

        if deduped:
            logger.warning(
                f"Fabrication check found {len(deduped)} unsupported claim(s) "
                f"in a generated document"
            )

        return deduped

    @staticmethod
    def _normalize(text: str) -> str:
        """Lowercase and collapse whitespace and punctuation for matching."""
        text = text.lower()
        text = re.sub(r"[^\w\s%$.@/:-]", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _numbers(text: str) -> Set[str]:
        """Extract normalized numeric tokens from text."""
        found = set()

        for match in _NUMERIC_PATTERN.findall(text):
            token = match.strip().lower().replace(",", "").replace("$", "").rstrip(".")
            if token:
                found.add(token)

        return found

    @staticmethod
    def _numbers_in(haystack: str) -> Set[str]:
        """Numeric tokens present in the reference text."""
        return FabricationCheck._numbers(haystack)

    @staticmethod
    def _proper_nouns(text: str) -> Set[str]:
        """
        Extract multi-word capitalized phrases likely to be named entities.

        Phrases made entirely of common sentence-starting words are skipped, so
        "Dear Hiring Manager" isn't reported as an invented organization.
        """
        found = set()

        for match in _PROPER_NOUN_PATTERN.findall(text):
            words = match.split()

            if all(w.lower().strip(".,") in _COMMON_CAPITALIZED for w in words):
                continue

            # Drop sentence punctuation clinging to the last word: "Acme
            # Robotics." must still match the master's "Acme Robotics". An
            # abbreviation like "Acme Corp." matches either way, since the
            # comparison is a substring test.
            found.add(match.strip().rstrip(".,;:"))

        return found


def verify_no_fabrication(
    master_text: str,
    variant_text: str,
    allowed_terms: Optional[Iterable[str]] = None,
) -> List[str]:
    """
    Convenience wrapper around FabricationCheck.verify().

    Args:
        master_text: Master document text
        variant_text: Generated variant text
        allowed_terms: Additional permitted terms (e.g. job title, company)

    Returns:
        List of flags, empty when the variant is fully supported
    """
    return FabricationCheck.verify(master_text, variant_text, allowed_terms)
