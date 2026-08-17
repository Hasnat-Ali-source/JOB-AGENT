"""Connectors package for Job Agent.

Each connector is a platform-specific implementation of ConnectedPlatformConnector,
enabling search, job reading, and application submission for that platform.

Build priority (§5 & §11):
1. Generic ATS/company career site fallback (Phase 2) ✅
2. Greenhouse, Lever, Ashby, Workday (Phase 2+)
3. SmartRecruiters, Workable
4. LinkedIn, Indeed (browser-assisted, read-only by default)
5. JobStreet, Glassdoor, ZipRecruiter, Wellfound, Dice
6. Email-based applications (special handler, not platform-specific)
"""

from job_agent.connectors.base import ConnectedPlatformConnector, PlatformCapabilities
from job_agent.connectors.registry import (
    get_registry,
    register_connector,
    unregister_connector,
    get_connector_class,
    create_connector,
    create_connector_for_account,
    get_connector_instance,
    list_connectors,
    is_connector_registered,
)

__all__ = [
    "ConnectedPlatformConnector",
    "PlatformCapabilities",
    "get_registry",
    "register_connector",
    "unregister_connector",
    "get_connector_class",
    "create_connector",
    "create_connector_for_account",
    "get_connector_instance",
    "list_connectors",
    "is_connector_registered",
]
