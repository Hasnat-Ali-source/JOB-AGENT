#!/usr/bin/env python3
"""
API tests for Phase 4: Document Generation.

Exercises the full user-facing path over HTTP:
upload a master → generate a tailored variant for a job → download the PDF.
"""

import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from job_agent.dashboard.deps import get_session
from job_agent.dashboard.main import app
from job_agent.models.database import DocumentType, Job, MasterDocument

MASTER_RESUME = """Alex Rivera
alex.rivera@example.com | San Francisco, CA

Summary
Backend engineer with 8 years building distributed systems.

Experience
Staff Engineer, Northwind Systems (2021 - Present)
- Led migration of the billing platform to PostgreSQL, cutting query latency by 35%
- Mentored 4 engineers across two teams

Skills
Python, PostgreSQL, AWS, Kafka
"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient bound to an isolated database and document store."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    session = Session(engine)

    # Keep generated files out of the user's real app-support directory
    from job_agent.config import Settings
    monkeypatch.setattr(
        Settings, "documents_dir",
        property(lambda self: tmp_path / "documents"),
    )

    app.dependency_overrides[get_session] = lambda: session

    with TestClient(app) as test_client:
        test_client.db = session  # type: ignore[attr-defined]
        yield test_client

    app.dependency_overrides.clear()
    session.close()


@pytest.fixture
def job(client) -> Job:
    """A stored job to tailor against."""
    record = Job(
        platform="generic_ats", external_id="REQ-1",
        title="Senior Backend Engineer", company="Acme Robotics", location="Remote",
        description="PostgreSQL, Kafka, billing infrastructure, mentoring.",
        requirements="Python, PostgreSQL, Kafka",
        apply_method="web_form", dedup_hash="hash-1",
    )
    client.db.add(record)
    client.db.commit()
    client.db.refresh(record)
    return record


def _upload(client, text: str = MASTER_RESUME, doc_type: str = "resume",
            filename: str = "resume.txt", name: str = "My Resume"):
    """Helper: POST a master document."""
    return client.post(
        "/api/v1/documents/masters",
        files={"file": (filename, io.BytesIO(text.encode()), "text/plain")},
        data={"doc_type": doc_type, "name": name},
    )


class TestMasterUploadApi:
    """POST /api/v1/documents/masters"""

    def test_upload_returns_parsed_master(self, client):
        response = _upload(client)

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "stored"
        assert body["doc_type"] == "resume"
        assert body["name"] == "My Resume"
        assert body["is_active"] is True
        assert "experience" in body["sections"]

    def test_uploaded_master_is_listed(self, client):
        _upload(client)

        body = client.get("/api/v1/documents/masters").json()

        assert body["total"] == 1
        assert body["masters"][0]["doc_type"] == "resume"

    def test_master_detail_includes_text(self, client):
        master_id = _upload(client).json()["id"]

        body = client.get(f"/api/v1/documents/masters/{master_id}").json()

        assert "Northwind Systems" in body["content_text"]
        assert "experience" in body["section_text"]

    def test_bad_doc_type_rejected(self, client):
        response = client.post(
            "/api/v1/documents/masters",
            files={"file": ("r.txt", io.BytesIO(b"text"), "text/plain")},
            data={"doc_type": "portfolio"},
        )

        assert response.status_code == 400
        assert "resume" in response.json()["detail"]

    def test_unsupported_extension_rejected(self, client):
        response = client.post(
            "/api/v1/documents/masters",
            files={"file": ("resume.pages", io.BytesIO(b"data"), "application/octet-stream")},
            data={"doc_type": "resume"},
        )

        assert response.status_code == 400
        assert "Unsupported document format" in response.json()["detail"]

    def test_empty_file_rejected(self, client):
        response = _upload(client, text="   \n  ")

        assert response.status_code == 400
        assert "No text" in response.json()["detail"]

    def test_missing_master_returns_404(self, client):
        assert client.get("/api/v1/documents/masters/999").status_code == 404

    def test_activate_switches_the_active_master(self, client):
        first = _upload(client, name="v1").json()["id"]
        second = _upload(client, name="v2").json()["id"]

        response = client.post(f"/api/v1/documents/masters/{first}/activate")

        assert response.status_code == 200
        masters = {m["id"]: m for m in client.get("/api/v1/documents/masters").json()["masters"]}
        assert masters[first]["is_active"] is True
        assert masters[second]["is_active"] is False


class TestMasterReparseApi:
    """
    POST /api/v1/documents/masters/{id}/reparse

    A master's text is derived from the file it was uploaded from, and a fix
    to the parser does not reach the documents the old one already read. The
    alternative was re-uploading, which loses the document's history and every
    tailored version's link back to it.
    """

    def test_reparsing_rereads_the_stored_file(self, client, monkeypatch):
        master_id = _upload(client).json()["id"]

        # Stand in for what a parser fix does: the same file, read better.
        client.db.execute(
            text("UPDATE master_documents SET content_text = 'stale' WHERE id = :i"),
            {"i": master_id},
        )
        client.db.commit()

        body = client.post(
            f"/api/v1/documents/masters/{master_id}/reparse"
        ).json()

        assert body["changed"] is True
        assert "Northwind Systems" in body["content_text"]
        assert "experience" in body["sections"]

    def test_reparsing_an_unchanged_file_says_so(self, client):
        master_id = _upload(client).json()["id"]

        body = client.post(
            f"/api/v1/documents/masters/{master_id}/reparse"
        ).json()

        assert body["changed"] is False

    def test_a_missing_source_file_is_refused(self, client):
        master_id = _upload(client).json()["id"]

        client.db.execute(
            text("UPDATE master_documents SET source_path = '/nope/gone.txt' "
                 "WHERE id = :i"),
            {"i": master_id},
        )
        client.db.commit()

        response = client.post(f"/api/v1/documents/masters/{master_id}/reparse")

        assert response.status_code == 409
        assert "upload it again" in response.json()["detail"]

    def test_unknown_master_returns_404(self, client):
        assert client.post(
            "/api/v1/documents/masters/999/reparse"
        ).status_code == 404


class TestGenerationApi:
    """POST /api/v1/documents/generate"""

    def test_generates_a_verified_variant(self, client, job):
        _upload(client)

        response = client.post(
            "/api/v1/documents/generate", params={"job_id": job.id, "doc_type": "resume"})

        assert response.status_code == 200
        body = response.json()
        assert body["company"] == "Acme Robotics"
        assert body["all_verified"] is True
        assert body["review_required"] is False
        assert len(body["generated"]) == 1
        assert body["generated"][0]["has_pdf"] is True

    def test_package_generates_resume_and_letter(self, client, job):
        _upload(client)
        _upload(client, doc_type="cover_letter", filename="letter.txt", name="My Letter")

        body = client.post(
            "/api/v1/documents/generate",
            params={"job_id": job.id, "doc_type": "package"}).json()

        assert [g["doc_type"] for g in body["generated"]] == ["resume", "cover_letter"]

    def test_generating_without_a_master_returns_400(self, client, job):
        response = client.post(
            "/api/v1/documents/generate", params={"job_id": job.id, "doc_type": "resume"})

        assert response.status_code == 400
        assert "No active master" in response.json()["detail"]

    def test_unknown_job_returns_404(self, client):
        _upload(client)

        response = client.post(
            "/api/v1/documents/generate", params={"job_id": 999, "doc_type": "resume"})

        assert response.status_code == 404

    def test_bad_doc_type_returns_400(self, client, job):
        _upload(client)

        response = client.post(
            "/api/v1/documents/generate", params={"job_id": job.id, "doc_type": "portfolio"})

        assert response.status_code == 400


class TestVersionApi:
    """GET /api/v1/documents/versions"""

    def test_versions_are_listed_for_a_job(self, client, job):
        _upload(client)
        client.post("/api/v1/documents/generate", params={"job_id": job.id})

        body = client.get("/api/v1/documents/versions", params={"job_id": job.id}).json()

        assert body["total"] == 1
        assert body["versions"][0]["verified"] is True
        assert body["versions"][0]["generator"] == "deterministic"

    def test_version_detail_includes_text_and_notes(self, client, job):
        _upload(client)
        version_id = client.post(
            "/api/v1/documents/generate",
            params={"job_id": job.id}).json()["generated"][0]["id"]

        body = client.get(f"/api/v1/documents/versions/{version_id}").json()

        assert "Alex Rivera" in body["content_text"]
        assert body["tailoring_notes"]
        assert body["fabrication_flags"] == []

    def test_pdf_downloads_as_a_real_pdf(self, client, job):
        """Acceptance: the user can download the variant to review before submitting."""
        _upload(client)
        version_id = client.post(
            "/api/v1/documents/generate",
            params={"job_id": job.id}).json()["generated"][0]["id"]

        response = client.get(f"/api/v1/documents/versions/{version_id}/pdf")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.content.startswith(b"%PDF")
        assert len(response.content) > 1000

    def test_missing_version_returns_404(self, client):
        assert client.get("/api/v1/documents/versions/999").status_code == 404
        assert client.get("/api/v1/documents/versions/999/pdf").status_code == 404

    def test_deleted_pdf_reports_clearly(self, client, job):
        _upload(client)
        version_id = client.post(
            "/api/v1/documents/generate",
            params={"job_id": job.id}).json()["generated"][0]["id"]

        detail = client.get(f"/api/v1/documents/versions/{version_id}").json()
        Path(detail["pdf_path"]).unlink()

        response = client.get(f"/api/v1/documents/versions/{version_id}/pdf")

        assert response.status_code == 404
        assert "missing from disk" in response.json()["detail"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
