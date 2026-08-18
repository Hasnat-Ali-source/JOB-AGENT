"""
Carrying an answer from one application to the next.

**The failure this closes.** Every application in the tray asks the same
fifteen questions — country, notice period, sponsorship, salary expectation,
how you heard about us. The user answers them on one, and the others still
show them blank. The answers *were* being saved: `submit_answers` writes them
to `CandidateProfile.remembered_answers`, and `FieldClassifier` reads that back
when it fills a form. But an application already sitting in the tray was
filled *before* the answer existed, and nothing ever went back for it. So the
saving worked and the reuse never happened, which from the tray is
indistinguishable from the answers not being saved at all.

This module is the missing step: after an answer is recorded, and again after
an application is submitted, every other application still waiting is offered
the answers the user has given.

**What is not carried, and why.** Matching is on the normalised question text,
which keeps the employer's name in it. "Have you previously worked at or
consulted for GitLab?" and "How did you hear about Remote?" therefore only
ever match the same employer's own forms — a company-specific answer cannot
leak onto a different company's application. Only blank fields are filled: an
answer the user has already given on that application is never overwritten.
Sensitive fields are carried only if the user chose to remember them, which is
the same choice they already made when saving.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from job_agent.models.database import Application, ApplicationStatus, CandidateProfile
from job_agent.services.field_classifier import FieldClassifier
from job_agent.utils.dates import utcnow

logger = logging.getLogger(__name__)

# Applications still waiting on the user. A submitted, rejected or withdrawn
# application is a record of what was sent, and back-filling it would rewrite
# history.
OPEN_STATUSES = (
    ApplicationStatus.DRAFT,
    ApplicationStatus.QUEUED_FOR_REVIEW,
)


@dataclass
class CarryResult:
    """What carrying the answers changed."""

    applications_updated: int = 0
    answers_filled: int = 0
    # Application id -> the questions it just had answered for it, so the
    # dashboard can say which other applications moved rather than only that
    # some did.
    by_application: Dict[int, List[str]] = field(default_factory=dict)

    @property
    def changed_anything(self) -> bool:
        """True when at least one waiting application gained an answer."""
        return self.answers_filled > 0

    def describe(self) -> str:
        """One sentence for the dashboard, empty when nothing moved."""
        if not self.changed_anything:
            return ""

        answers = (
            "answer" if self.answers_filled == 1 else "answers"
        )
        applications = (
            "application" if self.applications_updated == 1 else "applications"
        )

        return (
            f"Also filled {self.answers_filled} {answers} on "
            f"{self.applications_updated} other {applications} in the tray."
        )

    def to_dict(self) -> dict:
        """Serialize for the dashboard."""
        return {
            "applications_updated": self.applications_updated,
            "answers_filled": self.answers_filled,
            "by_application": {
                str(app_id): questions
                for app_id, questions in self.by_application.items()
            },
            "message": self.describe(),
        }


def carry_answers_forward(
    session: Session,
    profile: Optional[CandidateProfile],
    exclude_application_id: Optional[int] = None,
) -> CarryResult:
    """
    Offer every answer the user has saved to the applications still waiting.

    Args:
        session: Database session. Committed by the caller, not here — this
            runs inside the request that recorded the answer, and the two
            must land together or not at all.
        profile: The candidate profile holding the saved answers
        exclude_application_id: The application the user is working on, which
            has already had these answers written to it directly

    Returns:
        What changed, ready to report back to the user
    """
    result = CarryResult()

    remembered = dict(getattr(profile, "remembered_answers", None) or {})

    if not remembered:
        return result

    waiting = (
        session.query(Application)
        .filter(Application.submission_status.in_(OPEN_STATUSES))
        .all()
    )

    for application in waiting:
        if application.id == exclude_application_id:
            continue

        filled_here = _fill_one(application, remembered)

        if not filled_here:
            continue

        result.applications_updated += 1
        result.answers_filled += len(filled_here)
        result.by_application[application.id] = filled_here

    if result.changed_anything:
        logger.info(
            f"Carried {result.answers_filled} saved answer(s) onto "
            f"{result.applications_updated} waiting application(s)"
        )

    return result


def _fill_one(application: Application, remembered: dict) -> List[str]:
    """
    Fill one application's blank deferred questions from the saved answers.

    Args:
        application: The application, mutated in place when anything matches
        remembered: Saved answers, keyed by normalised question

    Returns:
        The questions that were just answered, empty if none matched
    """
    deferred = dict(application.deferred_fields or {})
    answered: List[str] = []

    for question, detail in deferred.items():
        # Never overwrite what the user said about this application.
        if (detail.get("value_entered_by_user") or "") != "":
            continue

        saved = remembered.get(FieldClassifier.remember_key(question))

        if saved in (None, ""):
            continue

        value = _fits_the_field(saved, detail.get("options"))

        # The same question on another employer's form can offer a different
        # set of choices. Writing an answer that is not on this form's menu
        # would be caught later — by the analyst, or by the form itself — but
        # only after the user had been told the question was answered.
        if value is None:
            continue

        deferred[question] = {
            **detail,
            "value_entered_by_user": value,
            "carried_from_an_earlier_answer": True,
        }
        answered.append(question)

    if not answered:
        return []

    # SQLAlchemy tracks JSON columns by identity, so the dict must be
    # reassigned rather than mutated in place.
    application.deferred_fields = deferred
    application.updated_at = utcnow()

    return answered


def _fits_the_field(saved: str, options: Optional[List[str]]) -> Optional[str]:
    """
    The value to write, given what this particular form will accept.

    Args:
        saved: The answer the user gave on an earlier form
        options: The choices this field offers, if it is a dropdown

    Returns:
        The value to write — the form's own spelling of the option where it
        offers a menu — or None when the answer is not on that menu
    """
    if not options:
        return saved

    wanted = str(saved).strip().lower()

    for option in options:
        if str(option).strip().lower() == wanted:
            # The form's own casing, not the user's: a select is matched by
            # its option text, and "yes" is not "Yes".
            return str(option)

    return None
