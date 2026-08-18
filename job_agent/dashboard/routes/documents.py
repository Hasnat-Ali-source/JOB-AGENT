"""
Document API Routes (Phase 4).

Endpoints:
- POST /api/v1/documents/masters — Upload a master resume or cover letter
- GET  /api/v1/documents/masters — List master documents
- GET  /api/v1/documents/masters/{id} — Master detail (text + sections)
- POST /api/v1/documents/masters/{id}/activate — Make a master the active one
- POST /api/v1/documents/generate — Generate a tailored variant for a job
- GET  /api/v1/documents/versions — List generated variants
- GET  /api/v1/documents/versions/{id} — Variant detail (text, notes, flags)
- GET  /api/v1/documents/versions/{id}/pdf — Download the rendered PDF
"""

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from job_agent.dashboard.deps import get_session
from job_agent.models.database import (
    DocumentType,
    DocumentVersion,
    Job,
    MasterDocument,
)
from job_agent.models.database import SearchProfile
from job_agent.services.document_builder import DocumentBuilder
from job_agent.services.resume_sync import sync_to_resume
from job_agent.utils.dates import utcnow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])
SessionDep = get_session


def _start_a_run_for(background_tasks: BackgroundTasks, session: Session) -> bool:
    """
    Kick off a search for the resume that has just come into use.

    The wire empties the moment the resume changes — every posting on it was
    found for a different one. Leaving the user to notice that and press the
    run button themselves is the gap between "the resume drives everything"
    and "the resume drives everything if you remember a second step". So the
    run starts itself, in the background, and the wire refills.

    Args:
        background_tasks: FastAPI's background queue
        session: Database session, used only to find the profile to run

    Returns:
        True if a run was queued
    """
    profile = (
        session.query(SearchProfile)
        .filter(SearchProfile.is_active == True)  # noqa: E712 — SQL comparison
        .first()
    )

    if not profile:
        logger.info("No active search profile — not starting a run")
        return False

    background_tasks.add_task(_run_search, profile.id)

    return True


async def _run_search(profile_id: int) -> None:
    """
    Run the pipeline for one search profile, on its own database session.

    A background task outlives the request, so it must not borrow the
    request's session — that one is closed the moment the response is sent.

    Args:
        profile_id: The search profile to run
    """
    from sqlmodel import Session as SQLModelSession

    from job_agent.core.orchestrator import RunOrchestrator
    from job_agent.dashboard.deps import get_engine

    try:
        with SQLModelSession(get_engine()) as run_session:
            profile = (
                run_session.query(SearchProfile)
                .filter(SearchProfile.id == profile_id)
                .first()
            )

            if not profile:
                return

            await RunOrchestrator(run_session).run(
                profile,
                trigger="resume_changed",
                generate_documents=True,
            )
    except Exception as e:
        # A background run that fails must not take the process with it. The
        # register records what happened; the desk's run button is still there.
        logger.error(f"Background run after a resume change failed: {e}")


def _master_summary(document: MasterDocument) -> dict:
    """Serialize a master document for list responses."""
    return {
        "id": document.id,
        "doc_type": document.doc_type.value,
        "name": document.name,
        "source_format": document.source_format.value,
        "is_active": document.is_active,
        "sections": list(document.sections or {}),
        "characters": len(document.content_text or ""),
        "created_at": document.created_at.isoformat(),
    }


def _version_summary(version: DocumentVersion) -> dict:
    """Serialize a document version for list responses."""
    return {
        "id": version.id,
        "doc_type": version.doc_type.value,
        "job_id": version.job_id,
        "master_document_id": version.master_document_id,
        "generator": version.generator,
        "verified": version.is_verified,
        "fabrication_flags": version.fabrication_flags,
        "tailoring_notes": version.tailoring_notes,
        "has_pdf": bool(version.pdf_path),
        "created_at": version.created_at.isoformat(),
    }


# ============================================================================
# Master documents
# ============================================================================

@router.post("/masters")
async def upload_master(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="Master .docx, .pdf, .txt or .md"),
    doc_type: str = Form("resume", description="resume or cover_letter"),
    name: Optional[str] = Form(None, description="Display name"),
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Upload and parse a master document.

    The file is parsed to text, split into sections, and copied into managed
    storage; the original upload is never modified.

    Args:
        file: The uploaded document
        doc_type: "resume" or "cover_letter"
        name: Display name (defaults to the filename)

    Returns:
        The stored master document
    """
    try:
        document_type = DocumentType(doc_type)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"doc_type must be 'resume' or 'cover_letter', got '{doc_type}'",
        )

    suffix = Path(file.filename or "").suffix or ".txt"

    # Spool to a temp file so the parser works on a real path
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        shutil.copyfileobj(file.file, tmp)
        temp_path = Path(tmp.name)

    try:
        builder = DocumentBuilder(session)
        document = builder.upload_master(
            temp_path,
            document_type,
            name=name or Path(file.filename or "").stem or None,
        )

        # `upload_master` makes the new resume the one in use, so the rest of
        # the pipeline has to follow it — otherwise the stations keep
        # searching the previous resume's job titles and the wire fills with
        # postings the new resume cannot answer.
        sync = None

        if document.doc_type == DocumentType.RESUME and document.is_active:
            sync = sync_to_resume(session, document)
            session.commit()

            _start_a_run_for(background_tasks, session)

        return {
            "status": "stored",
            **_master_summary(document),
            "pipeline": sync.to_dict() if sync else None,
        }

    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error storing master document: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        temp_path.unlink(missing_ok=True)


@router.get("/masters")
async def list_masters(
    doc_type: Optional[str] = Query(None, description="Filter by document type"),
    session: Session = Depends(SessionDep),
) -> dict:
    """
    List master documents.

    Args:
        doc_type: Optional "resume" / "cover_letter" filter

    Returns:
        Masters and total count
    """
    query = session.query(MasterDocument)

    if doc_type:
        query = query.filter(MasterDocument.doc_type == doc_type)

    masters = query.order_by(MasterDocument.created_at.desc()).all()

    return {
        "total": len(masters),
        "masters": [_master_summary(m) for m in masters],
    }


@router.get("/masters/{master_id}")
async def get_master(
    master_id: int,
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Get a master document including its parsed text.

    Args:
        master_id: Master document ID

    Returns:
        Full master document
    """
    document = session.query(MasterDocument).filter(MasterDocument.id == master_id).first()

    if not document:
        raise HTTPException(status_code=404, detail="Master document not found")

    return {
        **_master_summary(document),
        "content_text": document.content_text,
        "section_text": document.sections,
        "source_path": document.source_path,
    }


@router.post("/masters/{master_id}/activate")
async def activate_master(
    master_id: int,
    background_tasks: BackgroundTasks,
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Make a master document the active one for its type.

    Args:
        master_id: Master document ID

    Returns:
        Updated master
    """
    document = session.query(MasterDocument).filter(MasterDocument.id == master_id).first()

    if not document:
        raise HTTPException(status_code=404, detail="Master document not found")

    others = (
        session.query(MasterDocument)
        .filter(
            MasterDocument.doc_type == document.doc_type,
            MasterDocument.id != master_id,
        )
        .all()
    )

    for other in others:
        other.is_active = False

    document.is_active = True

    # Switching resume is meant to change what the agent is looking for. The
    # search terms, the wire and the tray all follow from here.
    sync = None

    if document.doc_type == DocumentType.RESUME:
        sync = sync_to_resume(session, document)

    session.commit()
    session.refresh(document)

    if sync:
        _start_a_run_for(background_tasks, session)

    return {
        "status": "activated",
        **_master_summary(document),
        "pipeline": sync.to_dict() if sync else None,
    }


@router.post("/masters/resync")
async def resync_pipeline(
    background_tasks: BackgroundTasks,
    run: bool = Query(True, description="Also start a search straight away"),
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Point the search at the resume in use, and refill the wire.

    Uploading or switching a resume does this by itself. This is the same
    action on demand, for the case the automatic one could not cover: a resume
    edited outside the app, a run that failed, or a wire that emptied because
    every posting on it belongs to a resume no longer in use.

    Args:
        run: Start a search immediately as well

    Returns:
        What the search now looks for, and whether a run was started

    Raises:
        HTTPException: If no resume is in use
    """
    from job_agent.services.resume_sync import active_master

    master = active_master(session)

    if not master:
        raise HTTPException(
            status_code=409,
            detail="No resume is in use — upload one on the desk first",
        )

    sync = sync_to_resume(session, master)
    session.commit()

    started = _start_a_run_for(background_tasks, session) if run else False

    return {
        "status": "resynced",
        "pipeline": sync.to_dict(),
        "run_started": started,
        "message": (
            sync.describe()
            + (" Searching now — the wire will refill." if started else "")
        ),
    }


@router.post("/masters/{master_id}/reparse")
async def reparse_master(
    master_id: int,
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Read a stored master again from the file it was uploaded from.

    A master's text is derived data, and a parser fix does not reach the
    documents already parsed by the old one. Re-uploading works but loses the
    document's history and every tailored version's link back to it; this
    re-derives the text in place.

    The original file is never touched — it is the input, not the output.

    Args:
        master_id: Master document ID

    Returns:
        What changed, with both texts for comparison

    Raises:
        HTTPException: If the document or its source file is missing
    """
    document = session.query(MasterDocument).filter(
        MasterDocument.id == master_id
    ).first()

    if not document:
        raise HTTPException(status_code=404, detail="Master document not found")

    source = Path(document.source_path or "")

    if not source.exists():
        raise HTTPException(
            status_code=409,
            detail=(
                f"The file this was uploaded from is gone ({source}) — "
                f"upload it again instead"
            ),
        )

    from job_agent.services.document_parser import parse_document

    before = document.content_text or ""
    content_text, sections, _ = parse_document(source)

    if not content_text.strip():
        raise HTTPException(
            status_code=409,
            detail="Re-reading the file produced no text; nothing was changed",
        )

    document.content_text = content_text
    document.sections = sections
    document.updated_at = utcnow()
    session.commit()
    session.refresh(document)

    return {
        "status": "reparsed",
        "master_id": master_id,
        "changed": before != content_text,
        "characters_before": len(before),
        "characters_after": len(content_text),
        "sections": list(sections),
        "content_text": content_text,
        "note": (
            "Tailored documents generated before this still carry the old "
            "text. Regenerate the applications you have not sent."
        ),
    }


@router.get("/match")
async def resume_matches_profile(
    profile_id: int = Query(..., description="Search profile to check against"),
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Whether the resume in use has anything to do with the jobs being searched.

    Tailoring reorders and rewords a master; it cannot turn one career into
    another, and it is designed not to try. So an active resume with no
    overlap at all with the roles being searched produces applications that
    are honest and hopeless — and the mismatch is invisible once a plausible
    PDF has been generated from it.

    Args:
        profile_id: The search profile to compare against

    Returns:
        The verdict, the resume checked, and which terms were found

    Raises:
        HTTPException: If the profile does not exist
    """
    from job_agent.models.database import SearchProfile

    profile = session.query(SearchProfile).filter(
        SearchProfile.id == profile_id
    ).first()

    if not profile:
        raise HTTPException(
            status_code=404, detail=f"Search profile {profile_id} not found"
        )

    resume = (
        session.query(MasterDocument)
        .filter(
            MasterDocument.doc_type == DocumentType.RESUME,
            MasterDocument.is_active == True,  # noqa: E712
        )
        .first()
    )

    if not resume:
        return {
            "checked": False,
            "matches": False,
            "reason": "No resume is in use yet.",
            "resume": None,
            "matched_terms": [],
        }

    text = (resume.content_text or "").lower()

    # Words worth looking for: the distinctive parts of the titles being
    # searched, minus the connective tissue every job title shares.
    noise = {
        "and", "the", "for", "with", "senior", "junior", "staff", "lead",
        "principal", "engineer", "developer", "manager", "specialist",
    }

    terms = {
        word
        for title in (profile.target_titles or [])
        for word in title.lower().replace("-", " ").split()
        if len(word) > 2 and word not in noise
    }

    matched = sorted(term for term in terms if term in text)

    # No distinctive terms means nothing to disagree with — a profile of
    # "Engineer" tells us nothing about whether this resume suits it.
    matches = bool(matched) or not terms

    return {
        "checked": True,
        "matches": matches,
        "reason": (
            None
            if matches
            else (
                f"“{resume.name}” does not mention "
                f"{', '.join(sorted(terms))} anywhere. Tailoring reorders and "
                f"rewords what a resume already says — it will not add "
                f"experience the resume does not contain."
            )
        ),
        "resume": resume.name,
        "matched_terms": matched,
    }


# ============================================================================
# Tailored variants
# ============================================================================

@router.post("/generate")
async def generate_variant(
    job_id: int = Query(..., description="Job to tailor for"),
    doc_type: str = Query("resume", description="resume, cover_letter, or package"),
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Generate tailored document(s) for a job.

    Use doc_type="package" to generate a resume plus a cover letter when a
    master cover letter exists.

    Variants whose `verified` is false contain claims the master document does
    not support and must be read before they are sent anywhere.

    Args:
        job_id: Job to tailor for
        doc_type: "resume", "cover_letter", or "package"

    Returns:
        The generated version(s)
    """
    job = session.query(Job).filter(Job.id == job_id).first()

    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    builder = DocumentBuilder(session)

    try:
        if doc_type == "package":
            versions = await builder.build_application_package(job)
        else:
            try:
                document_type = DocumentType(doc_type)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"doc_type must be 'resume', 'cover_letter' or 'package', "
                           f"got '{doc_type}'",
                )
            versions = [await builder.build_variant(job, document_type)]

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating documents for job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    unverified = [v for v in versions if not v.is_verified]

    return {
        "job_id": job_id,
        "job_title": job.title,
        "company": job.company,
        "generated": [_version_summary(v) for v in versions],
        "all_verified": not unverified,
        "review_required": bool(unverified),
    }


@router.get("/versions")
async def list_versions(
    job_id: Optional[int] = Query(None, description="Filter by job"),
    doc_type: Optional[str] = Query(None, description="Filter by document type"),
    verified_only: bool = Query(False, description="Exclude flagged variants"),
    session: Session = Depends(SessionDep),
) -> dict:
    """
    List generated document variants.

    Args:
        job_id: Filter by job
        doc_type: Filter by document type
        verified_only: Return only variants with no fabrication flags

    Returns:
        Versions and total count
    """
    query = session.query(DocumentVersion)

    if job_id is not None:
        query = query.filter(DocumentVersion.job_id == job_id)

    if doc_type:
        query = query.filter(DocumentVersion.doc_type == doc_type)

    versions = query.order_by(DocumentVersion.created_at.desc()).all()

    if verified_only:
        versions = [v for v in versions if v.is_verified]

    return {
        "total": len(versions),
        "versions": [_version_summary(v) for v in versions],
    }


@router.get("/versions/{version_id}")
async def get_version(
    version_id: int,
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Get a document variant including its full text.

    Args:
        version_id: Document version ID

    Returns:
        Full variant
    """
    version = session.query(DocumentVersion).filter(DocumentVersion.id == version_id).first()

    if not version:
        raise HTTPException(status_code=404, detail="Document version not found")

    return {
        **_version_summary(version),
        "content_text": version.content_text,
        "pdf_path": version.pdf_path,
    }


@router.get("/versions/{version_id}/pdf")
async def download_version_pdf(
    version_id: int,
    session: Session = Depends(SessionDep),
) -> FileResponse:
    """
    Download the rendered PDF for a variant.

    Args:
        version_id: Document version ID

    Returns:
        The PDF file
    """
    version = session.query(DocumentVersion).filter(DocumentVersion.id == version_id).first()

    if not version:
        raise HTTPException(status_code=404, detail="Document version not found")

    if not version.pdf_path:
        raise HTTPException(status_code=404, detail="No PDF was rendered for this version")

    pdf_path = Path(version.pdf_path)

    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail=f"PDF missing from disk: {pdf_path}")

    return FileResponse(
        path=pdf_path,
        media_type="application/pdf",
        filename=pdf_path.name,
    )

@router.get("/masters/{master_id}/ats")
async def ats_score(
    master_id: int,
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Score a master resume on machine readability.

    Args:
        master_id: The master document

    Returns:
        The ATS report, scored out of 100

    Raises:
        HTTPException: If the document does not exist
    """
    from job_agent.services.ats_check import check_ats

    document = session.query(MasterDocument).filter(
        MasterDocument.id == master_id
    ).first()

    if not document:
        raise HTTPException(status_code=404, detail="Master document not found")

    return {"master_id": master_id, "name": document.name, **check_ats(document.content_text).to_dict()}


@router.post("/masters/{master_id}/ats/improve")
async def ats_improve(
    master_id: int,
    apply: bool = Query(False, description="Write the improved text back"),
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Fix what the ATS check objects to, changing form only.

    Nothing is written back unless `apply` is set, so the user sees the
    rewrite and both scores before deciding. A rewrite that introduces
    anything the original does not say is refused outright rather than
    offered — that check is the reason the score is worth having.

    Args:
        master_id: The master document
        apply: Whether to save the improved text over the original

    Returns:
        Both scores, every change made, and the rewritten text

    Raises:
        HTTPException: If the document does not exist, or the rewrite failed
            the no-invention check
    """
    from job_agent.services.ats_improver import improve_ats

    document = session.query(MasterDocument).filter(
        MasterDocument.id == master_id
    ).first()

    if not document:
        raise HTTPException(status_code=404, detail="Master document not found")

    result = improve_ats(document.content_text)

    if not result.is_safe:
        raise HTTPException(
            status_code=409,
            detail=(
                f"The rewrite introduced something your resume does not say "
                f"({result.fabrication_flags[0]}) and was discarded. Nothing changed."
            ),
        )

    if apply and result.improved:
        document.content_text = result.text
        document.updated_at = utcnow()
        session.commit()

    return {
        "master_id": master_id,
        "applied": bool(apply and result.improved),
        **result.to_dict(),
    }


@router.get("/fit/{job_id}")
async def fit_report(
    job_id: int,
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Compare a posting's requirements against the resume in use.

    Says which requirements the candidate can evidence, with the sentence
    from their own resume that evidences each one, and which they cannot.
    A gap stays a gap: nothing here suggests closing one by adding
    experience the resume does not contain.

    Args:
        job_id: The posting to analyse

    Returns:
        The fit report

    Raises:
        HTTPException: If the job or an active resume is missing
    """
    from job_agent.services.fit_report import analyse_fit

    job = session.query(Job).filter(Job.id == job_id).first()

    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    resume = (
        session.query(MasterDocument)
        .filter(
            MasterDocument.doc_type == DocumentType.RESUME,
            MasterDocument.is_active == True,  # noqa: E712
        )
        .first()
    )

    if not resume:
        raise HTTPException(
            status_code=409,
            detail="No resume is in use — upload one on the desk first",
        )

    posting = "\n".join(filter(None, [job.description, job.requirements]))

    return {
        "job_id": job_id,
        "job_title": job.title,
        "company": job.company,
        "resume": resume.name,
        **analyse_fit(posting, resume.content_text).to_dict(),
    }
