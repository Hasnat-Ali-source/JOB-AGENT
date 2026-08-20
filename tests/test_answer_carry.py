"""
An answer given once must appear on the other applications waiting.

The bug this covers: answers *were* saved to the profile and *were* read back
when a form was filled — but an application already sitting in the tray had
been filled before the answer existed, and nothing went back for it. From the
tray, answers that save but never reappear are indistinguishable from answers
that were never saved.
"""

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from job_agent.models.database import (
    Application,
    ApplicationStatus,
    CandidateProfile,
)
from job_agent.services.answer_carry import carry_answers_forward
from job_agent.services.field_classifier import FieldClassifier


@pytest.fixture
def session():
    """In-memory database."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)

    with Session(engine) as db_session:
        yield db_session


def _deferred(*questions, answered=None):
    """Build a deferred_fields dict for a set of questions."""
    answered = answered or {}

    return {
        question: {
            "required": True,
            "category": "unknown",
            "reason": "The agent could not map this question",
            "value_entered_by_user": answered.get(question, ""),
        }
        for question in questions
    }


@pytest.fixture
def profile(session):
    """A profile carrying answers the user gave on an earlier application."""
    record = CandidateProfile(
        full_name="Hasnat Tahir",
        email="hasnat@example.com",
        remembered_answers={
            FieldClassifier.remember_key("Country"): "Malaysia",
            FieldClassifier.remember_key("Notice period"): "One month",
            FieldClassifier.remember_key(
                "Have you previously worked at or consulted for GitLab?"
            ): "No",
        },
    )
    session.add(record)
    session.commit()
    session.refresh(record)

    return record


def _application(session, job_id, questions, status=ApplicationStatus.QUEUED_FOR_REVIEW):
    record = Application(
        job_id=job_id,
        platform_account_id=1,
        submission_status=status,
        filled_fields={},
        deferred_fields=questions,
    )
    session.add(record)
    session.commit()
    session.refresh(record)

    return record


class TestCarryingForward:
    """What a saved answer reaches, and what it does not."""

    def test_a_waiting_application_gets_the_saved_answer(self, session, profile):
        waiting = _application(session, 1, _deferred("Country", "Notice period"))

        result = carry_answers_forward(session, profile)
        session.commit()
        session.refresh(waiting)

        assert result.answers_filled == 2
        assert result.applications_updated == 1
        assert waiting.deferred_fields["Country"]["value_entered_by_user"] == "Malaysia"
        assert (
            waiting.deferred_fields["Notice period"]["value_entered_by_user"]
            == "One month"
        )

    def test_a_carried_answer_is_marked_as_carried(self, session, profile):
        """The user should see it is theirs, and from where."""
        waiting = _application(session, 1, _deferred("Country"))

        carry_answers_forward(session, profile)
        session.commit()
        session.refresh(waiting)

        assert waiting.deferred_fields["Country"]["carried_from_an_earlier_answer"]

    def test_an_answer_already_given_is_never_overwritten(self, session, profile):
        """What the user said about *this* application wins."""
        waiting = _application(
            session,
            1,
            _deferred("Country", answered={"Country": "Singapore"}),
        )

        result = carry_answers_forward(session, profile)
        session.commit()
        session.refresh(waiting)

        assert result.answers_filled == 0
        assert waiting.deferred_fields["Country"]["value_entered_by_user"] == "Singapore"

    def test_the_application_being_worked_on_is_skipped(self, session, profile):
        source = _application(session, 1, _deferred("Country"))
        other = _application(session, 2, _deferred("Country"))

        result = carry_answers_forward(
            session, profile, exclude_application_id=source.id
        )
        session.commit()

        assert result.applications_updated == 1
        assert other.id in result.by_application
        assert source.id not in result.by_application

    def test_a_submitted_application_is_left_alone(self, session, profile):
        """It is a record of what was sent; back-filling would rewrite it."""
        sent = _application(
            session, 1, _deferred("Country"), status=ApplicationStatus.SUBMITTED
        )

        carry_answers_forward(session, profile)
        session.commit()
        session.refresh(sent)

        assert sent.deferred_fields["Country"]["value_entered_by_user"] == ""

    def test_a_company_specific_answer_does_not_reach_another_company(
        self, session, profile
    ):
        """
        The safety property the whole feature rests on. "Have you worked at
        GitLab?" and "Have you worked at Remote?" normalise to different keys
        because the employer's name is part of the question, so an answer
        about one employer can never be submitted to another.
        """
        elsewhere = _application(
            session,
            1,
            _deferred("Have you previously worked at or consulted for Remote?"),
        )

        result = carry_answers_forward(session, profile)
        session.commit()
        session.refresh(elsewhere)

        assert result.answers_filled == 0
        assert (
            elsewhere.deferred_fields[
                "Have you previously worked at or consulted for Remote?"
            ]["value_entered_by_user"]
            == ""
        )

    def test_the_same_company_question_does_carry(self, session, profile):
        same = _application(
            session,
            1,
            _deferred("Have you previously worked at or consulted for GitLab?"),
        )

        carry_answers_forward(session, profile)
        session.commit()
        session.refresh(same)

        assert (
            same.deferred_fields[
                "Have you previously worked at or consulted for GitLab?"
            ]["value_entered_by_user"]
            == "No"
        )

    def test_no_profile_is_not_an_error(self, session):
        assert carry_answers_forward(session, None).answers_filled == 0

    def test_nothing_saved_is_not_an_error(self, session):
        empty = CandidateProfile(full_name="A", email="a@b.c", remembered_answers={})
        session.add(empty)
        session.commit()

        assert carry_answers_forward(session, empty).answers_filled == 0

    def test_it_says_what_it_did(self, session, profile):
        _application(session, 1, _deferred("Country"))
        _application(session, 2, _deferred("Country", "Notice period"))

        result = carry_answers_forward(session, profile)

        assert "3 answers on 2 other applications" in result.describe()


class TestFieldsWithAMenu:
    """The same question offers different choices on different forms."""

    def test_an_answer_not_on_this_form_s_menu_is_not_carried(self, session, profile):
        """
        Writing it anyway would report the question as answered and fail at
        the form — after the user had been told it was handled.
        """
        waiting = _application(session, 1, _deferred("Country"))
        fields = dict(waiting.deferred_fields)
        fields["Country"] = {**fields["Country"], "options": ["Ireland", "Portugal"]}
        waiting.deferred_fields = fields
        session.commit()

        result = carry_answers_forward(session, profile)
        session.commit()
        session.refresh(waiting)

        assert result.answers_filled == 0
        assert waiting.deferred_fields["Country"]["value_entered_by_user"] == ""

    def test_an_answer_on_the_menu_is_carried(self, session, profile):
        waiting = _application(session, 1, _deferred("Country"))
        fields = dict(waiting.deferred_fields)
        fields["Country"] = {**fields["Country"], "options": ["Malaysia", "Portugal"]}
        waiting.deferred_fields = fields
        session.commit()

        carry_answers_forward(session, profile)
        session.commit()
        session.refresh(waiting)

        assert waiting.deferred_fields["Country"]["value_entered_by_user"] == "Malaysia"

    def test_the_form_s_own_spelling_wins(self, session, profile):
        """A select is matched by its option text, and "malaysia" is not "Malaysia"."""
        profile.remembered_answers = {
            **profile.remembered_answers,
            FieldClassifier.remember_key("Country"): "malaysia",
        }
        session.commit()

        waiting = _application(session, 1, _deferred("Country"))
        fields = dict(waiting.deferred_fields)
        fields["Country"] = {**fields["Country"], "options": ["Malaysia"]}
        waiting.deferred_fields = fields
        session.commit()

        carry_answers_forward(session, profile)
        session.commit()
        session.refresh(waiting)

        assert waiting.deferred_fields["Country"]["value_entered_by_user"] == "Malaysia"
