#!/usr/bin/env python3
"""
Live Ollama tests for Phase 4 (opt-in).

Every other test forces the deterministic path (see conftest.py) so the suite
stays hermetic and fast. These run against a real local model to check the
things only a real model can break: whether it follows the no-fabrication rule,
whether it returns a document instead of replaying the prompt, and whether the
guards catch it when it doesn't.

Skipped automatically when Ollama isn't running or has no local model.

Run explicitly:
    .venv/bin/python -m pytest tests/test_phase4_ollama_live.py -v
"""

import asyncio
from pathlib import Path

import pytest

from job_agent.models.database import DocumentType, Job
from job_agent.services.tailoring import TailoringService

MASTER_RESUME = """Alex Rivera
alex.rivera@example.com | +1 555 0100 | San Francisco, CA

Summary
Backend engineer with 8 years building distributed systems.

Experience
Staff Engineer, Northwind Systems (2021 - Present)
- Led migration of the billing platform to PostgreSQL, cutting query latency by 35%
- Designed an event pipeline processing 12M events per day
- Mentored 4 engineers across two teams

Backend Engineer, Lumen Data (2018 - 2021)
- Built REST services in Python and Django
- Introduced automated testing, raising coverage to 82%

Education
BSc Computer Science, University of Washington (2018)

Skills
Python, PostgreSQL, AWS, Kafka, Docker, Kubernetes
"""


def _ollama_model() -> str | None:
    """Return a usable local model name, or None if Ollama can't serve one."""
    async def probe():
        try:
            from ollama import AsyncClient

            service = TailoringService()
            client = AsyncClient(host=service.ollama_url)
            return await asyncio.wait_for(service.resolve_ollama_model(client), timeout=5)
        except Exception:
            return None

    try:
        return asyncio.run(probe())
    except Exception:
        return None


LIVE_MODEL = _ollama_model()

pytestmark = pytest.mark.skipif(
    LIVE_MODEL is None,
    reason="Ollama is not running or has no local model installed",
)


@pytest.fixture
def live_service() -> TailoringService:
    """A tailoring service with Ollama enabled, overriding the offline default."""
    service = TailoringService()
    service.use_ollama = True
    service.use_anthropic = False
    return service


@pytest.fixture
def job() -> Job:
    """A job to tailor against."""
    return Job(
        platform="generic_ats", external_id="1",
        title="Senior Backend Engineer", company="Acme Robotics", location="Remote",
        description=(
            "PostgreSQL, Kafka, event-driven pipelines, billing infrastructure, "
            "and mentoring other engineers."
        ),
        requirements="Python, PostgreSQL, Kafka, distributed systems",
        apply_method="web_form", dedup_hash="h",
    )


@pytest.mark.asyncio
class TestLiveOllama:
    """Behaviour that only a real model exercises."""

    async def test_model_is_resolved(self, live_service):
        from ollama import AsyncClient

        model = await live_service.resolve_ollama_model(AsyncClient(host=live_service.ollama_url))

        assert model
        assert not model.endswith(":cloud")  # local execution only, by default

    async def test_cloud_models_are_not_selected_by_default(self, live_service):
        """A ':cloud' model would send the user's resume off the machine."""
        from ollama import AsyncClient

        live_service.ollama_model = "definitely-not-installed"
        model = await live_service.resolve_ollama_model(
            AsyncClient(host=live_service.ollama_url))

        if model is not None:
            assert not model.endswith(":cloud")

    async def test_produces_a_usable_document(self, live_service, job):
        result = await live_service.tailor(MASTER_RESUME, job, DocumentType.RESUME)

        # Either the model produced something usable, or the guards rejected it
        # and the deterministic path took over. Both are acceptable; garbage is not.
        assert "Alex Rivera" in result.content_text
        assert "Northwind Systems" in result.content_text
        assert "--- The job being applied to" not in result.content_text
        assert "MASTER DOCUMENT" not in result.content_text
        assert len(result.content_text) > 200

    async def test_output_is_verified_against_the_master(self, live_service, job):
        """
        Whatever the model writes, unsupported claims must be caught rather
        than silently accepted.
        """
        result = await live_service.tailor(MASTER_RESUME, job, DocumentType.RESUME)

        # The invariant is that verification ran and its verdict matches the flags
        assert result.is_verified == (not result.fabrication_flags)

        for flag in result.fabrication_flags:
            assert "does not appear in the master document" in flag

    async def test_rendered_pdf_is_valid(self, live_service, job, tmp_path):
        from job_agent.services.pdf_renderer import PdfRenderer

        result = await live_service.tailor(MASTER_RESUME, job, DocumentType.RESUME)
        out = tmp_path / "live.pdf"

        PdfRenderer().render(result.content_text, out, title="Live tailored resume")

        assert out.read_bytes().startswith(b"%PDF")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
