"""
Form Field Classifier (Phase 5).

Decides, for each field on an application form, whether the agent may answer it
from the candidate profile or must hand it to the user.

Four outcomes:

- **KNOWN** — confidently mapped to a profile value (name, email, phone, links)
- **REMEMBERED** — the user answered this same question on an earlier form
- **SENSITIVE** — must be answered by the user, every time, without exception
- **UNKNOWN** — no confident mapping, so ask rather than guess

The SENSITIVE category is not a confidence judgement, and having a plausible
value available never overrides it:

- *Demographics, disability, veteran status* are voluntary self-identification
  under EEO rules. An agent selecting an answer would be fabricating
  protected-characteristic data on a legal document, and the user may have good
  reasons to decline to answer at all.
- *Compensation history* is unlawful for employers to ask in several US states,
  and a wrong answer permanently damages the user's negotiating position.
- *Criminal history, citizenship, age, and health* carry consequences the agent
  cannot weigh.

A field matching a sensitive pattern is deferred even if the profile happens to
hold something that would fit.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Pattern

from job_agent.models.database import FieldCategory

logger = logging.getLogger(__name__)


@dataclass
class FormField:
    """One input on an application form."""

    selector: str  # CSS selector to target it
    tag: str  # "input", "select", "textarea"
    field_type: str  # "text", "email", "file", "checkbox", "radio", "select", ...
    name: str = ""
    field_id: str = ""
    label: str = ""
    placeholder: str = ""
    aria_label: str = ""
    required: bool = False
    options: List[str] = None  # For select / radio groups
    answered: bool = False  # The form already holds an answer for this
    current: str = ""  # What that answer is, as the user reads it

    def __post_init__(self):
        if self.options is None:
            self.options = []

    @property
    def haystack(self) -> str:
        """
        All text describing this field, lowercased and word-separated.

        Half of what identifies a field is written in machine style — an id of
        `cover_letter`, a name of `workAuthorization` — while the patterns that
        match it are written the way a person says it. Folding both into spaced
        words is what lets `cover letter` find `cover_letter`; without it that
        upload was classified as an unmappable question, the tailored letter
        was never attached, and the form went out with the field blank.
        """
        text = " ".join(
            part
            for part in (self.label, self.name, self.field_id,
                         self.placeholder, self.aria_label)
            if part
        )

        # camelCase is split before lowercasing, while the boundary is visible
        text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text).lower()

        return re.sub(r"\s+", " ", re.sub(r"[_\-.]+", " ", text)).strip()

    @property
    def question(self) -> str:
        """
        The best human-readable description of this field.

        A CSS selector is the last resort and a bad one: "div >
        input:nth-of-type(1)" tells the user nothing about what the form is
        asking, and they cannot answer a question they cannot read. An
        attribute name at least carries the site's own word for the field, so
        it is humanised and preferred over the selector.
        """
        for candidate in (self.label, self.aria_label, self.placeholder):
            text = (candidate or "").strip()

            if not text:
                continue

            # "Attach" is the label on both the resume and the cover letter
            # upload, so on its own it names neither. The field's own id says
            # which is which, and two questions a user cannot tell apart are
            # worse than one they can.
            if text.lower() in GENERIC_LABELS:
                qualifier = self._humanise(self.field_id or self.name)

                if qualifier:
                    return f"{text} {qualifier.lower()}"

            return text

        for attribute in (self.name, self.field_id):
            humanised = self._humanise(attribute)
            if humanised:
                return humanised

        return self.selector

    @staticmethod
    def _humanise(attribute: str) -> str:
        """
        Turn a form field's attribute name into something readable.

        Args:
            attribute: An id or name like "cover_letter" or "workAuthorization"

        Returns:
            "Cover letter", "Work authorization", or "" if there was nothing
            usable — a generated id such as "input-42" is no better than the
            selector it would replace.
        """
        if not attribute or not attribute.strip():
            return ""

        text = re.sub(r"[_\-]+", " ", attribute.strip())
        text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

        # Framework-generated handles ("input 42", "field 7", a bare number)
        # are noise dressed up as a label.
        if not text or not re.search(r"[a-zA-Z]{3}", text):
            return ""

        if re.fullmatch(r"(input|field|text|select|control)\s*\d*", text, re.I):
            return ""

        return text[:1].upper() + text[1:]


@dataclass
class FieldClassification:
    """What to do with a form field."""

    category: FieldCategory
    profile_key: Optional[str] = None  # Attribute on CandidateProfile
    value: Optional[str] = None  # Resolved value, when one is available
    reason: str = ""  # Shown to the user in the review queue

    @property
    def is_fillable(self) -> bool:
        """True when the agent may fill this without asking."""
        return (
            self.category in (FieldCategory.KNOWN, FieldCategory.REMEMBERED)
            and self.value not in (None, "")
        )


# Labels that describe the control rather than the question. A form may use
# the same one for several fields, so on their own they identify nothing.
GENERIC_LABELS = {
    "attach", "upload", "choose file", "browse", "select file",
    "add file", "select", "choose",
}


def _patterns(*fragments: str) -> List[Pattern]:
    """Compile case-insensitive patterns from plain fragments."""
    return [re.compile(fragment, re.IGNORECASE) for fragment in fragments]


class FieldClassifier:
    """Maps form fields to profile values, or to the user."""

    # --- Never auto-answered, whatever the profile contains -----------------
    SENSITIVE_PATTERNS: Dict[str, List[Pattern]] = {
        "demographics": _patterns(
            r"\brace\b", r"\bethnic", r"\bgender\b", r"\bsex\b",
            r"hispanic", r"latino", r"national origin", r"\bdiversity\b",
            r"self.?identif",
            # Pronouns are identity, in the same family as gender. Left out,
            # the question fell through to the answer drafter, which read it
            # as a grammar question and produced "use first-person singular
            # pronouns (I, me, my)" — nonsense on a form, and not the agent's
            # to answer in the first place.
            r"\bpronoun",
            r"\blgbt", r"\blesbian\b", r"\btransgender\b", r"\bsexual orientation\b",
        ),
        "disability": _patterns(
            r"disab", r"\bada\b", r"accommodat", r"impairment",
        ),
        "veteran": _patterns(
            r"veteran", r"\bmilitary\b", r"uniformed service", r"armed forces",
        ),
        "compensation history": _patterns(
            r"current (salary|compensation|pay|ctc)",
            r"salary history", r"compensation history",
            r"previous (salary|compensation)", r"last drawn",
        ),
        "compensation expectation": _patterns(
            r"(expected|desired|target) (salary|compensation|pay|ctc)",
            r"salary (expectation|requirement)", r"compensation expectation",
        ),
        "criminal history": _patterns(
            r"convict", r"criminal", r"felony", r"background check consent",
        ),
        "citizenship or age": _patterns(
            r"citizenship", r"date of birth", r"\bdob\b", r"\bage\b",
            r"marital status", r"religio",
        ),
    }

    # --- Safe to fill from the profile --------------------------------------
    KNOWN_PATTERNS: List[tuple] = [
        # (profile attribute, patterns) — order matters, first match wins
        ("email", _patterns(r"e.?mail")),
        ("phone", _patterns(r"phone", r"mobile", r"telephone", r"\bcell\b")),
        ("linkedin_url", _patterns(r"linked.?in")),
        ("github_url", _patterns(r"github", r"\bgit\b")),
        ("portfolio_url", _patterns(r"portfolio", r"dribbble", r"behance")),
        ("website_url", _patterns(r"website", r"personal site", r"\bblog\b", r"\burl\b")),
        ("location", _patterns(r"location", r"\bcity\b", r"where.*based", r"address")),
        ("full_name", _patterns(r"full name", r"your name", r"^name$", r"\bname\b")),
        ("years_experience", _patterns(r"years of experience", r"years.*experience")),
        ("notice_period", _patterns(r"notice period", r"availability to start", r"start date")),
        ("work_authorization", _patterns(
            r"work authoriz", r"authorized to work", r"right to work", r"visa status",
        )),
        ("requires_sponsorship", _patterns(r"sponsor")),
        ("willing_to_relocate", _patterns(r"relocat")),
    ]

    # Profile values that are the user's own details rather than an answer to
    # a question. None of these is ever one of a form's offered choices, so a
    # chooser matching one of their patterns has been misread.
    TEXT_ONLY_ATTRIBUTES = frozenset({
        "email", "phone", "full_name", "first_name", "last_name",
        "linkedin_url", "github_url", "portfolio_url", "website_url",
    })

    # Document uploads are handled separately from profile text
    RESUME_PATTERNS = _patterns(r"resume", r"\bcv\b", r"curriculum vitae")
    COVER_LETTER_PATTERNS = _patterns(r"cover letter", r"covering letter", r"motivation")

    # Name sub-fields, when a form splits them
    FIRST_NAME_PATTERNS = _patterns(r"first name", r"given name", r"forename")
    LAST_NAME_PATTERNS = _patterns(r"last name", r"surname", r"family name")

    def __init__(self, profile: Any, remembered: Optional[Dict[str, str]] = None):
        """
        Initialize the classifier.

        Args:
            profile: CandidateProfile supplying values
            remembered: Previously user-supplied answers, keyed by question
        """
        self.profile = profile
        self.remembered = remembered or {}

    def classify(self, field: FormField) -> FieldClassification:
        """
        Decide how to handle one form field.

        Args:
            field: The field to classify

        Returns:
            FieldClassification
        """
        haystack = field.haystack

        # 1. Sensitive checks run first and are never overridden
        sensitive_reason = self._match_sensitive(haystack)
        if sensitive_reason:
            # The one exception is the user's own answer to this same
            # question, saved deliberately on an earlier application. Reusing
            # it is not the agent deciding — it is the agent not asking twice.
            saved = self.remembered.get(self.remember_key(field.question))

            if saved not in (None, ""):
                return FieldClassification(
                    category=FieldCategory.REMEMBERED,
                    value=str(saved),
                    reason=(
                        f"{sensitive_reason} — filled with the answer you saved "
                        f"for this question. Change it here if it's wrong."
                    ),
                )

            return FieldClassification(
                category=FieldCategory.SENSITIVE,
                reason=(
                    f"{sensitive_reason} — the agent never answers this. "
                    f"Answer it yourself, or leave it blank if it's optional."
                ),
            )

        # 2. The user's own answer to this very question.
        #
        # Ahead of the profile mappings, because those match on wording that
        # merely *mentions* a profile concept. An employer's SMS-consent
        # question ends "...at the mobile number provided?", which the phone
        # pattern matched, so a Yes/No consent question was answered with the
        # user's phone number — and the answer they had given for that exact
        # question, sitting in the bank, was never reached. A saved answer is
        # the user's own words about this question; a pattern match is a guess
        # about what the question is. The guess does not outrank them.
        remembered_key = self.remember_key(field.question)
        if remembered_key in self.remembered:
            return FieldClassification(
                category=FieldCategory.REMEMBERED,
                value=str(self.remembered[remembered_key]),
                reason="You answered this question on an earlier application",
            )

        # 3. Document uploads
        if self._matches(haystack, self.RESUME_PATTERNS):
            return FieldClassification(
                category=FieldCategory.KNOWN,
                profile_key="resume",
                reason="Tailored resume from this application's document package",
            )

        if self._matches(haystack, self.COVER_LETTER_PATTERNS):
            return FieldClassification(
                category=FieldCategory.KNOWN,
                profile_key="cover_letter",
                reason="Tailored cover letter from this application's document package",
            )

        # 4. Split name fields
        if self._matches(haystack, self.FIRST_NAME_PATTERNS):
            return self._from_profile("first_name", "First name from your profile")

        if self._matches(haystack, self.LAST_NAME_PATTERNS):
            return self._from_profile("last_name", "Last name from your profile")

        # 5. Straight profile mappings.
        #
        # An identity value never answers a chooser. "Do you consent to texts
        # at the mobile number provided?" matches the phone pattern, and its
        # answers are Yes and No — filling the user's phone number in there is
        # not a near miss, it is answering a question the agent misread. But
        # plenty of profile values *are* legitimate choices: work authorization
        # onto a Yes/No dropdown is exactly right, and `_closest_option` at
        # fill time refuses anything the list does not offer.
        chooser = self._is_a_chooser(field)

        for attribute, patterns in self.KNOWN_PATTERNS:
            if chooser and attribute in self.TEXT_ONLY_ATTRIBUTES:
                continue

            if self._matches(haystack, patterns):
                return self._from_profile(
                    attribute, f"'{attribute.replace('_', ' ')}' from your profile"
                )

        # 6. Anything else
        return FieldClassification(
            category=FieldCategory.UNKNOWN,
            reason="The agent could not map this question to your profile",
        )

    @staticmethod
    def _is_a_chooser(field: FormField) -> bool:
        """
        Whether this field picks from a fixed set rather than taking text.

        Args:
            field: The field

        Returns:
            True for radio groups, checkboxes and anything offering options
        """
        return (
            field.field_type in ("radio", "checkbox")
            or bool(field.options)
        )

    def _from_profile(self, attribute: str, reason: str) -> FieldClassification:
        """
        Build a classification from a profile attribute.

        A mapped field whose profile value is empty becomes UNKNOWN rather than
        being filled with a blank — the user is asked instead.
        """
        value = self._profile_value(attribute)

        if value in (None, ""):
            return FieldClassification(
                category=FieldCategory.UNKNOWN,
                profile_key=attribute,
                reason=(
                    f"Your profile has no '{attribute.replace('_', ' ')}' — "
                    f"add it to your profile or answer here"
                ),
            )

        return FieldClassification(
            category=FieldCategory.KNOWN,
            profile_key=attribute,
            value=value,
            reason=reason,
        )

    def _profile_value(self, attribute: str) -> Optional[str]:
        """
        Read a value off the candidate profile.

        Args:
            attribute: Profile attribute, or the synthetic "first_name" /
                "last_name" split of full_name

        Returns:
            String value, or None
        """
        if attribute in ("first_name", "last_name"):
            full_name = (getattr(self.profile, "full_name", "") or "").strip()
            if not full_name:
                return None
            parts = full_name.split()
            if attribute == "first_name":
                return parts[0]
            return " ".join(parts[1:]) if len(parts) > 1 else None

        value = getattr(self.profile, attribute, None)

        if value is None:
            return None

        if isinstance(value, bool):
            return "Yes" if value else "No"

        return str(value)

    @staticmethod
    def _matches(haystack: str, patterns: List[Pattern]) -> bool:
        """True if any pattern is found in the field's text."""
        return any(pattern.search(haystack) for pattern in patterns)

    @classmethod
    def _match_sensitive(cls, haystack: str) -> Optional[str]:
        """
        Return the sensitive category matched, or None.

        Args:
            haystack: The field's combined text

        Returns:
            Category name (e.g. "veteran"), or None
        """
        for category, patterns in cls.SENSITIVE_PATTERNS.items():
            if cls._matches(haystack, patterns):
                return f"Looks like a {category} question"

        return None

    @staticmethod
    def remember_key(question: str) -> str:
        """
        Normalize a question so the same one is recognized across forms.

        Args:
            question: The field's human-readable question

        Returns:
            A normalized key
        """
        key = re.sub(r"[^\w\s]", " ", question.lower())
        key = re.sub(r"\s+", " ", key).strip()
        return key[:120]

    @staticmethod
    def is_sensitive(text: str) -> bool:
        """
        Check whether arbitrary text reads as a sensitive question.

        Exposed so callers (and tests) can check a question without building a
        FormField.

        Args:
            text: Question text

        Returns:
            True if it matches any sensitive pattern
        """
        return FieldClassifier._match_sensitive(text.lower()) is not None
