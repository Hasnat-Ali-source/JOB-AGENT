"""
Search Pipeline Orchestration (Phase 3).

Coordinates the complete job search workflow:
1. Launch connector for a platform
2. Collect job links from search results (capped by daily_search_limit)
3. Read job details
4. Check for duplicates (exact dedup_hash, then fuzzy match)
5. Evaluate hard filters
6. Score fit to profile
7. Store jobs to SQLite
8. Write the audit trail and report results

Jobs that fail hard filters are still stored, with hard_filter_pass=False, so the
user can inspect what was rejected and why; only duplicates are dropped.

Used by dashboard search API and (Phase 8) scheduled search tasks.
"""

import logging
from typing import Any, List, Optional

from sqlalchemy.orm import Session

from job_agent.models.database import (
    Job, SearchProfile, PlatformAccount, AuditLog, AuditAction
)
from job_agent.core.session_manager import get_session_manager
from job_agent.connectors import create_connector_for_account
from job_agent.connectors.base import JobPosting
from job_agent.services.job_deduplicator import generate_dedup_hash, find_duplicate_job
from job_agent.services.filter_evaluator import evaluate_hard_filters
from job_agent.services.fit_scorer import get_fit_scorer
from job_agent.utils.dates import parse_posted_at, utcnow

logger = logging.getLogger(__name__)

# How many recent same-platform-agnostic jobs to fuzzy-compare against.
# Exact hash lookup is the primary pass; this is the near-miss safety net.
FUZZY_CANDIDATE_WINDOW = 200


class SearchResult:
    """Result of a search operation."""

    def __init__(self) -> None:
        self.jobs_found = 0  # Links returned by the connector (after limit cap)
        self.new_jobs = 0  # Jobs newly stored to SQLite
        self.duplicates_skipped = 0  # Jobs already known (hash or fuzzy match)
        self.hard_filters_failed = 0  # Stored, but marked hard_filter_pass=False
        self.limit_reached = False  # True when daily_search_limit capped this run
        self.jobs_stored: List[Job] = []
        self.errors: List[str] = []

    def to_dict(self) -> dict:
        """Serialize metrics for API responses and audit logs."""
        return {
            "jobs_found": self.jobs_found,
            "new_jobs": self.new_jobs,
            "duplicates_skipped": self.duplicates_skipped,
            "hard_filters_failed": self.hard_filters_failed,
            "limit_reached": self.limit_reached,
            "errors": self.errors,
        }


class SearchPipeline:
    """
    Orchestrates the complete job search workflow.
    """

    def __init__(self, db_session: Session):
        """
        Initialize search pipeline.

        Args:
            db_session: SQLAlchemy session for database access
        """
        self.db_session = db_session
        self.fit_scorer = get_fit_scorer()

    async def search(
        self,
        platform_account: PlatformAccount,
        search_profile: SearchProfile,
        connector: Optional[Any] = None,
    ) -> SearchResult:
        """
        Run the complete search pipeline.

        Args:
            platform_account: Platform to search on
            search_profile: Search filters to apply
            connector: Pre-built connector (tests inject one; production
                builds it from the registry and an authenticated page)

        Returns:
            SearchResult with metrics
        """
        result = SearchResult()

        try:
            if connector is None:
                connector = await self._build_connector(platform_account, result)
                if connector is None:
                    self._log_audit(platform_account, search_profile, result)
                    return result

            # Budget for this run, per §3 daily_search_limit
            remaining = self._remaining_search_budget(platform_account)

            if remaining <= 0:
                logger.info(
                    f"Daily search limit ({platform_account.daily_search_limit}) "
                    f"already reached for {platform_account.platform}"
                )
                result.limit_reached = True
                self._log_audit(platform_account, search_profile, result)
                return result

            logger.info(
                f"Searching {platform_account.platform} for '{search_profile.name}' "
                f"(budget: {remaining} jobs)"
            )

            await connector.open_search(search_profile)
            await connector.apply_search_filters(search_profile)

            job_links = await connector.collect_job_links()

            # Enforce the daily cap on collection, not just on storage
            if len(job_links) > remaining:
                logger.info(
                    f"Capping {len(job_links)} links to {remaining} "
                    f"(daily_search_limit={platform_account.daily_search_limit})"
                )
                job_links = job_links[:remaining]
                result.limit_reached = True

            result.jobs_found = len(job_links)
            logger.info(f"Found {result.jobs_found} job links on {platform_account.platform}")

            if not job_links:
                # There is a world of difference between "this board had no
                # matching postings" and "this page is not a job board", and
                # both used to arrive as "0 jobs found". Naming the page that
                # was actually read is what makes the second one fixable.
                result.errors.append(self._nothing_to_read(connector, platform_account))

            for link in job_links:
                try:
                    await self._process_job(
                        connector, link, platform_account, search_profile, result
                    )
                except Exception as e:
                    logger.error(f"Error processing job {link}: {e}")
                    result.errors.append(f"Error processing {link}: {e}")
                    continue

            self._log_audit(platform_account, search_profile, result)

            return result

        except Exception as e:
            logger.error(f"Search pipeline error: {e}")
            result.errors.append(f"Pipeline error: {e}")
            self._log_audit(platform_account, search_profile, result)
            return result

    @staticmethod
    def _nothing_to_read(connector: Any, platform_account: PlatformAccount) -> str:
        """
        Explain that the page the agent landed on had no postings.

        Args:
            connector: The connector that just searched
            platform_account: The station it searched for

        Returns:
            A message naming the page and the likely fix
        """
        page = getattr(connector, "page", None)
        landed = getattr(page, "url", None) or platform_account.search_url or "the page"

        # A sign-in form where listings should be is the single most common
        # way this happens: an account dashboard pasted in as a board URL.
        looks_like_a_login = any(
            marker in landed.lower()
            for marker in ("sign_in", "signin", "login", "log-in", "/auth")
        )

        if looks_like_a_login:
            return (
                f"{platform_account.platform} landed on a sign-in page ({landed}) — "
                f"that is an account login, not a job board. Use the public listings "
                f"page whose URL you can open in a private window and still see jobs."
            )

        return (
            f"{platform_account.platform} found no job postings on {landed} — "
            f"the page loaded but has no listings on it. Check the URL opens a list "
            f"of jobs in an ordinary browser."
        )

    async def _build_connector(
        self,
        platform_account: PlatformAccount,
        result: SearchResult,
    ) -> Optional[Any]:
        """
        Build a connector bound to the platform's authenticated page.

        Args:
            platform_account: Platform to build for
            result: SearchResult to record failures on

        Returns:
            Connector instance, or None if the platform is unusable
        """
        connector = create_connector_for_account(platform_account)

        if not connector:
            error_msg = f"No connector registered for {platform_account.platform}"
            logger.error(error_msg)
            result.errors.append(error_msg)
            return None

        session_manager = await get_session_manager()

        # A public board has nothing to sign into, so it gets a browser on
        # request. Anything that does need an account must already have one.
        page = await session_manager.get_page(
            platform_account.platform,
            needs_signin=connector.capabilities.requires_manual_signin,
        )

        if not page:
            error_msg = (
                f"Could not get an authenticated page for {platform_account.platform} "
                f"(session may have expired — reconnect the platform)"
            )
            logger.error(error_msg)
            result.errors.append(error_msg)
            return None

        connector.set_page(page)

        # Where this platform's search lives (generic/career-site connectors
        # need it; connectors with their own search flow ignore it).
        if platform_account.search_url:
            connector.set_search_url(platform_account.search_url)

        return connector

    def _remaining_search_budget(self, platform_account: PlatformAccount) -> int:
        """
        Compute how many more jobs may be collected today for this platform.

        Counts jobs already collected today from the SEARCH_RUN audit entries,
        so repeated runs within a day share one budget.

        Args:
            platform_account: Platform to check

        Returns:
            Remaining job count (never negative)
        """
        limit = platform_account.daily_search_limit or 0

        if limit <= 0:
            return 0

        midnight = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

        todays_runs = self.db_session.query(AuditLog).filter(
            AuditLog.action == AuditAction.SEARCH_RUN,
            AuditLog.platform == platform_account.platform,
            AuditLog.timestamp >= midnight,
        ).all()

        collected_today = 0
        for run in todays_runs:
            detail = run.detail_json or {}
            collected_today += int(detail.get("jobs_found", 0) or 0)

        return max(0, limit - collected_today)

    async def _process_job(
        self,
        connector: Any,
        job_url: str,
        platform_account: PlatformAccount,
        search_profile: SearchProfile,
        result: SearchResult,
    ) -> None:
        """
        Process a single job posting: read, dedup, filter, score, store.

        Args:
            connector: Platform connector
            job_url: URL to the job posting
            platform_account: Platform account
            search_profile: Search profile
            result: SearchResult to update
        """
        job_posting = await connector.read_job_details(job_url)

        if not job_posting:
            logger.debug(f"No job details returned for {job_url}")
            return

        dedup_hash = generate_dedup_hash(
            job_posting.company,
            job_posting.title,
            job_posting.location,
        )

        # Primary dedup pass: exact hash
        existing = self.db_session.query(Job).filter(
            Job.dedup_hash == dedup_hash
        ).first()

        # Secondary dedup pass: fuzzy match against recent jobs
        if not existing:
            candidates = self.db_session.query(Job).order_by(
                Job.first_seen_at.desc()
            ).limit(FUZZY_CANDIDATE_WINDOW).all()

            existing = find_duplicate_job(
                job_posting.company,
                job_posting.title,
                job_posting.location,
                candidates,
            )

        if existing:
            logger.debug(
                f"Duplicate job detected: {job_posting.title} at {job_posting.company} "
                f"(matches job id={existing.id})"
            )
            result.duplicates_skipped += 1
            self._log_dedup(platform_account, job_posting, existing)
            return

        job = self._build_job(job_posting, platform_account, dedup_hash)

        # Hard filters: record the verdict, keep the job either way
        job.hard_filter_pass = evaluate_hard_filters(job, search_profile)

        if not job.hard_filter_pass:
            logger.debug(f"Job failed hard filters: {job.title}")
            result.hard_filters_failed += 1
            job.status = "filtered_out"

        # Fit score (Phase 3 heuristic; Phase 4 swaps in the LLM scorer)
        job.fit_score = self.fit_scorer.score_job(job, search_profile)

        self.db_session.add(job)
        self.db_session.commit()
        self.db_session.refresh(job)

        result.new_jobs += 1
        result.jobs_stored.append(job)

        logger.info(
            f"Stored job: {job.title} at {job.company} "
            f"(score: {job.fit_score:.2f}, hard_filter_pass: {job.hard_filter_pass})"
        )

    def _build_job(
        self,
        job_posting: JobPosting,
        platform_account: PlatformAccount,
        dedup_hash: str,
    ) -> Job:
        """
        Convert a connector JobPosting into a storable Job row.

        Args:
            job_posting: Connector output
            platform_account: Platform the posting came from
            dedup_hash: Precomputed dedup hash

        Returns:
            Unsaved Job instance
        """
        raw_data = dict(job_posting.raw_data or {})
        raw_data.setdefault("url", job_posting.apply_url)
        raw_data.setdefault("external_id", job_posting.external_id)

        return Job(
            platform=platform_account.platform,
            external_id=job_posting.external_id or "",
            title=job_posting.title,
            company=job_posting.company,
            location=job_posting.location or "",
            job_type=job_posting.job_type,
            description=job_posting.description or "",
            requirements=job_posting.requirements,
            salary=job_posting.salary,
            # posted_at arrives as free text ("2 days ago", ISO, etc.)
            posted_at=parse_posted_at(job_posting.posted_at),
            posted_at_text=job_posting.posted_at,
            apply_method=job_posting.apply_method or "web_form",
            recruiter_contact=job_posting.recruiter_contact,
            dedup_hash=dedup_hash,
            status="new",
            raw_data=raw_data,
        )

    def _log_dedup(
        self,
        platform_account: PlatformAccount,
        job_posting: JobPosting,
        existing: Job,
    ) -> None:
        """
        Record a deduplication event in the audit log.

        Args:
            platform_account: Platform the duplicate came from
            job_posting: The duplicate posting
            existing: The job it matched
        """
        audit = AuditLog(
            timestamp=utcnow(),
            action=AuditAction.JOB_DEDUPED,
            platform=platform_account.platform,
            actor="agent",
            detail=(
                f"Skipped duplicate '{job_posting.title}' at {job_posting.company} "
                f"(matches job id={existing.id})"
            ),
            detail_json={
                "existing_job_id": existing.id,
                "title": job_posting.title,
                "company": job_posting.company,
                "location": job_posting.location,
            },
            result="success",
        )

        self.db_session.add(audit)
        self.db_session.commit()

    def _log_audit(
        self,
        platform_account: PlatformAccount,
        search_profile: SearchProfile,
        result: SearchResult,
    ) -> None:
        """
        Log the audit trail for a search run.

        Args:
            platform_account: Platform account
            search_profile: Search profile
            result: SearchResult
        """
        detail_json = {
            "search_profile_id": search_profile.id,
            "search_profile_name": search_profile.name,
            "platform": platform_account.platform,
            "daily_search_limit": platform_account.daily_search_limit,
        }
        detail_json.update(result.to_dict())

        audit = AuditLog(
            timestamp=utcnow(),
            action=AuditAction.SEARCH_RUN,
            platform=platform_account.platform,
            actor="agent",
            detail=(
                f"Searched for '{search_profile.name}' on {platform_account.platform}: "
                f"{result.jobs_found} found, {result.new_jobs} new, "
                f"{result.duplicates_skipped} duplicates"
            ),
            detail_json=detail_json,
            result="success" if not result.errors else "partial",
            error_message="; ".join(result.errors) if result.errors else None,
        )

        self.db_session.add(audit)
        self.db_session.commit()
