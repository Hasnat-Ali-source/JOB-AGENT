#!/usr/bin/env python3
"""
Tests for Phase 10: Audit Log & Export.

Covers the Phase 10 acceptance criteria:
- Export applications as CSV with job_id, company, title, submitted_at, status,
  confirmed_ref
- The audit log shows every action, filterable by platform, date and action type

Plus the property that makes an export worth having: it must survive a round
trip through a CSV reader, including job descriptions with commas and newlines
in them.
"""

import csv
import io
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from job_agent.dashboard.deps import get_session
from job_agent.dashboard.main import app
from job_agent.models.database import (
    AgentRun,
    Application,
    ApplicationStatus,
    AuditAction,
    AuditLog,
    AutomationMode,
    DocumentFormat,
    DocumentType,
    DocumentVersion,
    EmailDraft,
    EmailDraftStatus,
    Job,
    MasterDocument,
    PlatformAccount,
    RunStatus,
)
from job_agent.services.exporter import Exporter
from job_agent.utils.dates import utcnow


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def session():
    """In-memory database."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)

    with Session(engine) as db_session:
        yield db_session


@pytest.fixture
def client(session):
    """TestClient sharing the same session."""
    app.dependency_overrides[get_session] = lambda: session

    with TestClient(app) as test_client:
        test_client.db = session  # type: ignore[attr-defined]
        yield test_client

    app.dependency_overrides.clear()


@pytest.fixture
def seeded(session):
    """A realistic slice of data across every exportable table."""
    account = PlatformAccount(
        platform="greenhouse", profile_dir="/tmp/a",
        automation_mode=AutomationMode.SEARCH_AND_PREPARE,
        daily_search_limit=50, daily_apply_limit=5, clean_submissions_count=2,
    )
    session.add(account)
    session.commit()
    session.refresh(account)

    # A description with a comma and a newline — the things that break CSV
    job = Job(
        platform="greenhouse", external_id="REQ-1",
        title="Senior Backend Engineer", company="Acme Robotics", location="Remote",
        description="Python, PostgreSQL and Kafka.\nSecond line, with a comma.",
        salary="USD 180,000 - 210,000", apply_method="web_form", dedup_hash="h1",
        fit_score=0.87, hard_filter_pass=True,
        raw_data={"url": "https://job-boards.greenhouse.io/acme/jobs/1"},
    )
    filtered = Job(
        platform="greenhouse", external_id="REQ-2", title="Junior Role",
        company="CheapCo", location="Remote", description="d",
        apply_method="web_form", dedup_hash="h2",
        fit_score=0.3, hard_filter_pass=False, status="filtered_out",
    )
    session.add_all([job, filtered])
    session.commit()
    session.refresh(job)

    master = MasterDocument(
        doc_type=DocumentType.RESUME, name="m", source_path="/tmp/m.txt",
        source_format=DocumentFormat.TXT, content_text="x",
    )
    session.add(master)
    session.commit()
    session.refresh(master)

    version = DocumentVersion(
        master_document_id=master.id, job_id=job.id, doc_type=DocumentType.RESUME,
        content_text="t", pdf_path="/tmp/r.pdf", generator="deterministic",
    )
    session.add(version)
    session.commit()
    session.refresh(version)

    application = Application(
        job_id=job.id, platform_account_id=account.id,
        resume_version_id=version.id, resume_version="/tmp/r.pdf",
        filled_fields={"Full name": {"value": "Alex Rivera"}},
        deferred_fields={
            "Expected salary": {
                "category": "sensitive", "required": True,
                "value_entered_by_user": "180000",
            },
            "Why us?": {
                "category": "unknown", "required": True, "value_entered_by_user": None,
            },
        },
        submission_status=ApplicationStatus.SUBMITTED,
        submitted_at=utcnow(), confirmation_ref="ACME-88421",
        confirmation_url="https://acme.test/thanks",
        reviewed_by_user=True, filled_at=utcnow(), form_url="https://acme.test/apply",
    )
    session.add(application)

    session.add(AgentRun(
        trigger="scheduled", status=RunStatus.PARTIAL,
        started_at=utcnow(), finished_at=utcnow(),
        platforms_run=["greenhouse"], platforms_skipped={"indeed": "session expired"},
        jobs_found=42, new_jobs=3, duplicates_skipped=2,
        errors=["one posting failed"],
    ))

    session.add(EmailDraft(
        job_id=job.id, to_email="careers@acme.test", subject="Application",
        body="b", status=EmailDraftStatus.SENT, sent_at=utcnow(),
        sent_message_id="<x@test>", send_method="smtp", attachments=["/tmp/r.pdf"],
    ))

    for i, (action, platform, result) in enumerate([
        (AuditAction.SEARCH_RUN, "greenhouse", "success"),
        (AuditAction.JOB_DEDUPED, "greenhouse", "success"),
        (AuditAction.APPLICATION_SUBMITTED, "greenhouse", "success"),
        (AuditAction.RUN_INTERRUPTED, "indeed", "paused"),
        (AuditAction.ERROR_OCCURRED, "indeed", "failure"),
    ]):
        session.add(AuditLog(
            timestamp=utcnow() - timedelta(hours=i),
            action=action, platform=platform, actor="agent",
            detail=f"{action.value} on {platform}",
            detail_json={"collected": 42, "new": 3}, result=result,
        ))

    session.commit()

    return {"job": job, "account": account, "application": application}


def parse_csv(content: str) -> list:
    """Read CSV text back into rows."""
    return list(csv.reader(io.StringIO(content)))


# ============================================================================
# Application export
# ============================================================================

class TestApplicationExport:
    """Acceptance: applications export with the specified columns."""

    def test_required_columns_are_present(self, session, seeded):
        rows = parse_csv(Exporter.to_csv(Exporter(session).applications()))
        header = rows[0]

        for column in ("job_id", "company", "title", "submitted_at", "status",
                       "confirmation_ref"):
            assert column in header

    def test_values_are_exported(self, session, seeded):
        rows = parse_csv(Exporter.to_csv(Exporter(session).applications()))
        header, row = rows[0], rows[1]
        record = dict(zip(header, row))

        assert record["company"] == "Acme Robotics"
        assert record["title"] == "Senior Backend Engineer"
        assert record["status"] == "submitted"
        assert record["confirmation_ref"] == "ACME-88421"
        assert record["submitted_at"]

    def test_unanswered_required_questions_are_surfaced(self, session, seeded):
        rows = parse_csv(Exporter.to_csv(Exporter(session).applications()))
        record = dict(zip(rows[0], rows[1]))

        assert "Why us?" in record["unanswered_required"]
        assert "Expected salary" not in record["unanswered_required"]

    def test_document_verification_is_reported(self, session, seeded):
        rows = parse_csv(Exporter.to_csv(Exporter(session).applications()))
        record = dict(zip(rows[0], rows[1]))

        assert record["documents_verified"] == "yes"

    def test_empty_export_still_has_a_header(self, session):
        rows = parse_csv(Exporter.to_csv(Exporter(session).applications()))

        assert len(rows) == 1
        assert rows[0] == Exporter.APPLICATION_COLUMNS


# ============================================================================
# Job export
# ============================================================================

class TestJobExport:
    """Jobs, including the ones that were filtered out."""

    def test_all_jobs_by_default(self, session, seeded):
        rows = parse_csv(Exporter.to_csv(Exporter(session).jobs()))

        assert len(rows) == 3  # header + 2 jobs

    def test_filtered_jobs_can_be_excluded(self, session, seeded):
        rows = parse_csv(Exporter.to_csv(Exporter(session).jobs(include_filtered=False)))

        assert len(rows) == 2
        assert "CheapCo" not in rows[1]

    def test_commas_and_newlines_survive_the_round_trip(self, session, seeded):
        """
        A description containing a comma and a newline must stay in one cell —
        otherwise one job becomes several rows in a spreadsheet.
        """
        rows = parse_csv(Exporter.to_csv(Exporter(session).jobs()))

        assert len(rows) == 3  # header + exactly 2 jobs, not more

        acme = self._row_for(rows, "Acme Robotics")
        assert acme["salary"] == "USD 180,000 - 210,000"

    def test_url_is_pulled_from_raw_data(self, session, seeded):
        rows = parse_csv(Exporter.to_csv(Exporter(session).jobs()))

        assert self._row_for(rows, "Acme Robotics")["url"].endswith("/acme/jobs/1")

    def test_export_order_is_stable(self, session, seeded):
        """Repeated exports must be byte-identical, or they can't be diffed."""
        first = Exporter.to_csv(Exporter(session).jobs())
        second = Exporter.to_csv(Exporter(session).jobs())

        assert first == second

    @staticmethod
    def _row_for(rows, company: str) -> dict:
        """Find a row by company, so tests don't depend on row order."""
        header = rows[0]
        for row in rows[1:]:
            record = dict(zip(header, row))
            if record["company"] == company:
                return record
        raise AssertionError(f"no exported row for {company}")

    def test_platform_filter(self, session, seeded):
        rows = parse_csv(Exporter.to_csv(Exporter(session).jobs(platform="indeed")))

        assert len(rows) == 1  # header only


# ============================================================================
# Audit export and other tables
# ============================================================================

class TestOtherExports:
    """Audit, runs and emails."""

    def test_audit_export(self, session, seeded):
        rows = parse_csv(Exporter.to_csv(Exporter(session).audit()))

        assert len(rows) == 6  # header + 5 entries
        assert "timestamp" in rows[0]

    def test_audit_export_filters_by_platform(self, session, seeded):
        rows = parse_csv(Exporter.to_csv(Exporter(session).audit(platform="indeed")))

        assert len(rows) == 3  # header + 2 indeed entries

    def test_audit_export_filters_by_time(self, session, seeded):
        since = utcnow() - timedelta(hours=2, minutes=30)
        rows = parse_csv(Exporter.to_csv(Exporter(session).audit(since=since)))

        assert len(rows) == 4  # header + 3 recent entries

    def test_run_export_flattens_lists(self, session, seeded):
        rows = parse_csv(Exporter.to_csv(Exporter(session).runs()))
        record = dict(zip(rows[0], rows[1]))

        assert record["platforms_run"] == "greenhouse"
        assert "indeed: session expired" in record["platforms_skipped"]
        assert record["jobs_found"] == "42"

    def test_email_export(self, session, seeded):
        rows = parse_csv(Exporter.to_csv(Exporter(session).emails()))
        record = dict(zip(rows[0], rows[1]))

        assert record["to_email"] == "careers@acme.test"
        assert record["status"] == "sent"

    def test_unknown_export_is_rejected(self, session):
        with pytest.raises(ValueError, match="Unknown export"):
            list(Exporter(session).rows_for("passwords"))


# ============================================================================
# API
# ============================================================================

class TestAuditApi:
    """GET /api/v1/audit"""

    def test_lists_entries_newest_first(self, client, seeded):
        body = client.get("/api/v1/audit").json()

        assert body["total"] == 5
        assert body["entries"][0]["action"] == "search_run"

    def test_reads_like_a_log_line(self, client, seeded):
        """Acceptance: "2026-08-14 14:32:15 greenhouse search_run ..."."""
        entry = client.get("/api/v1/audit").json()["entries"][0]

        assert " " in entry["timestamp"]  # "YYYY-MM-DD HH:MM:SS"
        assert entry["platform"] == "greenhouse"
        assert entry["detail_json"]["collected"] == 42

    @pytest.mark.parametrize("params,expected", [
        ({"platform": "indeed"}, 2),
        ({"action": "search_run"}, 1),
        ({"result": "paused"}, 1),
        ({"result": "failure"}, 1),
    ])
    def test_filters(self, client, seeded, params, expected):
        assert client.get("/api/v1/audit", params=params).json()["total"] == expected

    def test_pagination(self, client, seeded):
        body = client.get("/api/v1/audit", params={"limit": 2}).json()

        assert body["total"] == 5
        assert body["returned"] == 2

    def test_actions_endpoint_lists_what_exists(self, client, seeded):
        body = client.get("/api/v1/audit/actions").json()

        actions = [a["action"] for a in body["actions"]]
        assert "search_run" in actions
        assert body["platforms"] == ["greenhouse", "indeed"]

    def test_summary_counts_what_needs_attention(self, client, seeded):
        body = client.get("/api/v1/audit/summary").json()

        assert body["total_entries"] == 5
        assert body["by_platform"]["greenhouse"] == 3
        assert body["needs_attention"] == 2  # one paused, one failure


class TestExportApi:
    """GET /api/v1/exports"""

    def test_lists_available_exports(self, client, seeded):
        body = client.get("/api/v1/exports").json()
        names = {e["name"] for e in body["exports"]}

        assert names == {"applications", "jobs", "audit", "runs", "emails"}

    def test_reports_row_counts(self, client, seeded):
        body = client.get("/api/v1/exports").json()
        by_name = {e["name"]: e for e in body["exports"]}

        assert by_name["jobs"]["rows"] == 2
        assert by_name["applications"]["rows"] == 1

    def test_downloads_as_a_csv_attachment(self, client, seeded):
        response = client.get("/api/v1/exports/applications")

        assert response.status_code == 200
        assert "text/csv" in response.headers["content-type"]
        assert "attachment" in response.headers["content-disposition"]
        assert ".csv" in response.headers["content-disposition"]

    def test_downloaded_csv_parses(self, client, seeded):
        rows = parse_csv(client.get("/api/v1/exports/applications").text)

        assert rows[0][:3] == ["application_id", "job_id", "platform"]
        assert len(rows) == 2

    def test_audit_download_respects_filters(self, client, seeded):
        rows = parse_csv(
            client.get("/api/v1/exports/audit", params={"platform": "indeed"}).text)

        assert len(rows) == 3

    def test_unknown_export_returns_404(self, client):
        assert client.get("/api/v1/exports/passwords").status_code == 404


class TestSettingsApi:
    """GET/PATCH /api/v1/settings"""

    def test_shows_platform_limits(self, client, seeded):
        body = client.get("/api/v1/settings").json()
        platform = body["platforms"][0]

        assert platform["platform"] == "greenhouse"
        assert platform["daily_search_limit"] == 50
        assert platform["clean_submissions_count"] == 2

    def test_thresholds_are_read_only(self, client, seeded):
        """
        A running agent must not be able to lower its own review threshold
        through the API.
        """
        body = client.get("/api/v1/settings").json()

        assert body["thresholds"]["editable"] is False
        assert body["thresholds"]["clean_submissions_threshold"] == 3

    def test_platform_limits_can_be_changed(self, client, seeded):
        body = client.patch(
            "/api/v1/settings/platforms/greenhouse",
            json={"daily_search_limit": 25, "daily_apply_limit": 2},
        ).json()

        assert body["daily_search_limit"] == 25
        assert body["daily_apply_limit"] == 2

    def test_automation_mode_change_notes_the_gate(self, client, seeded):
        """Enabling submission mode doesn't bypass the clean-submissions gate."""
        body = client.patch(
            "/api/v1/settings/platforms/greenhouse",
            json={"automation_mode": "search_fill_submit"},
        ).json()

        assert body["automation_mode"] == "search_fill_submit"
        assert "3 reviewed submissions" in body["note"]
        assert "currently 2" in body["note"]

    def test_negative_limits_are_rejected(self, client, seeded):
        response = client.patch(
            "/api/v1/settings/platforms/greenhouse", json={"daily_apply_limit": -1})

        assert response.status_code == 400

    def test_invalid_automation_mode_is_rejected(self, client, seeded):
        response = client.patch(
            "/api/v1/settings/platforms/greenhouse", json={"automation_mode": "yolo"})

        assert response.status_code == 400
        assert "must be one of" in response.json()["detail"]

    def test_unknown_platform_returns_404(self, client):
        assert client.patch(
            "/api/v1/settings/platforms/myspace", json={}).status_code == 404


# ============================================================================
# Audit coverage across phases
# ============================================================================

class TestAuditCoverage:
    """
    Every phase writes to the audit log; Phase 10 surfaces it.

    This checks the action vocabulary actually spans the pipeline, so the log
    can answer "what did the agent do" rather than just "what did it search".
    """

    @pytest.mark.parametrize("action", [
        AuditAction.ACCOUNT_CONNECTED,      # Phase 1
        AuditAction.SEARCH_RUN,             # Phase 3
        AuditAction.JOB_DEDUPED,            # Phase 3
        AuditAction.DOCUMENT_TAILORED,      # Phase 4
        AuditAction.DOCUMENT_FLAGGED,       # Phase 4
        AuditAction.APPLICATION_FILLED,     # Phase 5
        AuditAction.FIELD_DEFERRED,         # Phase 5
        AuditAction.APPLICATION_SUBMITTED,  # Phase 6
        AuditAction.EMAIL_SENT,             # Phase 6b
        AuditAction.RUN_COMPLETED,          # Phase 8
        AuditAction.INTERRUPTION_RAISED,    # Phase 9
        AuditAction.PLATFORM_RESUMED,       # Phase 9
    ])
    def test_action_exists(self, action):
        assert action.value

    def test_every_action_is_exportable(self, session):
        """A new action type must not break the export."""
        for action in AuditAction:
            session.add(AuditLog(
                timestamp=utcnow(), action=action, actor="agent",
                detail=f"{action.value}", result="success",
            ))
        session.commit()

        rows = parse_csv(Exporter.to_csv(Exporter(session).audit()))

        assert len(rows) == len(list(AuditAction)) + 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
