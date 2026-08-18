"""
Session Monitor & Recovery (Phase 9).

Phase 8 built the detection half: a run notices a CAPTCHA or an expired session
and pauses that platform. This is the other half — noticing before a run, telling
the user what needs doing, and getting the platform working again afterwards.

Three ideas hold it together:

- **An interruption is a task, not a log line.** `PlatformInterruption` outlives
  the run that hit it, and the platform stays paused until that row is
  resolved. A CAPTCHA that scrolled past in a log is a CAPTCHA nobody handles.
- **Resolution needs evidence.** Either the user says they've handled it, or
  `check_session()` confirms the platform works again. The agent never decides
  on its own that a challenge "probably passed" — retrying into a live
  challenge is what escalates it into a blocked account.
- **Health is per platform.** One platform being broken must never stop the
  others, so everything here is scoped to a single platform.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from job_agent.config import settings
from job_agent.connectors import create_connector_for_account
from job_agent.models.database import (
    AuditAction,
    AuditLog,
    ConnectionStatus,
    Job,
    PlatformAccount,
    PlatformInterruption,
)
from job_agent.services.interruption_detector import Interruption, InterruptionDetector
from job_agent.utils.dates import utcnow

logger = logging.getLogger(__name__)

# Statuses that mean the platform can't be used until someone intervenes
BLOCKED_STATUSES = {
    ConnectionStatus.SESSION_EXPIRED,
    ConnectionStatus.NEEDS_SIGNIN,
    ConnectionStatus.ERROR,
}


@dataclass
class PlatformHealth:
    """The state of one platform."""

    platform: str
    status: str
    healthy: bool
    reason: Optional[str] = None
    open_interruptions: List[dict] = field(default_factory=list)
    needs_reconnect: bool = False
    last_verified_at: Optional[str] = None
    pending_jobs: int = 0
    search_url: Optional[str] = None
    is_custom: bool = False
    # Whether the user said this station has to be signed into. Surfaced so
    # the card can offer to change it: the wrong answer here leaves a public
    # board permanently "needs_signin", with a Reconnect button that cannot
    # help.
    requires_signin: bool = False
    needs_search_url: bool = False
    daily_search_limit: int = 0
    daily_apply_limit: int = 0
    searches_left_today: int = 0

    def to_dict(self) -> dict:
        """Serialize for the dashboard."""
        return {
            "platform": self.platform,
            "status": self.status,
            "healthy": self.healthy,
            "reason": self.reason,
            "open_interruptions": self.open_interruptions,
            "needs_reconnect": self.needs_reconnect,
            "last_verified_at": self.last_verified_at,
            "pending_jobs": self.pending_jobs,
            "search_url": self.search_url,
            "is_custom": self.is_custom,
            "requires_signin": self.requires_signin,
            "needs_search_url": self.needs_search_url,
            "daily_search_limit": self.daily_search_limit,
            "daily_apply_limit": self.daily_apply_limit,
            "searches_left_today": self.searches_left_today,
        }


class SessionMonitor:
    """Watches platform sessions and drives recovery."""

    def __init__(self, db_session: Session):
        """
        Initialize the monitor.

        Args:
            db_session: Database session
        """
        self.db_session = db_session

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self) -> List[PlatformHealth]:
        """
        Report the state of every connected platform.

        Returns:
            One PlatformHealth per platform account
        """
        accounts = self.db_session.query(PlatformAccount).order_by(
            PlatformAccount.platform
        ).all()

        return [self.health_for(account) for account in accounts]

    def health_for(self, account: PlatformAccount) -> PlatformHealth:
        """
        Report the state of one platform.

        Args:
            account: The platform account

        Returns:
            PlatformHealth
        """
        open_interruptions = self.open_interruptions(account.platform)

        blocked = account.status in BLOCKED_STATUSES or bool(open_interruptions)

        reason = None
        if open_interruptions:
            reason = open_interruptions[0].guidance
        elif account.status in BLOCKED_STATUSES:
            reason = account.last_error or f"status is {account.status.value}"

        return PlatformHealth(
            platform=account.platform,
            status=account.status.value,
            healthy=not blocked,
            reason=reason,
            open_interruptions=[self.describe(i) for i in open_interruptions],
            needs_reconnect=blocked,
            last_verified_at=(
                account.last_verified_at.isoformat() if account.last_verified_at else None
            ),
            pending_jobs=self.pending_job_count(account),
            search_url=account.search_url,
            is_custom=bool(account.connector_kind),
            requires_signin=bool(account.requires_signin),
            needs_search_url=self._needs_a_board(account),
            daily_search_limit=account.daily_search_limit,
            daily_apply_limit=account.daily_apply_limit,
            searches_left_today=self._searches_left(account),
        )

    def _searches_left(self, account: PlatformAccount) -> int:
        """
        How many more postings may be collected from this platform today.

        Shown because running out of it looks exactly like a broken station
        otherwise — the run stops and the card gives no hint why.

        Args:
            account: The platform account

        Returns:
            Remaining postings, never negative
        """
        from job_agent.core.search_pipeline import SearchPipeline

        try:
            return max(0, SearchPipeline(self.db_session)._remaining_search_budget(account))
        except Exception:
            return account.daily_search_limit

    @staticmethod
    def _needs_a_board(account: PlatformAccount) -> bool:
        """
        Whether this station is connected but has nowhere to search.

        LinkedIn and Indeed carry their own search URL; Greenhouse and the
        other per-company boards do not, and are inert until told whose jobs
        to read. The connector is the only thing that knows which it is.

        Args:
            account: The platform account

        Returns:
            True if a board URL still has to be supplied
        """
        from job_agent.connectors.generic_ats import GenericATSConnector

        connector = create_connector_for_account(account)

        if connector is None:
            return False

        return isinstance(connector, GenericATSConnector) and not connector.search_url

    def pending_job_count(self, account: PlatformAccount) -> int:
        """
        Count jobs found on a platform that no application exists for yet.

        This is the work that resumes once a platform is healthy again.

        Args:
            account: The platform account

        Returns:
            Number of pending jobs
        """
        from job_agent.models.database import Application

        applied_job_ids = {
            row[0] for row in
            self.db_session.query(Application.job_id)
            .filter(Application.platform_account_id == account.id)
            .all()
        }

        query = self.db_session.query(Job).filter(
            Job.platform == account.platform,
            Job.hard_filter_pass == True,  # noqa: E712
            Job.fit_score >= settings.fit_score_threshold,
        )

        return sum(1 for job in query.all() if job.id not in applied_job_ids)

    # ------------------------------------------------------------------
    # Interruptions
    # ------------------------------------------------------------------

    def open_interruptions(self, platform: Optional[str] = None) -> List[PlatformInterruption]:
        """
        Interruptions still awaiting the user.

        Args:
            platform: Restrict to one platform

        Returns:
            Open interruptions, newest first
        """
        query = self.db_session.query(PlatformInterruption).filter(
            PlatformInterruption.resolved_at.is_(None)
        )

        if platform:
            query = query.filter(PlatformInterruption.platform == platform)

        return query.order_by(PlatformInterruption.detected_at.desc()).all()

    def record(
        self,
        account: PlatformAccount,
        interruption: Interruption,
        run_id: Optional[int] = None,
    ) -> PlatformInterruption:
        """
        Record an interruption and pause the platform.

        An identical open interruption isn't duplicated — three CAPTCHAs on one
        platform is one thing for the user to do, not three.

        Args:
            account: The affected platform
            interruption: What was detected
            run_id: The run that hit it, if any

        Returns:
            The stored (or existing) interruption
        """
        existing = next(
            (
                i for i in self.open_interruptions(account.platform)
                if i.kind == interruption.kind.value
            ),
            None,
        )

        if existing:
            logger.info(
                f"{account.platform} already has an open {interruption.kind.value} "
                f"interruption (#{existing.id})"
            )
            return existing

        record = PlatformInterruption(
            platform=account.platform,
            kind=interruption.kind.value,
            url=interruption.url,
            evidence=interruption.evidence,
            guidance=interruption.guidance,
            screenshot_path=interruption.screenshot_path,
            run_id=run_id,
        )

        self.db_session.add(record)

        account.status = ConnectionStatus.SESSION_EXPIRED
        account.last_error = f"{interruption.kind.value}: {interruption.guidance}"
        account.updated_at = utcnow()

        self.db_session.commit()
        self.db_session.refresh(record)

        self._log(
            AuditAction.INTERRUPTION_RAISED,
            f"{account.platform} paused: {interruption.kind.value} — "
            f"{interruption.guidance}",
            {"interruption_id": record.id, **interruption.to_dict()},
            platform=account.platform,
            result="paused",
        )

        return record

    async def resolve(
        self,
        interruption: PlatformInterruption,
        resolved_by: str = "user",
        verify: bool = True,
    ) -> Dict[str, Any]:
        """
        Mark an interruption handled, verifying where possible.

        When `verify` is set, the platform's session is checked before the
        interruption is closed. A user who says "I've done it" but hasn't would
        otherwise send the next run straight back into the same wall.

        Args:
            interruption: The interruption to close
            resolved_by: "user" or "session_check"
            verify: Confirm the session works before closing

        Returns:
            {"resolved": bool, "verified": bool, "message": str}
        """
        account = self._account_for(interruption.platform)

        if not account:
            return {
                "resolved": False, "verified": False,
                "message": f"{interruption.platform} is not connected",
            }

        verified = False

        if verify:
            check = await self.check_session(account)
            verified = check["healthy"]

            if not verified:
                return {
                    "resolved": False,
                    "verified": False,
                    "message": (
                        f"{interruption.platform} still looks blocked "
                        f"({check['reason']}). Resolve it in the browser first, or "
                        f"resolve without verification if you're sure."
                    ),
                }

        interruption.resolved_at = utcnow()
        interruption.resolved_by = resolved_by
        interruption.resolution_note = (
            "Session verified after resolution" if verified
            else "Marked resolved without verification"
        )

        # Only clear the platform once nothing else is blocking it
        remaining = [
            i for i in self.open_interruptions(interruption.platform)
            if i.id != interruption.id
        ]

        if not remaining:
            account.status = ConnectionStatus.CONNECTED
            account.last_error = None
            account.last_verified_at = utcnow() if verified else account.last_verified_at
            account.updated_at = utcnow()

        self.db_session.commit()

        self._log(
            AuditAction.INTERRUPTION_RESOLVED,
            f"{interruption.platform} interruption #{interruption.id} "
            f"({interruption.kind}) resolved by {resolved_by}"
            + ("" if verified else " without verification"),
            {
                "interruption_id": interruption.id,
                "verified": verified,
                "remaining_open": len(remaining),
            },
            platform=interruption.platform,
            actor=resolved_by,
        )

        return {
            "resolved": True,
            "verified": verified,
            "message": (
                f"{interruption.platform} is available again"
                if not remaining
                else f"{len(remaining)} other interruption(s) still open"
            ),
        }

    # ------------------------------------------------------------------
    # Session checking and reconnect
    # ------------------------------------------------------------------

    async def check_session(self, account: PlatformAccount) -> Dict[str, Any]:
        """
        Check whether a platform's session is usable.

        Looks for logged-in state through the connector and, on the live page,
        for a wall the agent shouldn't push through.

        Args:
            account: The platform account

        Returns:
            {"healthy": bool, "reason": str, "interruption": dict | None}
        """
        connector = create_connector_for_account(account)

        if not connector:
            return {
                "healthy": False,
                "reason": f"no connector registered for {account.platform}",
                "interruption": None,
            }

        try:
            from job_agent.core.session_manager import get_session_manager

            session_manager = await get_session_manager()
            page = await session_manager.get_page(
                account.platform,
                needs_signin=connector.capabilities.requires_manual_signin,
            )
        except Exception as e:
            return {
                "healthy": False,
                "reason": f"could not open a browser session: {e}",
                "interruption": None,
            }

        if not page:
            return {
                "healthy": False,
                "reason": "no authenticated browser session",
                "interruption": None,
            }

        connector.set_page(page)

        interruption = await InterruptionDetector.detect(page)

        if interruption:
            await InterruptionDetector.capture(
                page, interruption, settings.documents_dir / "interruptions"
            )
            return {
                "healthy": False,
                "reason": f"{interruption.kind.value}: {interruption.guidance}",
                "interruption": interruption,
            }

        try:
            status = await connector.check_session()
        except Exception as e:
            return {
                "healthy": False,
                "reason": f"session check failed: {e}",
                "interruption": None,
            }

        healthy = str(status).lower() in ("connected", "connectionstatus.connected")

        return {
            "healthy": healthy,
            "reason": None if healthy else f"connector reports '{status}'",
            "interruption": None,
        }

    async def monitor(self, platform: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Check every platform and record anything newly wrong.

        Args:
            platform: Restrict to one platform

        Returns:
            One result per platform checked
        """
        query = self.db_session.query(PlatformAccount)

        if platform:
            query = query.filter(PlatformAccount.platform == platform)

        results = []

        for account in query.order_by(PlatformAccount.platform).all():
            check = await self.check_session(account)

            if check["interruption"]:
                self.record(account, check["interruption"])
            elif check["healthy"]:
                self._mark_healthy(account)
            else:
                account.last_error = check["reason"]
                account.updated_at = utcnow()
                self.db_session.commit()

            results.append({
                "platform": account.platform,
                "healthy": check["healthy"],
                "reason": check["reason"],
            })

        return results

    def _mark_healthy(self, account: PlatformAccount) -> None:
        """Record that a platform checked out fine."""
        was_blocked = account.status in BLOCKED_STATUSES

        account.status = ConnectionStatus.CONNECTED
        account.last_verified_at = utcnow()
        account.last_error = None
        account.updated_at = utcnow()

        self.db_session.commit()

        if was_blocked:
            self._log(
                AuditAction.SESSION_HEALTHY,
                f"{account.platform} is working again",
                {"platform": account.platform},
                platform=account.platform,
            )

    async def reconnect(self, account: PlatformAccount, login_url: Optional[str] = None) -> dict:
        """
        Open a browser window for the user to sign in again.

        The agent opens the window and waits. It never types credentials — the
        user signs in themselves, exactly as in Phase 1.

        Args:
            account: The platform to reconnect
            login_url: Where to open (defaults to the platform's search URL)

        Returns:
            {"opened": bool, "url": str | None, "message": str}
        """
        try:
            from job_agent.core.session_manager import get_session_manager

            session_manager = await get_session_manager()
            context = await session_manager.launch_browser_for_connection(account.platform)
        except Exception as e:
            return {
                "opened": False, "url": None,
                "message": f"Could not open a browser for {account.platform}: {e}",
            }

        connector = create_connector_for_account(account)
        target = login_url or getattr(connector, "search_url", None) or account.search_url

        page = context.pages[0] if getattr(context, "pages", None) else None

        if page and target:
            try:
                await page.goto(target, timeout=30000)
            except Exception as e:
                logger.warning(f"Could not navigate to {target}: {e}")

        account.status = ConnectionStatus.NEEDS_SIGNIN
        account.updated_at = utcnow()
        self.db_session.commit()

        self._log(
            AuditAction.PLATFORM_RECONNECTED,
            f"Opened a browser window for the user to sign in to {account.platform}",
            {"platform": account.platform, "url": target},
            platform=account.platform,
            actor="user",
            result="paused",
        )

        return {
            "opened": True,
            "url": target,
            "message": (
                f"Sign in to {account.platform} in the open browser window, then "
                f"confirm — the agent never enters credentials for you."
            ),
        }

    async def resume(self, account: PlatformAccount) -> dict:
        """
        Bring a platform back into service once its session is healthy.

        Args:
            account: The platform to resume

        Returns:
            What was unblocked, and what work is waiting
        """
        check = await self.check_session(account)

        if not check["healthy"]:
            if check["interruption"]:
                self.record(account, check["interruption"])

            return {
                "resumed": False,
                "platform": account.platform,
                "reason": check["reason"],
                "pending_jobs": self.pending_job_count(account),
            }

        closed = 0
        for interruption in self.open_interruptions(account.platform):
            interruption.resolved_at = utcnow()
            interruption.resolved_by = "session_check"
            interruption.resolution_note = "Session verified healthy on resume"
            closed += 1

        self._mark_healthy(account)

        pending = self.pending_job_count(account)

        self._log(
            AuditAction.PLATFORM_RESUMED,
            f"{account.platform} resumed — {closed} interruption(s) cleared, "
            f"{pending} job(s) waiting",
            {
                "platform": account.platform,
                "interruptions_closed": closed,
                "pending_jobs": pending,
            },
            platform=account.platform,
        )

        return {
            "resumed": True,
            "platform": account.platform,
            "interruptions_closed": closed,
            "pending_jobs": pending,
            "next_step": (
                f"{pending} job(s) are waiting on {account.platform}. Start a run to "
                f"prepare them."
            ) if pending else "Nothing is waiting on this platform.",
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _account_for(self, platform: str) -> Optional[PlatformAccount]:
        """Look up a platform account by name."""
        return (
            self.db_session.query(PlatformAccount)
            .filter(PlatformAccount.platform == platform)
            .first()
        )

    @staticmethod
    def describe(interruption: PlatformInterruption) -> dict:
        """
        Serialize an interruption for the dashboard.

        Args:
            interruption: The record

        Returns:
            Interruption detail
        """
        return {
            "id": interruption.id,
            "platform": interruption.platform,
            "kind": interruption.kind,
            "detected_at": interruption.detected_at.isoformat(),
            "url": interruption.url,
            "guidance": interruption.guidance,
            "evidence": interruption.evidence,
            "has_screenshot": bool(interruption.screenshot_path),
            "is_open": interruption.is_open,
            "resolved_at": (
                interruption.resolved_at.isoformat() if interruption.resolved_at else None
            ),
            "resolved_by": interruption.resolved_by,
            "run_id": interruption.run_id,
        }

    def _log(
        self,
        action: AuditAction,
        detail: str,
        detail_json: dict,
        platform: Optional[str] = None,
        actor: str = "agent",
        result: str = "success",
    ) -> None:
        """Write an audit entry."""
        self.db_session.add(
            AuditLog(
                timestamp=utcnow(),
                action=action,
                platform=platform,
                actor=actor,
                detail=detail,
                detail_json=detail_json,
                result=result,
            )
        )
        self.db_session.commit()
