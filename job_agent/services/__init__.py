"""Services package for Job Agent.

Business logic services for:
- Job deduplication (rapidfuzz matching)
- Hard filter evaluation (salary, location, keywords, date posted)
- Fit scoring (Phase 3 heuristic, Phase 4+ LLM-based)
- Document parsing, tailoring, verification, and PDF rendering (Phase 4)
- Form field classification, reading, and filling (Phase 5)
- Submission gating, submitting, and recording (Phase 6)
- Email detection, composition, sending, and reply tracking (Phase 6b)
"""

from job_agent.services.job_deduplicator import (
    JobDeduplicator,
    generate_dedup_hash,
    is_duplicate_job,
    find_duplicate_job,
)
from job_agent.services.filter_evaluator import (
    FilterEvaluator,
    evaluate_hard_filters,
)
from job_agent.services.fit_scorer import (
    FitScorer,
    get_fit_scorer,
)
from job_agent.services.document_parser import (
    DocumentParser,
    parse_document,
)
from job_agent.services.pdf_renderer import (
    PdfRenderer,
    render_pdf,
)
from job_agent.services.fabrication_check import (
    FabricationCheck,
    verify_no_fabrication,
)
from job_agent.services.tailoring import (
    TailoringService,
    TailoringResult,
    get_tailoring_service,
)
from job_agent.services.document_builder import DocumentBuilder
from job_agent.services.field_classifier import (
    FieldClassifier,
    FieldClassification,
    FormField,
)
from job_agent.services.form_reader import FormReader
from job_agent.services.application_filler import ApplicationFiller, FillOutcome
from job_agent.services.submission_gate import SubmissionGate, SubmissionDecision
from job_agent.services.submitter import ApplicationSubmitter, SubmissionOutcome
from job_agent.services.submission_recorder import SubmissionRecorder
from job_agent.services.email_detector import (
    EmailApplicationDetector,
    detect_application_email,
)
from job_agent.services.email_composer import EmailComposer, ComposedEmail
from job_agent.services.email_sender import EmailSender, SendResult
from job_agent.services.interruption_detector import (
    InterruptionDetector,
    InterruptionKind,
    Interruption,
    detect_interruption,
)
from job_agent.services.session_monitor import SessionMonitor, PlatformHealth
from job_agent.services.exporter import Exporter
from job_agent.services.email_service import (
    EmailApplicationService,
    EmailApplicationError,
)

__all__ = [
    "JobDeduplicator",
    "generate_dedup_hash",
    "is_duplicate_job",
    "find_duplicate_job",
    "FilterEvaluator",
    "evaluate_hard_filters",
    "FitScorer",
    "get_fit_scorer",
    "DocumentParser",
    "parse_document",
    "PdfRenderer",
    "render_pdf",
    "FabricationCheck",
    "verify_no_fabrication",
    "TailoringService",
    "TailoringResult",
    "get_tailoring_service",
    "DocumentBuilder",
    "FieldClassifier",
    "FieldClassification",
    "FormField",
    "FormReader",
    "ApplicationFiller",
    "FillOutcome",
    "SubmissionGate",
    "SubmissionDecision",
    "ApplicationSubmitter",
    "SubmissionOutcome",
    "SubmissionRecorder",
    "EmailApplicationDetector",
    "detect_application_email",
    "EmailComposer",
    "ComposedEmail",
    "EmailSender",
    "SendResult",
    "EmailApplicationService",
    "EmailApplicationError",
    "InterruptionDetector",
    "InterruptionKind",
    "Interruption",
    "detect_interruption",
    "SessionMonitor",
    "PlatformHealth",
    "Exporter",
]
