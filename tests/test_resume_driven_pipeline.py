"""
The resume decides what is searched for, and therefore what is on the wire.

The failure being fixed: the search profile and the master resume were edited
independently. On a real install this produced a search for "software
developer" against a Full-Stack resume, a wire in which *none* of 59 postings
shared a job title with the resume, and a fit analyser reporting 0-7% on every
one — which reads as a broken analyser rather than an agent looking for the
wrong jobs.
"""

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from job_agent.models.database import (
    DocumentType,
    Job,
    MasterDocument,
    SearchProfile,
)
from job_agent.services.resume_profile import matches_titles, read_resume
from job_agent.services.resume_sync import sync_to_resume


RESUME = """HASNAT TAHIR
Petaling Jaya, Malaysia | hasnat@example.com
PROFILE SUMMARY
Full-Stack Developer with hands-on experience across the React ecosystem and
Node.js back ends. Actively seeking remote and contract-based Full-Stack
Developer engagements.
TECHNICAL SKILLS
Frontend: React, Vite, Tailwind CSS
Backend: Node.js, Express
Databases & Caching: MongoDB, Redis
PROFESSIONAL EXPERIENCE
React Developer | SimpleX Technology Sep 2024 - Feb 2026
• Built desktop applications using Windows Forms and WPF.
Front-End Developer | ADMAXIM Jul 2023 - Sep 2024
• Developed responsive web interfaces using React and JavaScript.
Visiting Lecturer | Govt. Islamia Graduate College Sep 2022 - Jul 2023
• Managed computer labs and coursework delivery.
EDUCATION
Bachelor of Science in Computer Science 2017 - 2021
"""


@pytest.fixture
def session():
    """In-memory database."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)

    with Session(engine) as db_session:
        yield db_session


@pytest.fixture
def master(session):
    record = MasterDocument(
        doc_type=DocumentType.RESUME,
        name="Hasnat CV",
        source_path="/tmp/unused.pdf",
        source_format="pdf",
        content_text=RESUME,
        is_active=True,
    )
    session.add(record)
    session.commit()
    session.refresh(record)

    return record


@pytest.fixture
def search_profile(session):
    record = SearchProfile(
        name="Default",
        target_titles=["software developer"],
        keywords=[],
        is_active=True,
    )
    session.add(record)
    session.commit()
    session.refresh(record)

    return record


class TestReadingAResume:
    """What the resume says it should be searched for."""

    def test_the_stated_target_leads(self):
        """
        What they say they want outranks what they have done — a lecturer
        retraining into development should be searched for development.
        """
        profile = read_resume(RESUME)

        assert profile.titles[0] == "Full-Stack Developer"

    def test_a_hyphenated_title_survives(self):
        """
        Treating a bare hyphen as a title separator returned "Front" from
        "Front-End Developer" — one word, discarded — so the most relevant
        role on the resume was invisible to the search.
        """
        assert "Front-End Developer" in read_resume(RESUME).titles

    def test_every_held_title_is_found(self):
        profile = read_resume(RESUME)

        for title in ("React Developer", "Front-End Developer", "Visiting Lecturer"):
            assert title in profile.titles

    def test_the_whole_skills_section_is_read(self):
        """
        A labelled skills line reads as a heading, so the section ended after
        its first line and every category but the first was lost.
        """
        keywords = read_resume(RESUME).keywords

        assert "React" in keywords
        assert "MongoDB" in keywords
        assert "Redis" in keywords

    def test_a_stray_fragment_is_trimmed_from_a_stated_target(self):
        """"contract-based Full-Stack Developer" must not yield "based Full-Stack Developer"."""
        assert not any(
            title.lower().startswith("based") for title in read_resume(RESUME).titles
        )

    def test_a_resume_with_no_experience_section_yields_nothing(self):
        """Searching on a guess is how the wire filled with unanswerable jobs."""
        profile = read_resume("Jane Doe\njane@example.com\nHello.")

        assert not profile.is_usable

    def test_seniority_comes_from_the_titles(self):
        senior = read_resume(RESUME.replace("React Developer |", "Senior Engineer |"))

        assert senior.seniority == "senior"


class TestMatchingTitles:
    """Which postings a resume is even looking for."""

    def test_a_related_posting_matches(self):
        profile = read_resume(RESUME)

        assert matches_titles("Senior React Developer", profile)

    def test_an_unrelated_posting_does_not(self):
        profile = read_resume(RESUME)

        assert not matches_titles("Vice President, Data & Insights", profile)

    def test_nothing_derived_filters_nothing(self):
        """An unreadable resume must not silently hide every posting."""
        empty = read_resume("")

        assert matches_titles("Anything At All", empty)


class TestSyncing:
    """Bringing the search into line with the resume."""

    def test_the_search_takes_the_resume_s_titles(self, session, master, search_profile):
        sync_to_resume(session, master)
        session.commit()
        session.refresh(search_profile)

        assert "Full-Stack Developer" in search_profile.target_titles
        assert "software developer" not in search_profile.target_titles

    def test_the_search_takes_the_resume_s_skills(self, session, master, search_profile):
        sync_to_resume(session, master)
        session.commit()
        session.refresh(search_profile)

        assert "React" in search_profile.keywords

    def test_the_profile_records_which_resume_it_came_from(
        self, session, master, search_profile
    ):
        sync_to_resume(session, master)
        session.commit()
        session.refresh(search_profile)

        assert search_profile.derived_from_master_id == master.id

    def test_the_user_s_own_location_choices_are_left_alone(
        self, session, master, search_profile
    ):
        """
        A resume says nothing about where you want to work or what you will
        accept. Overwriting those would undo settings chosen deliberately.
        """
        search_profile.country = "MY"
        search_profile.remote_pref = "remote"
        search_profile.salary_min = 50000
        session.commit()

        sync_to_resume(session, master)
        session.commit()
        session.refresh(search_profile)

        assert search_profile.country == "MY"
        assert search_profile.remote_pref == "remote"
        assert search_profile.salary_min == 50000

    def test_postings_from_an_earlier_resume_are_counted(
        self, session, master, search_profile
    ):
        session.add(
            Job(
                platform="greenhouse",
                external_id="OLD-1",
                title="Vice President, Data",
                company="Acme",
                location="Remote",
                description="",
                apply_method="web_form",
                dedup_hash="old-1",
                matched_master_id=None,
            )
        )
        session.commit()

        result = sync_to_resume(session, master)

        assert result.jobs_from_previous_resumes == 1
        assert "no longer on the wire" in result.describe()

    def test_an_unreadable_resume_leaves_the_search_alone(
        self, session, search_profile
    ):
        """Better a stale search than one derived from nothing."""
        blank = MasterDocument(
            doc_type=DocumentType.RESUME,
            name="Empty",
            source_path="/tmp/unused.pdf",
            source_format="pdf",
            content_text="Jane Doe\njane@example.com",
            is_active=True,
        )
        session.add(blank)
        session.commit()

        sync_to_resume(session, blank)
        session.commit()
        session.refresh(search_profile)

        assert search_profile.target_titles == ["software developer"]

    def test_no_resume_is_not_an_error(self, session):
        assert sync_to_resume(session).master_id is None
