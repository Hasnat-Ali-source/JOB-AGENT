"""
Connector API Routes (Phase 7).

- GET /api/v1/connectors — Every registered connector and what it can do
- GET /api/v1/connectors/{platform} — One connector in detail

The GUI uses this to show only the actions a platform actually supports, and to
surface terms-of-service risk notes before a user enables automation on a
consumer board. `verified_against` says how far each capability claim has been
tested, so "the connector claims it can submit" and "submitting has been proven
against the live site" stay distinguishable.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from job_agent.connectors import create_connector, list_connectors
from job_agent.dashboard.deps import get_session
from job_agent.models.database import PlatformAccount

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/connectors", tags=["connectors"])
SessionDep = get_session


def _describe(platform: str) -> Optional[dict]:
    """
    Describe one connector.

    Args:
        platform: Registry name

    Returns:
        Capability detail, or None if the connector can't be built
    """
    connector = create_connector(platform)

    if connector is None:
        return None

    if hasattr(connector, "describe"):
        return connector.describe()

    # Connectors predating Phase 7 (generic_ats, test_connector)
    capabilities = connector.capabilities

    return {
        "platform": connector.platform_name,
        "board": None,
        "search_url": getattr(connector, "search_url", None),
        "verified_against": None,
        "capabilities": {
            "search": capabilities.can_search,
            "filter": capabilities.can_filter,
            "read_details": capabilities.can_read_details,
            "start_application": capabilities.can_start_application,
            "fill_standard_fields": capabilities.can_fill_standard_fields,
            "upload_documents": capabilities.can_upload_documents,
            "process_custom_questions": capabilities.can_process_custom_questions,
            "submit_automatically": capabilities.can_submit_automatically,
        },
        "requires_manual_signin": capabilities.requires_manual_signin,
        "requires_manual_review_first_n": capabilities.requires_manual_review_first_n,
        "tos_risk_note": capabilities.tos_risk_note,
    }


@router.get("")
async def list_all(
    can_submit: Optional[bool] = Query(
        None, description="Filter to connectors that can or cannot submit"
    ),
    session: Session = Depends(SessionDep),
) -> dict:
    """
    List every registered connector with its capabilities.

    Args:
        can_submit: Optional filter on automatic submission support

    Returns:
        Connectors, grouped counts, and whether each is connected
    """
    connected = {
        account.platform: account
        for account in session.query(PlatformAccount).all()
    }

    described = []

    for platform in sorted(list_connectors()):
        detail = _describe(platform)

        if detail is None:
            logger.warning(f"Connector {platform} is registered but failed to construct")
            continue

        if can_submit is not None:
            if detail["capabilities"]["submit_automatically"] != can_submit:
                continue

        account = connected.get(platform)

        detail["connected"] = account is not None
        detail["automation_mode"] = (
            account.automation_mode.value if account else None
        )
        detail["clean_submissions_count"] = (
            account.clean_submissions_count if account else 0
        )

        described.append(detail)

    return {
        "total": len(described),
        "can_submit": sum(
            1 for d in described if d["capabilities"]["submit_automatically"]
        ),
        "read_only": sum(
            1 for d in described if not d["capabilities"]["submit_automatically"]
        ),
        "with_risk_notes": sum(1 for d in described if d["tos_risk_note"]),
        "connectors": described,
    }


@router.get("/{platform}")
async def get_connector(
    platform: str,
    session: Session = Depends(SessionDep),
) -> dict:
    """
    Describe one connector.

    Args:
        platform: Registry name

    Returns:
        Capability detail
    """
    detail = _describe(platform)

    if detail is None:
        raise HTTPException(
            status_code=404, detail=f"No connector registered for '{platform}'"
        )

    account = session.query(PlatformAccount).filter(
        PlatformAccount.platform == platform
    ).first()

    detail["connected"] = account is not None
    detail["automation_mode"] = account.automation_mode.value if account else None
    detail["clean_submissions_count"] = account.clean_submissions_count if account else 0

    return detail
