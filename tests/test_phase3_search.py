#!/usr/bin/env python3
"""
Integration tests for Phase 3: Job Search & Pipeline.

Covers the Phase 3 acceptance criteria:
- Run a search against a test platform, collect 10 jobs, store to SQLite
- Duplicate detection: the same posting from two search results is deduplicated
- Hard filters: with salary_min=$100k, an $80k job is stored with hard_filter_pass=False
- Metrics reported: jobs_found, new_jobs, duplicates_skipped
- daily_search_limit is respected during link collection
"""

from datetime import datetime, timedelta
from typing import List

import pytest
from sqlmodel import Session, SQLModel, create_engine

from job_agent.connectors.base import (
    ApplicationSession,
    ConnectedPlatformConnector,
    JobPosting,
    PlatformCapabilities,
    SubmissionResult,
)
from job_agent.core.search_pipeline import SearchPipeline
from job_agent.models.database import (
    AuditAction,
    AuditLog,
    Job,
    PlatformAccount,
    SearchProfile,
)
from job_agent.services.filter_evaluator import FilterEvaluator, evaluate_hard_filters
from job_agent.services.fit_scorer import FitScorer, get_fit_scorer
from job_agent.services.job_deduplicator import (
    JobDeduplicator,
    find_duplicate_job,
    generate_dedup_hash,
    is_duplicate_job,
)
from job_agent.utils.dates import parse_posted_at, utcnow


# ============================================================================
# Fixtures & fakes
# ============================================================================

class FakeConnector(ConnectedPlatformConnector):
    """
    In-memory connector returning a fixed set of postings.

    Lets the pipeline be tested end-to-end without a browser.
    """

    def __init__(self, postings: List[JobPosting], platform_name: str = "fake"):
        capabilities = PlatformCapabilities(
            can_search=True,
            can_filter=True,
            can_read_details=True,
        )
        super().__init__(platform_name, capabilities)
        self.postings = postings
        self.open_search_calls = 0
        self.apply_filters_calls = 0
        self.read_calls: List[str] = []

    async def check_session(self):
        return "connected"

    async def open_search(self, search_profile) -> None:
        self.open_search_calls += 1

    async def apply_search_filters(self, search_profile) -> None:
        self.apply_filters_calls += 1

    async def collect_job_links(self) -> List[str]:
        return [f"https://example.test/job/{p.external_id}" for p in self.postings]

    async def read_job_details(self, job_url: str) -> JobPosting:
        self.read_calls.append(job_url)
        external_id = job_url.rsplit("/", 1)[-1]
        return next(p for p in self.postings if p.external_id == external_id)

    async def begin_application(self, job: JobPosting) -> ApplicationSession:
        return ApplicationSession(job=job, platform_account_id=0)

    async def fill_application(self, session, candidate_profile, application_package):
        return session

    async def submit_application(self, session) -> SubmissionResult:
        return SubmissionResult(success=False, error_message="fake connector")


def make_posting(
    external_id: str,
    title: str = "Backend Engineer",
    company: str = "TechCorp",
    location: str = "Remote",
    salary: str = "$120,000 - $160,000",
    description: str = "Build backend services in Python on AWS.",
    posted_at: str = "2 days ago",
) -> JobPosting:
    """Build a JobPosting with sensible defaults."""
    return JobPosting(
        platform="fake",
        external_id=external_id,
        title=title,
        company=company,
        location=location,
        job_type="full_time",
        description=description,
        requirements="Python, PostgreSQL, AWS",
        salary=salary,
        posted_at=posted_at,
        apply_method="web_form",
        apply_url=f"https://example.test/job/{external_id}",
    )


@pytest.fixture
def session():
    """In-memory SQLite session with all tables created."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    with Session(engine) as db_session:
        yield db_session


@pytest.fixture
def account(session) -> PlatformAccount:
    """A connected platform account with a generous search limit."""
    acct = PlatformAccount(
        platform="fake",
        profile_dir="/tmp/job-agent-test/fake",
        daily_search_limit=100,
    )
    session.add(acct)
    session.commit()
    session.refresh(acct)
    return acct


@pytest.fixture
def profile(session) -> SearchProfile:
    """A search profile targeting remote backend roles."""
    prof = SearchProfile(
        name="Backend - Remote",
        target_titles=["Backend Engineer"],
        alt_titles=["Software Engineer"],
        keywords=["python"],
        exclusions=[],
        remote_pref="remote",
    )
    session.add(prof)
    session.commit()
    session.refresh(prof)
    return prof


# ============================================================================
# Deduplication
# ============================================================================

class TestJobDeduplicator:
    """Dedup hashing and fuzzy matching."""

    def test_same_job_same_hash(self):
        h1 = generate_dedup_hash("TechCorp", "Backend Engineer", "Remote")
        h2 = generate_dedup_hash("TechCorp", "Backend Engineer", "Remote")
        assert h1 == h2

    def test_normalization_collapses_variants(self):
        """Company suffix, seniority prefix, and casing are normalized away."""
        h1 = generate_dedup_hash("TechCorp Inc.", "Senior Backend Engineer", "Remote")
        h2 = generate_dedup_hash("techcorp", "backend engineer", "REMOTE")
        assert h1 == h2

    def test_remote_variants_normalize(self):
        assert JobDeduplicator._normalize_location("Work From Home") == "remote"
        assert JobDeduplicator._normalize_location("Remote - US") == "remote"

    def test_different_jobs_different_hash(self):
        h1 = generate_dedup_hash("TechCorp", "Backend Engineer", "Remote")
        h2 = generate_dedup_hash("OtherCo", "Frontend Developer", "New York, NY")
        assert h1 != h2

    def test_fuzzy_match_near_duplicate(self):
        assert is_duplicate_job(
            "TechCorp", "Sr. Backend Engineer", "Remote",
            "TechCorp Inc", "Senior Backend Engineer", "Remote",
        )

    def test_fuzzy_rejects_unrelated(self):
        assert not is_duplicate_job(
            "TechCorp", "Backend Engineer", "Remote",
            "Globex", "Marketing Manager", "New York, NY",
        )

    def test_find_duplicate_in_candidates(self):
        existing = Job(
            platform="fake", external_id="1", title="Senior Backend Engineer",
            company="TechCorp Inc", location="Remote", description="",
            apply_method="web_form", dedup_hash="abc",
        )

        match = find_duplicate_job("TechCorp", "Sr. Backend Engineer", "Remote", [existing])
        assert match is existing

        no_match = find_duplicate_job("Globex", "Marketing Manager", "NYC", [existing])
        assert no_match is None


# ============================================================================
# Hard filters
# ============================================================================

class TestFilterEvaluator:
    """Hard pass/fail filtering."""

    def _job(self, **overrides) -> Job:
        defaults = dict(
            platform="fake", external_id="1", title="Backend Engineer",
            company="TechCorp", location="Remote",
            description="Python backend work", requirements="Python",
            salary="$120,000 - $160,000", apply_method="web_form",
            dedup_hash="hash", job_type="full_time",
        )
        defaults.update(overrides)
        return Job(**defaults)

    def test_salary_below_minimum_fails(self):
        """Acceptance: salary_min=$100k → an $80k job fails the hard filter."""
        profile = SearchProfile(name="p", salary_min=100000)
        job = self._job(salary="$80,000 - $95,000")

        assert evaluate_hard_filters(job, profile) is False

    def test_salary_above_minimum_passes(self):
        profile = SearchProfile(name="p", salary_min=100000)
        job = self._job(salary="$120,000 - $160,000")

        assert evaluate_hard_filters(job, profile) is True

    def test_missing_salary_is_not_rejected(self):
        """Unknown salary shouldn't silently drop a job."""
        profile = SearchProfile(name="p", salary_min=100000)
        job = self._job(salary=None)

        assert evaluate_hard_filters(job, profile) is True

    def test_k_notation_salary(self):
        assert FilterEvaluator._extract_salary_min("100k-150k") == 100000
        assert FilterEvaluator._extract_salary_min("$95,000") == 95000

    def test_exclusion_keyword_fails(self):
        profile = SearchProfile(name="p", exclusions=["unpaid"])
        job = self._job(description="This is an unpaid internship")

        assert evaluate_hard_filters(job, profile) is False

    def test_remote_preference_fails_onsite(self):
        profile = SearchProfile(name="p", remote_pref="remote")
        job = self._job(location="New York, NY")

        assert evaluate_hard_filters(job, profile) is False

    def test_stale_posting_fails_date_filter(self):
        profile = SearchProfile(name="p", date_posted_within_days=7)
        job = self._job(posted_at=utcnow() - timedelta(days=30))

        assert evaluate_hard_filters(job, profile) is False

    def test_recent_posting_passes_date_filter(self):
        profile = SearchProfile(name="p", date_posted_within_days=7)
        job = self._job(posted_at=utcnow() - timedelta(days=2))

        assert evaluate_hard_filters(job, profile) is True

    def test_job_type_mismatch_fails(self):
        profile = SearchProfile(name="p", job_type="contract")
        job = self._job(job_type="full_time")

        assert evaluate_hard_filters(job, profile) is False


class TestRemoteJobsInARegion:
    """
    "Remote, in this country" is the most ordinary search there is, and it used
    to match almost nothing: the region had to appear verbatim in the location,
    so "Remote" and "Remote, US" were both rejected for a profile asking for
    the United States. The run then reported zero jobs, which reads as "there
    is no such work" rather than "your filter cannot match this wording".
    """

    def _job(self, location: str) -> Job:
        return Job(
            platform="fake", external_id="1", title="Frontend Engineer",
            company="Acme", location=location, description="React work",
            apply_method="web_form", dedup_hash="hash",
        )

    @pytest.mark.parametrize("location", [
        "Remote",
        "Remote, US",
        "Remote (USA)",
        "Remote - United States",
        "Remote, United States",
    ])
    def test_remote_us_postings_pass(self, location):
        profile = SearchProfile(
            name="p", remote_pref="remote", region="united state"
        )

        assert evaluate_hard_filters(self._job(location), profile) is True

    @pytest.mark.parametrize("location", [
        "Remote - Europe",
        "Remote, Canada",
        "London, UK",
        "San Francisco, CA",
    ])
    def test_other_places_still_fail(self, location):
        profile = SearchProfile(
            name="p", remote_pref="remote", region="united state"
        )

        assert evaluate_hard_filters(self._job(location), profile) is False

    def test_short_aliases_match_whole_words_only(self):
        """"us" must not match inside "Austin", nor "uk" inside "Ukraine"."""
        profile = SearchProfile(
            name="p", remote_pref="remote", region="united states"
        )

        assert evaluate_hard_filters(self._job("Remote - Austin, TX"), profile) is False
        assert evaluate_hard_filters(self._job("Remote, Ukraine"), profile) is False


# ============================================================================
# Fit scoring
# ============================================================================

class TestFitScorer:
    """Phase 3 heuristic scoring (LLM scoring lands in Phase 4)."""

    def _job(self, **overrides) -> Job:
        defaults = dict(
            platform="fake", external_id="1", title="Backend Engineer",
            company="TechCorp", location="Remote",
            description="Python, AWS, distributed systems. 5+ years experience.",
            apply_method="web_form", dedup_hash="hash",
        )
        defaults.update(overrides)
        return Job(**defaults)

    def test_score_is_bounded(self):
        profile = SearchProfile(name="p", target_titles=["Backend Engineer"])
        score = get_fit_scorer().score_job(self._job(), profile)

        assert 0.0 <= score <= 1.0

    def test_title_match_scores_higher_than_mismatch(self):
        profile = SearchProfile(name="p", target_titles=["Backend Engineer"])

        match = get_fit_scorer().score_job(self._job(), profile)
        mismatch = get_fit_scorer().score_job(self._job(title="Sales Director"), profile)

        assert match > mismatch

    def test_alt_title_scores_between(self):
        profile = SearchProfile(
            name="p",
            target_titles=["Backend Engineer"],
            alt_titles=["Platform Engineer"],
        )

        exact = FitScorer._score_title("Backend Engineer", profile)
        alt = FitScorer._score_title("Platform Engineer", profile)

        assert exact == 1.0
        assert 0.0 < alt < exact

    def test_keyword_coverage_scores_proportionally(self):
        assert FitScorer._score_keywords("python and aws", ["python", "aws"]) == 1.0
        assert FitScorer._score_keywords("python only", ["python", "aws"]) == 0.5

    def test_exclusions_penalize(self):
        assert FitScorer._score_exclusions("clean description", ["unpaid"]) == 1.0
        assert FitScorer._score_exclusions("unpaid role", ["unpaid"]) < 0.5


# ============================================================================
# Date parsing
# ============================================================================

class TestParsePostedAt:
    """Posting dates arrive as free text and must become datetimes."""

    def test_iso_with_zulu(self):
        parsed = parse_posted_at("2026-08-12T10:00:00Z")
        assert parsed == datetime(2026, 8, 12, 10, 0, 0)

    def test_relative_days(self):
        now = datetime(2026, 8, 14, 12, 0, 0)
        assert parse_posted_at("2 days ago", now=now) == datetime(2026, 8, 12, 12, 0, 0)

    def test_relative_with_prefix(self):
        now = datetime(2026, 8, 14, 12, 0, 0)
        assert parse_posted_at("Posted 1 week ago", now=now) == datetime(2026, 8, 7, 12, 0, 0)

    def test_human_format(self):
        assert parse_posted_at("August 12, 2026") == datetime(2026, 8, 12)

    def test_today_and_yesterday(self):
        now = datetime(2026, 8, 14, 9, 0, 0)
        assert parse_posted_at("today", now=now) == now
        assert parse_posted_at("yesterday", now=now) == datetime(2026, 8, 13, 9, 0, 0)

    def test_unparseable_returns_none(self):
        assert parse_posted_at("whenever we feel like it") is None
        assert parse_posted_at(None) is None
        assert parse_posted_at("") is None


# ============================================================================
# Pipeline (end-to-end, no browser)
# ============================================================================

@pytest.mark.asyncio
class TestSearchPipeline:
    """End-to-end pipeline behavior against an in-memory database."""

    async def test_collects_ten_jobs_and_stores_them(self, session, account, profile):
        """Acceptance: collect 10 jobs from a platform and store them to SQLite."""
        postings = [make_posting(str(i), company=f"Company{i}") for i in range(1, 11)]
        pipeline = SearchPipeline(session)

        result = await pipeline.search(account, profile, connector=FakeConnector(postings))

        assert result.jobs_found == 10
        assert result.new_jobs == 10
        assert result.duplicates_skipped == 0
        assert not result.errors
        assert session.query(Job).count() == 10

    async def test_stored_job_fields_are_populated(self, session, account, profile):
        posting = make_posting("1", posted_at="2026-08-12T10:00:00Z")
        pipeline = SearchPipeline(session)

        await pipeline.search(account, profile, connector=FakeConnector([posting]))

        job = session.query(Job).one()
        assert job.platform == "fake"
        assert job.external_id == "1"
        assert job.title == "Backend Engineer"
        assert job.company == "TechCorp"
        assert job.posted_at == datetime(2026, 8, 12, 10, 0, 0)
        assert job.posted_at_text == "2026-08-12T10:00:00Z"
        assert job.dedup_hash
        assert job.fit_score is not None
        assert job.raw_data["url"] == "https://example.test/job/1"

    async def test_duplicate_from_second_result_is_skipped(self, session, account, profile):
        """Acceptance: the same posting appearing twice is deduplicated once."""
        postings = [
            make_posting("1", title="Backend Engineer", company="TechCorp"),
            # Same job, different listing id and slightly different wording
            make_posting("2", title="Sr. Backend Engineer", company="TechCorp Inc"),
        ]
        pipeline = SearchPipeline(session)

        result = await pipeline.search(account, profile, connector=FakeConnector(postings))

        assert result.jobs_found == 2
        assert result.new_jobs == 1
        assert result.duplicates_skipped == 1
        assert session.query(Job).count() == 1

    async def test_duplicate_across_runs_is_skipped(self, session, account, profile):
        pipeline = SearchPipeline(session)
        postings = [make_posting("1")]

        first = await pipeline.search(account, profile, connector=FakeConnector(postings))
        second = await pipeline.search(account, profile, connector=FakeConnector(postings))

        assert first.new_jobs == 1
        assert second.new_jobs == 0
        assert second.duplicates_skipped == 1

    async def test_hard_filter_failure_is_stored_not_dropped(self, session, account):
        """Acceptance: with salary_min=$100k, an $80k job gets hard_filter_pass=False."""
        strict_profile = SearchProfile(
            name="Well paid only",
            target_titles=["Backend Engineer"],
            salary_min=100000,
        )
        session.add(strict_profile)
        session.commit()
        session.refresh(strict_profile)

        postings = [
            make_posting("1", company="RichCo", salary="$120,000 - $160,000"),
            make_posting("2", company="CheapCo", salary="$80,000 - $95,000"),
        ]
        pipeline = SearchPipeline(session)

        result = await pipeline.search(account, strict_profile, connector=FakeConnector(postings))

        assert result.new_jobs == 2
        assert result.hard_filters_failed == 1

        rich = session.query(Job).filter(Job.company == "RichCo").one()
        cheap = session.query(Job).filter(Job.company == "CheapCo").one()

        assert rich.hard_filter_pass is True
        assert cheap.hard_filter_pass is False
        assert cheap.status == "filtered_out"

    async def test_daily_search_limit_caps_collection(self, session, profile):
        """Link collection stops at daily_search_limit."""
        account = PlatformAccount(
            platform="fake",
            profile_dir="/tmp/job-agent-test/fake",
            daily_search_limit=3,
        )
        session.add(account)
        session.commit()
        session.refresh(account)

        postings = [make_posting(str(i), company=f"Company{i}") for i in range(1, 11)]
        pipeline = SearchPipeline(session)

        result = await pipeline.search(account, profile, connector=FakeConnector(postings))

        assert result.jobs_found == 3
        assert result.limit_reached is True
        assert session.query(Job).count() == 3

    async def test_daily_limit_is_shared_across_runs(self, session, profile):
        """A second run the same day sees the reduced remaining budget."""
        account = PlatformAccount(
            platform="fake",
            profile_dir="/tmp/job-agent-test/fake",
            daily_search_limit=4,
        )
        session.add(account)
        session.commit()
        session.refresh(account)

        pipeline = SearchPipeline(session)
        batch_one = [make_posting(str(i), company=f"A{i}") for i in range(1, 4)]
        batch_two = [make_posting(str(i), company=f"B{i}") for i in range(10, 20)]

        first = await pipeline.search(account, profile, connector=FakeConnector(batch_one))
        second = await pipeline.search(account, profile, connector=FakeConnector(batch_two))

        assert first.jobs_found == 3
        assert second.jobs_found == 1  # only 1 of the 4/day budget left
        assert second.limit_reached is True

    async def test_exhausted_limit_short_circuits(self, session, profile):
        account = PlatformAccount(
            platform="fake",
            profile_dir="/tmp/job-agent-test/fake",
            daily_search_limit=1,
        )
        session.add(account)
        session.commit()
        session.refresh(account)

        pipeline = SearchPipeline(session)
        connector = FakeConnector([make_posting("1")])

        await pipeline.search(account, profile, connector=FakeConnector([make_posting("9")]))
        second = await pipeline.search(account, profile, connector=connector)

        assert second.jobs_found == 0
        assert second.limit_reached is True
        assert connector.open_search_calls == 0  # never even opened the search page

    async def test_audit_log_records_run_metrics(self, session, account, profile):
        """Acceptance: dashboard-visible counts land in the audit log."""
        postings = [make_posting(str(i), company=f"Company{i}") for i in range(1, 4)]
        pipeline = SearchPipeline(session)

        await pipeline.search(account, profile, connector=FakeConnector(postings))

        audit = session.query(AuditLog).filter(
            AuditLog.action == AuditAction.SEARCH_RUN
        ).one()

        assert audit.platform == "fake"
        assert audit.result == "success"
        assert audit.detail_json["jobs_found"] == 3
        assert audit.detail_json["new_jobs"] == 3
        assert audit.detail_json["duplicates_skipped"] == 0

    async def test_dedup_events_are_audited(self, session, account, profile):
        postings = [
            make_posting("1", title="Backend Engineer", company="TechCorp"),
            make_posting("2", title="Sr. Backend Engineer", company="TechCorp Inc"),
        ]
        pipeline = SearchPipeline(session)

        await pipeline.search(account, profile, connector=FakeConnector(postings))

        deduped = session.query(AuditLog).filter(
            AuditLog.action == AuditAction.JOB_DEDUPED
        ).all()

        assert len(deduped) == 1
        assert deduped[0].detail_json["company"] == "TechCorp Inc"

    async def test_read_failure_on_one_job_does_not_abort_run(self, session, account, profile):
        class FlakyConnector(FakeConnector):
            async def read_job_details(self, job_url: str) -> JobPosting:
                if job_url.endswith("/2"):
                    raise RuntimeError("page crashed")
                return await super().read_job_details(job_url)

        postings = [make_posting(str(i), company=f"Company{i}") for i in range(1, 4)]
        pipeline = SearchPipeline(session)

        result = await pipeline.search(account, profile, connector=FlakyConnector(postings))

        assert result.jobs_found == 3
        assert result.new_jobs == 2
        assert len(result.errors) == 1
        assert session.query(Job).count() == 2

    async def test_connector_search_hooks_are_called(self, session, account, profile):
        connector = FakeConnector([make_posting("1")])
        pipeline = SearchPipeline(session)

        await pipeline.search(account, profile, connector=connector)

        assert connector.open_search_calls == 1
        assert connector.apply_filters_calls == 1
        assert connector.read_calls == ["https://example.test/job/1"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
