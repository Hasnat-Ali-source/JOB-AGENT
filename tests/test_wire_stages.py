"""
What the wire says about a posting, and what it lets you do next.

The bug: discarding an application left the posting showing as untouched
("Found") while its row still pointed at the discarded application. So the
wire offered to open something the tray did not hold, and preparing the job
again was refused outright — discarding a posting silently meant never again.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from job_agent.dashboard.deps import get_session
from job_agent.dashboard.main import app
from job_agent.models.database import (
    Application,
    ApplicationStatus,
    AutomationMode,
    ConnectionStatus,
    Job,
    PlatformAccount,
)
from job_agent.utils.dates import utcnow


@pytest.fixture
def client():
    """A dashboard client over an in-memory database."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)

    with Session(engine) as db:
        app.dependency_overrides[get_session] = lambda: db

        account = PlatformAccount(
            platform="greenhouse",
            status=ConnectionStatus.CONNECTED,
            profile_dir="/tmp/unused",
            automation_mode=AutomationMode.SEARCH_AND_ANALYZE,
        )
        db.add(account)
        db.commit()
        db.refresh(account)

        test_client = TestClient(app)
        test_client.db = db
        test_client.account = account

        yield test_client

        app.dependency_overrides.clear()


def _job(client, external_id="REQ-1") -> Job:
    record = Job(
        platform="greenhouse",
        external_id=external_id,
        title="Support Engineer",
        company="Acme",
        location="Remote",
        url=f"https://acme.test/{external_id}",
        description="Handle customer escalations end to end.",
        apply_method="web_form",
        dedup_hash=external_id,
        first_seen_at=utcnow(),
        hard_filter_pass=True,
        fit_score=0.7,
    )
    client.db.add(record)
    client.db.commit()
    client.db.refresh(record)

    return record


def _application(client, job, status) -> Application:
    record = Application(
        job_id=job.id,
        platform_account_id=client.account.id,
        submission_status=status,
        filled_fields={},
        deferred_fields={},
    )
    client.db.add(record)
    client.db.commit()
    client.db.refresh(record)

    return record


def _row(client, job_id) -> dict:
    response = client.get("/api/v1/jobs", params={"limit": 50})

    return next(row for row in response.json()["jobs"] if row["id"] == job_id)


class TestStage:
    """The stage a posting reports, and whether it points anywhere."""

    def test_an_untouched_posting_is_found(self, client):
        job = _job(client)

        assert _row(client, job.id)["stage"] == "found"

    def test_a_waiting_application_shows_and_links(self, client):
        job = _job(client)
        application = _application(client, job, ApplicationStatus.QUEUED_FOR_REVIEW)

        row = _row(client, job.id)

        assert row["stage"] == "queued_for_review"
        assert row["application_id"] == application.id

    def test_a_discarded_posting_says_discarded(self, client):
        """It read as "Found" — the stage map had no entry for it."""
        job = _job(client)
        _application(client, job, ApplicationStatus.REJECTED)

        assert _row(client, job.id)["stage"] == "discarded"

    def test_a_discarded_posting_offers_no_dead_link(self, client):
        """
        With an application_id set, the row offered "Open in tray" — and the
        tray lists only what is waiting, so the button went nowhere.
        """
        job = _job(client)
        _application(client, job, ApplicationStatus.REJECTED)

        assert _row(client, job.id)["application_id"] is None

    def test_a_live_application_outranks_a_discarded_one(self, client):
        """
        A posting can carry both: discarded once, prepared again. The live one
        is what the wire is for.
        """
        job = _job(client)
        _application(client, job, ApplicationStatus.REJECTED)
        live = _application(client, job, ApplicationStatus.QUEUED_FOR_REVIEW)

        row = _row(client, job.id)

        assert row["stage"] == "queued_for_review"
        assert row["application_id"] == live.id

    def test_a_withdrawn_application_reads_the_same_as_a_discarded_one(self, client):
        job = _job(client)
        _application(client, job, ApplicationStatus.WITHDRAWN)

        assert _row(client, job.id)["stage"] == "discarded"


class TestPreparingAgain:
    """Discarding means "not this one, not yet" — never "never again"."""

    def test_a_live_application_blocks_a_second(self, client):
        job = _job(client)
        _application(client, job, ApplicationStatus.QUEUED_FOR_REVIEW)

        response = client.post(f"/api/v1/jobs/{job.id}/prepare")

        assert response.status_code == 409
        assert "already exists" in response.json()["detail"]

    def test_a_submitted_application_blocks_a_second(self, client):
        job = _job(client)
        _application(client, job, ApplicationStatus.SUBMITTED)

        assert client.post(f"/api/v1/jobs/{job.id}/prepare").status_code == 409

    @pytest.mark.parametrize(
        "status", [ApplicationStatus.REJECTED, ApplicationStatus.WITHDRAWN]
    )
    def test_a_discarded_application_does_not_block(self, client, status):
        """
        It got as far as refusing outright. Now it gets past that check and
        fails later for want of a master resume in this fixture — which is a
        different, honest reason.
        """
        job = _job(client)
        _application(client, job, status)

        response = client.post(f"/api/v1/jobs/{job.id}/prepare")

        assert "already exists" not in response.text
