#!/usr/bin/env python3
"""
API tests for Phase 7: the connector catalogue.

The capability declarations only protect the user if the user can see them —
this is the endpoint the GUI uses to hide unsupported actions and to show a
terms-of-service risk note before anyone enables automation on a consumer board.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from job_agent.dashboard.deps import get_session
from job_agent.dashboard.main import app
from job_agent.models.database import AutomationMode, PlatformAccount


@pytest.fixture
def client():
    """TestClient bound to an isolated database."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    app.dependency_overrides[get_session] = lambda: session

    with TestClient(app) as test_client:
        test_client.db = session  # type: ignore[attr-defined]
        yield test_client

    app.dependency_overrides.clear()
    session.close()


class TestConnectorCatalogue:
    """GET /api/v1/connectors"""

    def test_lists_every_registered_connector(self, client):
        body = client.get("/api/v1/connectors").json()

        platforms = [c["platform"] for c in body["connectors"]]

        for expected in ("greenhouse", "lever", "ashby", "workday",
                         "smartrecruiters", "workable", "linkedin", "indeed"):
            assert expected in platforms

    def test_separates_submitting_from_read_only(self, client):
        body = client.get("/api/v1/connectors").json()

        assert body["can_submit"] + body["read_only"] == body["total"]
        assert body["can_submit"] > 0
        assert body["read_only"] > 0

    def test_consumer_boards_are_read_only_with_notes(self, client):
        body = client.get("/api/v1/connectors").json()
        by_platform = {c["platform"]: c for c in body["connectors"]}

        for platform in ("linkedin", "indeed", "glassdoor", "ziprecruiter",
                         "wellfound", "dice", "jobstreet"):
            entry = by_platform[platform]
            assert entry["capabilities"]["submit_automatically"] is False, platform
            assert entry["tos_risk_note"], platform

    def test_ats_connectors_report_fixture_verification(self, client):
        """A capability claim and a proven capability are different things."""
        body = client.get("/api/v1/connectors/greenhouse").json()

        assert body["capabilities"]["submit_automatically"] is True
        assert body["verified_against"] == "fixture"

    def test_filter_by_submission_support(self, client):
        body = client.get("/api/v1/connectors", params={"can_submit": False}).json()

        assert all(
            c["capabilities"]["submit_automatically"] is False
            for c in body["connectors"]
        )

    def test_reports_connection_state(self, client):
        client.db.add(PlatformAccount(
            platform="greenhouse", profile_dir="/tmp/x",
            automation_mode=AutomationMode.SEARCH_FILL_SUBMIT,
            clean_submissions_count=2,
        ))
        client.db.commit()

        body = client.get("/api/v1/connectors/greenhouse").json()

        assert body["connected"] is True
        assert body["automation_mode"] == "search_fill_submit"
        assert body["clean_submissions_count"] == 2

    def test_unconnected_platform_reports_zero(self, client):
        body = client.get("/api/v1/connectors/lever").json()

        assert body["connected"] is False
        assert body["clean_submissions_count"] == 0

    def test_unknown_platform_returns_404(self, client):
        assert client.get("/api/v1/connectors/myspace").status_code == 404

    def test_every_entry_declares_a_review_threshold(self, client):
        body = client.get("/api/v1/connectors").json()

        for entry in body["connectors"]:
            assert entry["requires_manual_review_first_n"] >= 3, entry["platform"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
