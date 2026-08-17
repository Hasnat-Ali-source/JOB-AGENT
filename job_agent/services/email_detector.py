"""
Email Application Detector (Phase 6b).

Finds the address a job posting asks applications to be sent to.

Getting this wrong is not a small error: the agent would email a stranger — a
support desk, a press contact, a random address in a footer — with the user's
resume attached, from the user's own address. So detection is conservative and
always reports *why* it chose an address, which the user sees before approving.

Addresses are scored by their surroundings. "Send your CV to jobs@acme.com"
scores highly; an address in a privacy notice scores negative and is rejected.
"""

import logging
import re
from dataclasses import dataclass
from typing import List, Optional

logger = logging.getLogger(__name__)

EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+[\w]")

# Wording near an address that suggests applications go there
APPLY_CUES = [
    (re.compile(r"send\s+(your\s+)?(cv|r[ée]sum[ée]|application|details)", re.I), 5),
    (re.compile(r"(e-?mail|write|apply)\s+(to|us at|your)", re.I), 5),
    (re.compile(r"applications?\s+(to|should be sent)", re.I), 5),
    (re.compile(r"\bapply\b", re.I), 3),
    (re.compile(r"r[ée]sum[ée]|\bcv\b", re.I), 3),
    (re.compile(r"interested\??\s*(please\s*)?(contact|reach)", re.I), 3),
    (re.compile(r"\bcontact\b", re.I), 1),
]

# Local parts that are almost never an application inbox
NEGATIVE_LOCAL_PARTS = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "postmaster",
    "abuse", "unsubscribe", "privacy", "legal", "dpo", "gdpr", "security",
    "webmaster", "marketing", "sales", "billing", "invoices", "press",
    "media", "investor", "investors", "support", "help", "helpdesk",
}

# Local parts that strongly suggest an application inbox
POSITIVE_LOCAL_PARTS = {
    "jobs", "job", "careers", "career", "hiring", "recruiting", "recruitment",
    "recruiter", "talent", "apply", "applications", "hr", "people", "cv",
    "resumes", "resume", "join", "work",
}

# Wording near an address that means it is *not* for applications
NEGATIVE_CUES = [
    re.compile(r"privacy|unsubscribe|terms of (use|service)|cookie", re.I),
    re.compile(r"do not (contact|email|reply)", re.I),
    re.compile(r"(technical )?support (queries|questions|requests)", re.I),
]

CONTEXT_WINDOW = 120  # Characters either side of an address to examine


@dataclass
class RecipientCandidate:
    """A possible application address with the evidence for it."""

    email: str
    score: int
    context: str
    reasons: List[str]

    @property
    def is_confident(self) -> bool:
        """True when the evidence is strong enough to propose this address."""
        return self.score >= 3


class EmailApplicationDetector:
    """Finds where a posting asks applications to be sent."""

    @staticmethod
    def detect(job) -> Optional[RecipientCandidate]:
        """
        Find the best application address in a job posting.

        Args:
            job: Job record (description, requirements, recruiter_contact)

        Returns:
            The best candidate, or None when nothing is convincing
        """
        # An address already captured from structured data wins outright
        explicit = getattr(job, "recruiter_contact", None)
        if explicit and EMAIL_PATTERN.fullmatch(explicit.strip()):
            return RecipientCandidate(
                email=explicit.strip().lower(),
                score=10,
                context="",
                reasons=["Listed as the posting's application contact"],
            )

        text = " ".join(
            str(getattr(job, attribute, "") or "")
            for attribute in ("description", "requirements")
        )

        candidates = EmailApplicationDetector.rank(text)

        if not candidates:
            return None

        best = candidates[0]

        if not best.is_confident:
            logger.info(
                f"Found {best.email} in the posting but the surrounding text does not "
                f"suggest it takes applications (score {best.score}) — not proposing it"
            )
            return None

        return best

    @staticmethod
    def rank(text: str) -> List[RecipientCandidate]:
        """
        Score every address in a block of text.

        Args:
            text: Posting text

        Returns:
            Candidates, best first
        """
        if not text:
            return []

        candidates: List[RecipientCandidate] = []
        seen = set()

        for match in EMAIL_PATTERN.finditer(text):
            email = match.group(0).lower().rstrip(".")

            if email in seen:
                continue
            seen.add(email)

            context = EmailApplicationDetector._context_for(text, match)

            score, reasons = EmailApplicationDetector._score(email, context)

            candidates.append(
                RecipientCandidate(
                    email=email, score=score, context=context, reasons=reasons
                )
            )

        return sorted(candidates, key=lambda c: c.score, reverse=True)

    @staticmethod
    def _context_for(text: str, match) -> str:
        """
        Extract the text surrounding one address, without crossing into another.

        Postings often list several addresses close together ("apply to
        careers@…  Unsubscribe: no-reply@…"). A fixed window would pull the
        neighbouring address's wording in, so a good address inherits a bad
        one's penalty and the user is shown contradictory reasons for the same
        pick. The window is therefore clipped at any other address.

        Args:
            text: Full posting text
            match: The regex match for this address

        Returns:
            Cleaned context string
        """
        start = max(0, match.start() - CONTEXT_WINDOW)
        end = min(len(text), match.end() + CONTEXT_WINDOW)

        for other in EMAIL_PATTERN.finditer(text):
            if other.start() == match.start():
                continue

            # A neighbour before this address moves the left edge in
            if other.end() <= match.start():
                start = max(start, other.end())
            # A neighbour after it moves the right edge in
            elif other.start() >= match.end():
                end = min(end, other.start())

        # Stop at the end of this address's own sentence. Clipping at the next
        # address alone still lets its introducing label through — in
        # "...send your CV to careers@x. Unsubscribe: no-reply@x", the word
        # "Unsubscribe" would otherwise penalize the correct address.
        sentence_end = re.search(r"[.!?;\n]", text[match.end():end])
        if sentence_end:
            end = match.end() + sentence_end.start() + 1

        return " ".join(text[start:end].split())

    @staticmethod
    def _score(email: str, context: str) -> tuple:
        """
        Score one address from its local part and surrounding text.

        Args:
            email: The address
            context: Text around it

        Returns:
            (score, reasons)
        """
        local_part = email.split("@")[0].lower()

        # Match three ways, because separators are used inconsistently:
        # the whole local part ("no-reply"), the separator-stripped form
        # ("noreply"), and each token ("careers" from "careers-emea").
        # Tokens alone would miss "no-reply", whose parts are both innocuous.
        tokens = set(re.split(r"[.\-_+]", local_part))
        forms = tokens | {local_part, re.sub(r"[.\-_+]", "", local_part)}

        score = 0
        reasons: List[str] = []

        if forms & POSITIVE_LOCAL_PARTS:
            score += 6
            reasons.append(f"'{local_part}' reads as an application inbox")

        if forms & NEGATIVE_LOCAL_PARTS:
            score -= 8
            reasons.append(f"'{local_part}' is not an application inbox")

        for pattern, weight in APPLY_CUES:
            if pattern.search(context):
                score += weight
                reasons.append(f"Nearby text matches '{pattern.pattern}'")
                break  # One contextual cue is enough; don't stack them

        for pattern in NEGATIVE_CUES:
            if pattern.search(context):
                score -= 6
                reasons.append("Nearby text suggests this address is for something else")
                break

        return score, reasons


def detect_application_email(job) -> Optional[RecipientCandidate]:
    """
    Convenience wrapper around EmailApplicationDetector.detect().

    Args:
        job: Job record

    Returns:
        Best candidate, or None
    """
    return EmailApplicationDetector.detect(job)
