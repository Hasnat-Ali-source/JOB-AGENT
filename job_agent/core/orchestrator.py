"""
Run Orchestrator (Phase 8).

The loop that ties every phase together:

    for each connected platform:
        skip if paused, disconnected, or out of daily budget
        search        (Phase 3)  → jobs stored, deduped, filtered, scored
        generate      (Phase 4)  → tailored documents for the best matches
        queue         (Phase 5)  → nothing is submitted unattended here
    write the run summary

**What this loop deliberately does not do.** It never submits. Phase 6 built
submission behind a gate, and the gate's whole purpose is that the first
applications on a platform are seen by a human. A scheduled run happens while
the user is asleep, so it prepares work and stops; submitting is a separate,
user-initiated act. `generate_documents` and `queue_applications` are both
opt-in per run.

**One platform's failure is not the run's failure.** A CAPTCHA on Indeed pauses
Indeed and leaves Greenhouse to finish — that is the difference between a run
that returns 40 jobs with one platform flagged, and a run that returns nothing.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from job_agent.config import settings
from job_agent.connectors import create_connector_for_account
from job_agent.core.search_pipeline import SearchPipeline
from job_agent.models.database import (
    AgentRun,
    AuditAction,
    AuditLog,
    ConnectionStatus,
    Job,
    PlatformAccount,
    RunStatus,
    SearchProfile,
)
from job_agent.services.interruption_detector import InterruptionDetector
from job_agent.utils.dates import utcnow

logger = logging.getLogger(__name__)


@dataclass
class PlatformOutcome:
    """What happened on one platform during a run."""

    platform: str
    ran: bool = False
    skip_reason: Optional[str] = None
    jobs_found: int = 0
    new_jobs: int = 0
    duplicates_skipped: int = 0
    hard_filters_failed: int = 0
    documents_generated: int = 0
    applications_queued: int = 0
    interruption: Optional[dict] = None
    errors: List[str] = field(default_factory=list)


class RunOrchestrator:
    """Runs the pipeline across every connected platform."""

    def __init__(self, db_session: Session):
        """
        Initialize the orchestrator.

        Args:
            db_session: Database session
        """
        self.db_session = db_session

    async def run(
        self,
        search_profile: SearchProfile,
        platforms: Optional[List[str]] = None,
        trigger: str = "manual",
        generate_documents: bool = False,
        queue_applications: bool = False,
    ) -> AgentRun:
        """
        Run the pipeline across the connected platforms.

        Args:
            search_profile: Filters to search with
            platforms: Restrict to these platforms (default: all connected)
            trigger: "manual" or "scheduled"
            generate_documents: Tailor documents for jobs that pass the filters
            queue_applications: Open and fill application forms, then queue them
                for review. Never submits. Requires generate_documents, since an
                application without a tailored resume isn't worth queueing.

        Returns:
            The completed AgentRun record
        """
        run = AgentRun(
            search_profile_id=search_profile.id,
            trigger=trigger,
            status=RunStatus.RUNNING,
        )
        self.db_session.add(run)
        self.db_session.commit()
        self.db_session.refresh(run)

        self._log(
            AuditAction.RUN_STARTED,
            f"Run #{run.id} started ({trigger}) with profile '{search_profile.name}'",
            {"run_id": run.id, "trigger": trigger, "platforms": platforms},
        )

        accounts = self._accounts_to_run(platforms)

        if not accounts:
            logger.warning("No connected platforms to run")

        # Queueing an application without a tailored resume would attach
        # nothing, so it implies document generation rather than silently
        # producing empty applications.
        if queue_applications and not generate_documents:
            generate_documents = True
            logger.info("queue_applications implies generate_documents; enabling it")

        outcomes: List[PlatformOutcome] = []

        for account in accounts:
            outcome = await self._run_platform(
                account, search_profile, generate_documents, queue_applications
            )
            outcomes.append(outcome)

        return self._finish(run, outcomes)

    # ------------------------------------------------------------------
    # Platform selection
    # ------------------------------------------------------------------

    def _accounts_to_run(self, platforms: Optional[List[str]]) -> List[PlatformAccount]:
        """
        Choose which platform accounts this run covers.

        Args:
            platforms: Explicit platform names, or None for all

        Returns:
            Platform accounts, in a stable order
        """
        query = self.db_session.query(PlatformAccount)

        if platforms:
            query = query.filter(PlatformAccount.platform.in_(platforms))

        return query.order_by(PlatformAccount.platform).all()

    def _skip_reason(self, account: PlatformAccount) -> Optional[str]:
        """
        Decide whether a platform should be skipped, and why.

        Args:
            account: The platform account

        Returns:
            A human-readable reason, or None to run it
        """
        # The user's own "not this one, for now". Deliberately checked before
        # every other reason: a paused station should read as paused, not as
        # broken, however its session happens to be.
        if getattr(account, "paused", False):
            return "paused by you — start it again under Stations"

        if account.status == ConnectionStatus.DISABLED:
            return "platform is disabled"

        if account.status == ConnectionStatus.SESSION_EXPIRED:
            return "session expired — reconnect this platform"

        if account.status == ConnectionStatus.NEEDS_SIGNIN:
            return "not signed in — connect this platform"

        if account.status == ConnectionStatus.ERROR:
            return f"platform is in an error state: {account.last_error or 'unknown'}"

        # An unresolved CAPTCHA or MFA prompt is a task for the user; retrying
        # into a live challenge is what escalates it into a blocked account.
        from job_agent.services.session_monitor import SessionMonitor

        open_interruptions = SessionMonitor(self.db_session).open_interruptions(
            account.platform
        )

        if open_interruptions:
            return (
                f"{open_interruptions[0].kind} needs you — "
                f"{open_interruptions[0].guidance}"
            )

        connector = create_connector_for_account(account)

        if connector is None:
            return f"no connector registered for {account.platform}"

        if not connector.capabilities.can_search:
            return f"the {account.platform} connector cannot search"

        # Greenhouse, Lever, Ashby and friends host a separate board per
        # company; there is no site-wide search to point at. Connected without
        # one, the connector has no page to open and the run reports zero jobs
        # found — which reads as "no such jobs exist" rather than "you haven't
        # said whose jobs to look at".
        from job_agent.connectors.generic_ats import GenericATSConnector

        if isinstance(connector, GenericATSConnector) and not connector.search_url:
            return (
                f"{account.platform} has no board to search — add the company's "
                f"board URL for this station, or add the board as its own station"
            )

        return None

    def _search_budget_reason(self, account: PlatformAccount) -> Optional[str]:
        """
        Whether this platform has any searching left in it today.

        Kept apart from `_skip_reason` because it is not a reason to skip the
        platform outright: it caps how many postings are *collected*, and says
        nothing about preparing applications for postings already collected.

        Args:
            account: The platform account

        Returns:
            A human-readable reason, or None if there is budget left
        """
        pipeline = SearchPipeline(self.db_session)

        if pipeline._remaining_search_budget(account) <= 0:
            return (
                f"daily search limit reached "
                f"({account.daily_search_limit}/{account.daily_search_limit})"
            )

        return None

    def _unprepared_jobs(
        self,
        account: PlatformAccount,
        limit: int,
        for_applications: bool,
    ) -> List[Job]:
        """
        Jobs already found on this platform that still need work doing on them.

        The search budget stops the agent collecting more postings; it should
        not stop it acting on the ones already collected. Without this, a user
        who searched in the morning and ticked "fill applications" in the
        afternoon gets a run that refuses to do anything at all.

        Args:
            account: The platform account
            limit: Most jobs to return
            for_applications: Whether the run is queueing applications. If so,
                a job with documents but no application still has work left;
                if not, having documents is the whole job.

        Returns:
            Eligible jobs, best fit first
        """
        from job_agent.models.database import Application, DocumentVersion

        if limit <= 0:
            return []

        if for_applications:
            done = self.db_session.query(Application.job_id).distinct()
        else:
            done = self.db_session.query(DocumentVersion.job_id).distinct()

        return (
            self.db_session.query(Job)
            .filter(
                Job.platform == account.platform,
                Job.hard_filter_pass == True,  # noqa: E712
                Job.fit_score >= settings.fit_score_threshold,
                Job.id.notin_(done),
            )
            .order_by(Job.fit_score.desc())
            .limit(limit)
            .all()
        )

    # ------------------------------------------------------------------
    # Per-platform run
    # ------------------------------------------------------------------

    async def _run_platform(
        self,
        account: PlatformAccount,
        search_profile: SearchProfile,
        generate_documents: bool,
        queue_applications: bool = False,
    ) -> PlatformOutcome:
        """
        Run the pipeline on one platform, containing any failure to it.

        Args:
            account: Platform to run
            search_profile: Search filters
            generate_documents: Whether to tailor documents afterwards
            queue_applications: Whether to fill and queue applications

        Returns:
            PlatformOutcome
        """
        outcome = PlatformOutcome(platform=account.platform)

        skip_reason = self._skip_reason(account)

        if skip_reason:
            outcome.skip_reason = skip_reason
            logger.info(f"Skipping {account.platform}: {skip_reason}")

            self._log(
                AuditAction.PLATFORM_SKIPPED,
                f"Skipped {account.platform}: {skip_reason}",
                {"platform": account.platform, "reason": skip_reason},
                platform=account.platform,
                result="paused",
            )
            return outcome

        preparing = generate_documents or queue_applications
        budget_reason = self._search_budget_reason(account)

        if budget_reason and not preparing:
            outcome.skip_reason = budget_reason
            logger.info(f"Skipping {account.platform}: {budget_reason}")

            self._log(
                AuditAction.PLATFORM_SKIPPED,
                f"Skipped {account.platform}: {budget_reason}",
                {"platform": account.platform, "reason": budget_reason},
                platform=account.platform,
                result="paused",
            )
            return outcome

        try:
            if budget_reason:
                # Out of searching for today, but there is prepared work to do
                # on what earlier runs already found.
                outcome.ran = True
                outcome.errors.append(
                    f"{account.platform}: {budget_reason} — preparing the jobs "
                    f"already found instead of searching for more"
                )
                eligible = self._unprepared_jobs(
                    account,
                    self._remaining_apply_budget(account),
                    for_applications=queue_applications,
                )
            else:
                pipeline = SearchPipeline(self.db_session)
                result = await pipeline.search(account, search_profile)

                outcome.ran = True
                outcome.jobs_found = result.jobs_found
                outcome.new_jobs = result.new_jobs
                outcome.duplicates_skipped = result.duplicates_skipped
                outcome.hard_filters_failed = result.hard_filters_failed
                outcome.errors = list(result.errors)

                # A run that produced nothing may have hit a wall rather than an
                # empty board; check before reporting "0 jobs" as a clean result.
                if not result.jobs_found:
                    interruption = await self._check_for_interruption(account)
                    if interruption:
                        outcome.interruption = interruption
                        return outcome

                eligible = self._eligible_jobs(result.jobs_stored)

                if preparing:
                    # A board rarely changes between runs, so a second run
                    # finds nothing new and — preparing only what it just
                    # found — would do nothing at all. The user who has
                    # ticked "fill applications" means the postings on the
                    # wire, not only the ones discovered in the last minute.
                    already_seen = {job.id for job in eligible}
                    room = self._remaining_apply_budget(account) - len(eligible)

                    eligible += [
                        job
                        for job in self._unprepared_jobs(
                            account, room, for_applications=queue_applications
                        )
                        if job.id not in already_seen
                    ]

            if generate_documents and eligible:
                outcome.documents_generated = await self._generate_documents(eligible)

            # Queue against the eligible jobs, not against how many documents
            # this run happened to write: a job documented by an earlier run
            # is ready to apply for, and gating on a fresh write left those
            # jobs stuck with documents and no application forever.
            if queue_applications and eligible:
                outcome.applications_queued = await self._queue_applications(
                    account, eligible, outcome
                )

        except Exception as e:
            message = f"{account.platform} failed: {e}"
            logger.error(message)
            outcome.errors.append(message)

            interruption = await self._check_for_interruption(account)
            if interruption:
                outcome.interruption = interruption

        return outcome

    async def _check_for_interruption(self, account: PlatformAccount) -> Optional[dict]:
        """
        Look for a CAPTCHA, MFA prompt or sign-in wall on the platform's page.

        A platform that hit one is marked so later runs skip it until the user
        resolves it — retrying into a CAPTCHA is what escalates a challenge
        into a blocked account.

        Args:
            account: The platform account

        Returns:
            Serialized interruption, or None
        """
        try:
            from job_agent.core.session_manager import get_session_manager

            session_manager = await get_session_manager()
            page = await session_manager.get_page(account.platform)

            if not page:
                return None

            interruption = await InterruptionDetector.detect(page)

            if not interruption:
                return None

            await InterruptionDetector.capture(
                page, interruption, settings.documents_dir / "interruptions"
            )

            # Persist it as a task for the user, not just a line in this run
            from job_agent.services.session_monitor import SessionMonitor

            SessionMonitor(self.db_session).record(account, interruption)

            self._log(
                AuditAction.RUN_INTERRUPTED,
                f"{account.platform} interrupted by {interruption.kind.value} — "
                f"{interruption.guidance}",
                {"platform": account.platform, **interruption.to_dict()},
                platform=account.platform,
                result="paused",
            )

            return interruption.to_dict()

        except Exception as e:
            logger.debug(f"Could not check {account.platform} for interruptions: {e}")
            return None

    @staticmethod
    def _eligible_jobs(jobs: List[Job]) -> List[Job]:
        """
        Jobs worth preparing an application for.

        Args:
            jobs: Jobs stored by this run

        Returns:
            Jobs that passed the hard filters and scored above the threshold
        """
        return [
            job for job in jobs
            if job.hard_filter_pass
            and (job.fit_score or 0) >= settings.fit_score_threshold
        ]

    async def _generate_documents(self, jobs: List[Job]) -> int:
        """
        Tailor documents for jobs that passed the hard filters.

        Args:
            jobs: Eligible jobs

        Returns:
            Number of jobs documents were generated for
        """
        from job_agent.models.database import DocumentType
        from job_agent.services.document_builder import DocumentBuilder

        builder = DocumentBuilder(self.db_session)

        if not builder.get_active_master(DocumentType.RESUME):
            logger.info("No master resume uploaded — skipping document generation")
            return 0

        generated = 0

        for job in jobs:
            try:
                await builder.build_application_package(job)
                generated += 1
            except Exception as e:
                logger.error(f"Could not generate documents for job {job.id}: {e}")
                continue

        logger.info(f"Generated documents for {generated} of {len(jobs)} eligible job(s)")

        return generated

    async def _queue_applications(
        self,
        account: PlatformAccount,
        jobs: List[Job],
        outcome: PlatformOutcome,
    ) -> int:
        """
        Open, fill and queue applications for review.

        This is the furthest an unattended run goes. Forms are filled using the
        Phase 5 classifier — which leaves demographic and compensation
        questions blank — screenshotted, and queued. Nothing is submitted.

        Stops at `daily_apply_limit`, and stops entirely if the platform throws
        up a CAPTCHA partway: repeatedly opening application forms into a
        challenge is how a session gets flagged.

        Args:
            account: Platform to apply on
            jobs: Eligible jobs, already documented
            outcome: Outcome collecting errors

        Returns:
            Number of applications queued
        """
        from job_agent.models.database import CandidateProfile, DocumentType, DocumentVersion
        from job_agent.services.application_filler import ApplicationFiller

        connector = create_connector_for_account(account)

        if not connector or not connector.capabilities.can_fill_standard_fields:
            logger.info(
                f"{account.platform} cannot fill application forms — "
                f"documents were prepared but nothing was queued"
            )
            return 0

        profile = (
            self.db_session.query(CandidateProfile)
            .filter(CandidateProfile.is_active == True)  # noqa: E712
            .first()
        )

        if not profile:
            outcome.errors.append(
                "No candidate profile — create one before queueing applications"
            )
            return 0

        budget = self._remaining_apply_budget(account)

        if budget <= 0:
            logger.info(f"{account.platform}: daily apply limit already reached")
            return 0

        from job_agent.core.session_manager import get_session_manager

        session_manager = await get_session_manager()

        # A public job board has nothing to sign into. Demanding a saved
        # sign-in before opening a browser for one turned "prepare this
        # application" into "not connected" on boards that were never meant to
        # be connected — the same fix the submit path already carries.
        page = await session_manager.get_page(
            account.platform,
            needs_signin=connector.capabilities.requires_manual_signin,
        )

        if not page:
            outcome.errors.append(
                f"No browser session for {account.platform}"
                + (
                    " — connect it under Stations"
                    if connector.capabilities.requires_manual_signin
                    else " and the browser could not be opened; see the log"
                )
                + "; documents were prepared but nothing was queued"
            )
            return 0

        connector.set_page(page)
        filler = ApplicationFiller(self.db_session)
        queued = 0

        for job in jobs[:budget]:
            try:
                documents = self._documents_for(job)

                if not documents.get("resume"):
                    logger.info(f"No tailored resume for job {job.id}; skipping")
                    continue

                posting = self._posting_for(job)

                app_session = await connector.begin_application(posting)

                # An application form is a natural place to meet a challenge.
                # Two very different things look alike here: a wall standing
                # between the agent and the page, and a tick-box that is part
                # of the form itself. The first means back off — pushing at it
                # is what gets a session flagged. The second is just one more
                # question only the user can answer, and abandoning the whole
                # application over it wastes the tailored documents and leaves
                # the user with nothing to review.
                interruption = await InterruptionDetector.detect(page)
                needs_challenge = False

                if interruption:
                    if await self._form_is_reachable(page):
                        needs_challenge = True
                        logger.info(
                            f"{account.platform}: a challenge sits on the form for "
                            f"job {job.id} — filling it and leaving the challenge "
                            f"for the user"
                        )
                    else:
                        outcome.interruption = interruption.to_dict()
                        await self._pause_platform(account, interruption)
                        break

                app_session = await connector.fill_application(
                    app_session, profile,
                    {
                        "db_session": self.db_session,
                        # The posting and the resume, so open written questions
                        # come back with a draft instead of an empty box.
                        "job": job,
                        "master_text": self._master_resume_text(),
                        **documents,
                    },
                )

                if needs_challenge:
                    # Surfaced as an unanswered required question, so the tray
                    # blocks release until the user has dealt with it.
                    state = app_session.form_state or {}
                    state.setdefault("required_unanswered", []).append(
                        "Complete the “I'm not a robot” check on the form yourself "
                        "before submitting — the agent will not do it for you."
                    )
                    app_session.form_state = state

                fill_outcome = type("Outcome", (), {
                    "filled_fields": app_session.filled_fields,
                    "deferred_fields": app_session.deferred_fields,
                    "screenshot_path": app_session.screenshot_path,
                    "errors": (app_session.form_state or {}).get("errors", []),
                    "required_deferred": (app_session.form_state or {}).get(
                        "required_unanswered", []),
                })()

                resume_version = self.db_session.query(DocumentVersion).filter(
                    DocumentVersion.job_id == job.id,
                    DocumentVersion.doc_type == DocumentType.RESUME,
                ).order_by(DocumentVersion.created_at.desc()).first()

                cover_version = self.db_session.query(DocumentVersion).filter(
                    DocumentVersion.job_id == job.id,
                    DocumentVersion.doc_type == DocumentType.COVER_LETTER,
                ).order_by(DocumentVersion.created_at.desc()).first()

                filler.queue_for_review(
                    job, account, profile, fill_outcome,
                    form_url=app_session.form_url or "",
                    resume_version=resume_version,
                    cover_letter_version=cover_version,
                )

                queued += 1

            except Exception as e:
                message = f"Could not queue an application for job {job.id}: {e}"
                logger.error(message)
                outcome.errors.append(message)
                continue

        logger.info(f"{account.platform}: queued {queued} application(s) for review")

        return queued

    @staticmethod
    async def _form_is_reachable(page: Any) -> bool:
        """
        Whether an application form is actually on screen behind a challenge.

        A bot-check interstitial replaces the page — there is nothing to fill.
        A CAPTCHA that guards a submit button sits alongside a full form. The
        presence of the form's own fields is what tells them apart.

        Args:
            page: The Playwright page

        Returns:
            True if there is a form worth filling
        """
        try:
            fields = await page.eval_on_selector_all(
                "input[type='text'], input[type='email'], input[type='file'], textarea",
                "els => els.length",
            )
        except Exception:
            return False

        # A login box has a couple of inputs; an application form has many.
        return fields >= 4

    def _master_resume_text(self) -> str:
        """
        The active master resume's text, or empty if none is uploaded.

        Returns:
            The resume text a drafted answer may draw facts from
        """
        from job_agent.models.database import DocumentType, MasterDocument

        master = (
            self.db_session.query(MasterDocument)
            .filter(
                MasterDocument.doc_type == DocumentType.RESUME,
                MasterDocument.is_active == True,  # noqa: E712
            )
            .first()
        )

        return (master.content_text or "") if master else ""

    def _documents_for(self, job: Job) -> Dict[str, str]:
        """
        Find the rendered PDFs to attach for a job.

        Args:
            job: The job

        Returns:
            {"resume": path, "cover_letter": path} for whatever exists
        """
        from job_agent.models.database import DocumentType, DocumentVersion

        documents: Dict[str, str] = {}

        for doc_type, key in (
            (DocumentType.RESUME, "resume"),
            (DocumentType.COVER_LETTER, "cover_letter"),
        ):
            version = (
                self.db_session.query(DocumentVersion)
                .filter(
                    DocumentVersion.job_id == job.id,
                    DocumentVersion.doc_type == doc_type,
                )
                .order_by(DocumentVersion.created_at.desc())
                .first()
            )

            if version and version.pdf_path:
                documents[key] = version.pdf_path

        return documents

    @staticmethod
    def _posting_for(job: Job) -> Any:
        """Build the JobPosting a connector expects from a stored Job."""
        from job_agent.connectors.base import JobPosting

        return JobPosting(
            platform=job.platform,
            external_id=job.external_id,
            title=job.title,
            company=job.company,
            location=job.location,
            description=job.description,
            apply_url=(job.raw_data or {}).get("url"),
        )

    def _remaining_apply_budget(self, account: PlatformAccount) -> int:
        """
        How many more applications may be prepared for this platform today.

        Counts everything queued or submitted today, not just submissions:
        forty drafts waiting for review is its own kind of overreach.

        Args:
            account: The platform account

        Returns:
            Remaining budget, never negative
        """
        from job_agent.models.database import Application

        limit = account.daily_apply_limit or 0

        if limit <= 0:
            return 0

        midnight = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

        used = (
            self.db_session.query(Application)
            .filter(
                Application.platform_account_id == account.id,
                Application.created_at >= midnight,
            )
            .count()
        )

        return max(0, limit - used)

    async def _pause_platform(self, account: PlatformAccount, interruption) -> None:
        """
        Mark a platform paused after hitting a wall mid-run.

        Args:
            account: The platform account
            interruption: The detected interruption
        """
        from job_agent.services.session_monitor import SessionMonitor

        SessionMonitor(self.db_session).record(account, interruption)

        self._log(
            AuditAction.RUN_INTERRUPTED,
            f"{account.platform} paused mid-application by "
            f"{interruption.kind.value} — {interruption.guidance}",
            {"platform": account.platform, **interruption.to_dict()},
            platform=account.platform,
            result="paused",
        )

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def _finish(
        self,
        run: AgentRun,
        outcomes: List[PlatformOutcome],
    ) -> AgentRun:
        """
        Aggregate the per-platform outcomes into the run record.

        Args:
            run: The run being finished
            outcomes: Per-platform results

        Returns:
            The completed run
        """
        run.platforms_run = [o.platform for o in outcomes if o.ran]
        run.platforms_skipped = {
            o.platform: o.skip_reason for o in outcomes if o.skip_reason
        }
        run.jobs_found = sum(o.jobs_found for o in outcomes)
        run.new_jobs = sum(o.new_jobs for o in outcomes)
        run.duplicates_skipped = sum(o.duplicates_skipped for o in outcomes)
        run.hard_filters_failed = sum(o.hard_filters_failed for o in outcomes)
        run.documents_generated = sum(o.documents_generated for o in outcomes)
        run.applications_queued = sum(o.applications_queued for o in outcomes)
        run.applications_submitted = 0  # This loop never submits

        run.interruptions = [o.interruption for o in outcomes if o.interruption]
        run.errors = [error for o in outcomes for error in o.errors]

        run.finished_at = utcnow()

        if run.interruptions or run.errors:
            run.status = RunStatus.PARTIAL
        elif not run.platforms_run:
            # Nothing was searched — because every platform was skipped, or
            # because none is connected at all. Reporting that as "completed,
            # 0 jobs found" reads as "there are no jobs for you", which sends
            # the user off rewriting a search profile that was never used.
            run.status = RunStatus.FAILED
        else:
            run.status = RunStatus.COMPLETED

        self.db_session.commit()
        self.db_session.refresh(run)

        self._log(
            AuditAction.RUN_COMPLETED,
            self.summarize(run),
            {"run_id": run.id, **self.summary_dict(run)},
            result="success" if run.status == RunStatus.COMPLETED else "partial",
        )

        logger.info(self.summarize(run))

        return run

    @staticmethod
    def summarize(run: AgentRun) -> str:
        """
        Build the one-line run summary.

        Args:
            run: The run

        Returns:
            Human-readable summary
        """
        # No platform ran, and none was even skipped: there was nothing to
        # search. Say so instead of reporting a tally of zeroes.
        if not run.platforms_run and not run.platforms_skipped:
            return (
                f"Run #{run.id} searched nothing — no platforms are connected. "
                f"Connect one under Stations, then run again."
            )

        # Only what actually happened. A list of zeroes on every line —
        # "0 jobs found, 0 new, 0 duplicates" — is the same length as a real
        # result and has to be read just as carefully to find out it says
        # nothing.
        parts = [f"Run #{run.id} {run.status.value}"]

        if run.jobs_found:
            parts.append(f"{run.jobs_found} jobs found")
        if run.new_jobs:
            parts.append(f"{run.new_jobs} new")
        if run.duplicates_skipped:
            parts.append(f"{run.duplicates_skipped} duplicates")
        if run.hard_filters_failed:
            parts.append(f"{run.hard_filters_failed} filtered out")
        if run.documents_generated:
            parts.append(f"{run.documents_generated} document sets")
        if run.applications_queued:
            parts.append(f"{run.applications_queued} queued for review")
        if run.interruptions:
            parts.append(f"{len(run.interruptions)} interruption(s)")
        if run.errors:
            parts.append(f"{len(run.errors)} error(s)")

        if len(parts) == 1:
            parts.append("nothing found")

        summary = ", ".join(parts)

        if run.platforms_skipped:
            # The name of a skipped platform is not the useful half. When
            # nothing ran at all, why it was skipped is the whole message.
            if not run.platforms_run:
                summary += " — " + "; ".join(
                    f"{platform}: {reason}"
                    for platform, reason in run.platforms_skipped.items()
                )
            else:
                summary += f" — skipped {', '.join(run.platforms_skipped)}"

        return summary

    @staticmethod
    def summary_dict(run: AgentRun) -> Dict[str, Any]:
        """
        Serialize a run for API responses and audit entries.

        Args:
            run: The run

        Returns:
            Run detail
        """
        return {
            "id": run.id,
            "status": run.status.value,
            "trigger": run.trigger,
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "duration_seconds": run.duration_seconds,
            "platforms_run": run.platforms_run,
            "platforms_skipped": run.platforms_skipped,
            "jobs_found": run.jobs_found,
            "new_jobs": run.new_jobs,
            "duplicates_skipped": run.duplicates_skipped,
            "hard_filters_failed": run.hard_filters_failed,
            "documents_generated": run.documents_generated,
            "applications_queued": run.applications_queued,
            "applications_submitted": run.applications_submitted,
            "interruptions": run.interruptions,
            "errors": run.errors,
            "summary": RunOrchestrator.summarize(run),
        }

    def _log(
        self,
        action: AuditAction,
        detail: str,
        detail_json: dict,
        platform: Optional[str] = None,
        result: str = "success",
    ) -> None:
        """Write an audit entry."""
        self.db_session.add(
            AuditLog(
                timestamp=utcnow(),
                action=action,
                platform=platform,
                actor="agent",
                detail=detail,
                detail_json=detail_json,
                result=result,
            )
        )
        self.db_session.commit()
