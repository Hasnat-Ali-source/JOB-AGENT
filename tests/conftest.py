"""
Shared pytest configuration.

The tailoring service reaches for a local Ollama server when one is running.
That makes any test touching document generation depend on whether the
developer happens to have `ollama serve` up — the same test then produces
different output, takes minutes instead of milliseconds, and can fail because
a 3B model phrased something unexpectedly.

So LLM backends are disabled for every test by default. Tests that genuinely
exercise a live model opt back in explicitly (see test_phase4_ollama_live.py).
"""

import pytest

from job_agent.config import settings


@pytest.fixture(autouse=True)
def in_process_pdf_rendering(monkeypatch):
    """
    Render PDFs in-process during tests.

    Production isolates WeasyPrint in a subprocess so a native crash can't kill
    an unattended run, but that costs ~0.9s per document and the suite renders
    a lot of them — 94s became 172s with it on by default.

    The isolated path is covered explicitly by the tests in
    test_phase4_documents.py that pass `isolate=True`, which this doesn't
    affect.
    """
    monkeypatch.setattr(settings, "pdf_isolate_weasyprint", False)


@pytest.fixture(autouse=True)
def offline_tailoring(monkeypatch):
    """
    Force the deterministic tailoring path for every test.

    Also resets the module-level singleton so the service is rebuilt with the
    patched settings rather than reusing one created by an earlier test.
    """
    import job_agent.services.tailoring as tailoring_module

    monkeypatch.setattr(settings, "use_ollama", False)
    monkeypatch.setattr(settings, "use_anthropic", False)
    monkeypatch.setattr(tailoring_module, "_tailoring_service", None)

    yield

    # Leave no singleton built from patched settings behind
    tailoring_module._tailoring_service = None
