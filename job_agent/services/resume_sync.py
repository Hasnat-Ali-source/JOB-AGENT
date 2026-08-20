"""
Keeping the whole pipeline pointed at the resume in use.

**The failure this closes.** The search profile and the master resume were
independent. Changing the resume changed nothing else, so the stations went on
searching yesterday's terms, the wire went on showing yesterday's postings,
and the tray went on carrying documents built from a resume that was no longer
in use. On a real install this produced a wire where *none* of 59 postings
matched the resume's own job titles — and then a fit analyser that reported
0-7% on every one of them, which reads as the analyser being broken rather
than the agent looking for the wrong jobs.

Fit is decided by what gets searched for, not by how well a document is
written afterwards. Tailoring reorders and rewords; it cannot turn a
Full-Stack Developer into a VP of Data, and it is built not to try. So the
resume has to drive the search.

**What changing the resume now does, in order:**

1. The resume is read for the roles and skills it supports (`resume_profile`).
2. The active search profile's titles and keywords are rewritten from it.
3. Postings found for the previous resume stop appearing on the wire. They
   are not deleted — the register, the applications already sent and the
   audit trail all still refer to them.
4. A fresh run starts in the background, so the wire refills itself.

Applications already in the tray are deliberately *not* hidden. They are work
the user has done, sometimes with answers typed in by hand; making them vanish
because a resume changed would lose that. They are marked instead, so it is
visible that their documents came from a superseded resume and can be rebuilt.
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy.orm import Session

from job_agent.models.database import (
    Application,
    ApplicationStatus,
    DocumentType,
    DocumentVersion,
    Job,
    MasterDocument,
    SearchProfile,
)
from job_agent.services.resume_profile import ResumeProfile, read_resume
from job_agent.utils.dates import utcnow

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    """What bringing the pipeline into line with a resume changed."""

    master_id: Optional[int] = None
    master_name: str = ""
    resume_profile: Optional[ResumeProfile] = None
    search_profile_id: Optional[int] = None
    search_profile_name: str = ""
    titles_before: List[str] = field(default_factory=list)
    titles_after: List[str] = field(default_factory=list)
    jobs_from_previous_resumes: int = 0
    applications_on_old_documents: int = 0

    @property
    def changed_the_search(self) -> bool:
        """Whether the stations will now look for something different."""
        return self.titles_before != self.titles_after

    def describe(self) -> str:
        """One sentence for the dashboard."""
        if not self.resume_profile or not self.resume_profile.is_usable:
            return (
                "Could not read any job titles from this resume, so the search "
                "was left as it was."
            )

        if not self.changed_the_search:
            return (
                f"The search already matches this resume: "
                f"{', '.join(self.titles_after[:3])}."
            )

        return (
            f"Now searching for {', '.join(self.titles_after[:3])}"
            + (
                f" and {len(self.titles_after) - 3} more"
                if len(self.titles_after) > 3
                else ""
            )
            + (
                f". {self.jobs_from_previous_resumes} posting(s) found for an "
                f"earlier resume are no longer on the wire"
                if self.jobs_from_previous_resumes
                else ""
            )
            + "."
        )

    def to_dict(self) -> dict:
        """Serialize for the dashboard."""
        return {
            "master_id": self.master_id,
            "master_name": self.master_name,
            "search_profile_id": self.search_profile_id,
            "search_profile_name": self.search_profile_name,
            "titles_before": self.titles_before,
            "titles_after": self.titles_after,
            "changed_the_search": self.changed_the_search,
            "jobs_from_previous_resumes": self.jobs_from_previous_resumes,
            "applications_on_old_documents": self.applications_on_old_documents,
            "resume": self.resume_profile.to_dict() if self.resume_profile else None,
            "message": self.describe(),
        }


def active_master(session: Session) -> Optional[MasterDocument]:
    """
    The resume currently in use.

    Args:
        session: Database session

    Returns:
        The active master resume, or None
    """
    return (
        session.query(MasterDocument)
        .filter(
            MasterDocument.doc_type == DocumentType.RESUME,
            MasterDocument.is_active == True,  # noqa: E712 — SQL comparison
        )
        .first()
    )


def sync_to_resume(
    session: Session,
    master: Optional[MasterDocument] = None,
    search_profile: Optional[SearchProfile] = None,
) -> SyncResult:
    """
    Point the search, and therefore the wire, at the resume in use.

    Does not commit — the caller owns the transaction, so this lands together
    with whatever made the resume active in the first place.

    Args:
        session: Database session
        master: The resume to sync to. Defaults to the active one.
        search_profile: The profile to rewrite. Defaults to the active one.

    Returns:
        What changed, ready to report to the user
    """
    master = master or active_master(session)
    result = SyncResult()

    if not master:
        logger.info("No active master resume — nothing to sync the search to")
        return result

    result.master_id = master.id
    result.master_name = master.name
    result.resume_profile = read_resume(master.content_text or "")

    profile = search_profile or (
        session.query(SearchProfile)
        .filter(SearchProfile.is_active == True)  # noqa: E712
        .first()
    )

    if profile:
        result.search_profile_id = profile.id
        result.search_profile_name = profile.name
        result.titles_before = list(profile.target_titles or [])

    if result.resume_profile.is_usable and profile:
        _apply_to_search_profile(profile, master, result.resume_profile)
        result.titles_after = list(profile.target_titles or [])
    else:
        result.titles_after = result.titles_before

    result.jobs_from_previous_resumes = _count_stale_jobs(session, master.id)
    result.applications_on_old_documents = _count_stale_applications(session, master.id)

    logger.info(
        f"Synced the pipeline to '{master.name}': {result.describe()}"
    )

    return result


def _apply_to_search_profile(
    profile: SearchProfile, master: MasterDocument, resume: ResumeProfile
) -> None:
    """
    Rewrite a search profile's terms from what the resume supports.

    Only the fields the resume can speak to are touched. Location, remote
    preference, salary floor and exclusions are the user's own decisions and a
    resume says nothing about them — overwriting those would quietly undo
    settings the user chose deliberately.

    Args:
        profile: The search profile, mutated in place
        master: The resume it is being derived from
        resume: What the resume asks for
    """
    profile.target_titles = list(resume.titles)
    profile.alt_titles = list(resume.titles[1:])
    profile.keywords = list(resume.keywords)

    if resume.seniority:
        profile.seniority = resume.seniority

    profile.derived_from_master_id = master.id


def _count_stale_jobs(session: Session, master_id: int) -> int:
    """
    How many postings on record were found for a different resume.

    Args:
        session: Database session
        master_id: The resume now in use

    Returns:
        The count that will drop off the wire
    """
    return (
        session.query(Job)
        .filter(
            (Job.matched_master_id.is_(None)) | (Job.matched_master_id != master_id)
        )
        .count()
    )


def _count_stale_applications(session: Session, master_id: int) -> int:
    """
    Applications in the tray whose documents came from an older resume.

    Args:
        session: Database session
        master_id: The resume now in use

    Returns:
        The count worth rebuilding
    """
    waiting = (
        session.query(Application)
        .filter(
            Application.submission_status.in_(
                [ApplicationStatus.DRAFT, ApplicationStatus.QUEUED_FOR_REVIEW]
            )
        )
        .all()
    )

    stale = 0

    for application in waiting:
        if not application.resume_version_id:
            continue

        version = (
            session.query(DocumentVersion)
            .filter(DocumentVersion.id == application.resume_version_id)
            .first()
        )

        if version and version.master_document_id != master_id:
            stale += 1

    return stale


def stamp_jobs_with_master(session: Session, job_ids: List[int], master_id: int) -> int:
    """
    Record which resume a batch of newly collected postings was found for.

    Args:
        session: Database session
        job_ids: The postings collected
        master_id: The resume in use during the run

    Returns:
        How many were stamped
    """
    if not job_ids or not master_id:
        return 0

    stamped = (
        session.query(Job)
        .filter(Job.id.in_(job_ids))
        .update({Job.matched_master_id: master_id}, synchronize_session=False)
    )

    return stamped or 0


def application_uses_current_resume(
    session: Session, application: Application, master_id: Optional[int]
) -> bool:
    """
    Whether an application's attached resume came from the resume in use.

    Args:
        session: Database session
        application: The application
        master_id: The resume in use

    Returns:
        True when they agree, or when there is nothing to compare
    """
    if not master_id or not application.resume_version_id:
        return True

    version = (
        session.query(DocumentVersion)
        .filter(DocumentVersion.id == application.resume_version_id)
        .first()
    )

    if not version:
        return True

    return version.master_document_id == master_id
