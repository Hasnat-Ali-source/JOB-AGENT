#!/usr/bin/env python3
"""
Phase 2 Quick Reference — Connector Framework

Fast lookup for Phase 2 APIs and patterns.
"""

# 1. CONNECTOR REGISTRY

from job_agent.connectors import (
    get_registry,
    register_connector,
    create_connector,
    list_connectors,
    is_connector_registered,
)

# Get registry
registry = get_registry()

# List connectors
connectors = list_connectors()
# {'test_connector': <class TestConnector>, 
#  'generic_ats': <class GenericATSConnector>}

# Check if registered
if is_connector_registered("generic_ats"):
    connector = create_connector("generic_ats")


# 2. GENERIC ATS CONNECTOR

from job_agent.connectors.generic_ats import GenericATSConnector

# Create instance
connector = GenericATSConnector()

# Check capabilities
print(f"Can read details: {connector.capabilities.can_read_details}")
print(f"Can start application: {connector.capabilities.can_start_application}")
print(f"Can submit: {connector.capabilities.can_submit_automatically}")
# Can read details: True
# Can start application: True
# Can submit: False (Phase 3+)

# Set page from SessionManager
from job_agent.core.session_manager import get_session_manager
session_manager = get_session_manager()
page = await session_manager.get_page("linkedin")
connector.set_page(page)

# Collect jobs
jobs = await connector.collect_job_links()
# [url1, url2, url3, ...]

# Read job details
from job_agent.connectors.base import JobPosting
job_posting = await connector.read_job_details(jobs[0])
# JobPosting(
#   platform='generic_ats',
#   title='Senior Engineer',
#   company='TechCorp',
#   location='San Francisco, CA',
#   description='...',
#   salary='USD 100,000 - 150,000',
#   apply_method='web_form'
# )

# Begin application
session = await connector.begin_application(job_posting)
# ApplicationSession(
#   job=job_posting,
#   form_url='https://...'
# )


# 3. REGISTERING A NEW CONNECTOR

from job_agent.connectors.base import ConnectedPlatformConnector, PlatformCapabilities

class LinkedInConnector(ConnectedPlatformConnector):
    def __init__(self):
        capabilities = PlatformCapabilities(
            can_search=True,
            can_filter=True,
            can_read_details=True,
            can_start_application=True,
            can_fill_standard_fields=False,  # Phase 3+
            can_submit_automatically=False,  # Phase 3+
            requires_manual_signin=True,
        )
        super().__init__("linkedin", capabilities)
    
    async def check_session(self):
        # LinkedIn-specific auth check
        return "connected"
    
    async def collect_job_links(self):
        # LinkedIn-specific job listing parsing
        return []
    
    # ... implement other methods

# Register
register_connector("linkedin", LinkedInConnector)

# Use
connector = create_connector("linkedin")


# 4. CAPABILITIES REFERENCE

# From Phase 2, all connectors have:
capabilities = {
    "can_search": True,              # Can navigate to search
    "can_filter": True,              # Can apply filters
    "can_read_details": True,        # Can extract job info
    "can_start_application": True,   # Can open apply form
    "can_fill_standard_fields": False,   # Phase 3+
    "can_upload_documents": False,   # Phase 3+
    "can_process_custom_questions": False,  # Phase 3+
    "can_submit_automatically": False,  # Phase 3+
    "requires_manual_signin": True,  # User signs in
    "requires_manual_review_first_n": 5,  # Review first 5
}


# 5. JOB POSTING STRUCTURE

from job_agent.connectors.base import JobPosting

job = JobPosting(
    platform="generic_ats",
    external_id="job-123",
    title="Senior Engineer",
    company="TechCorp",
    location="San Francisco, CA",
    job_type="full_time",
    description="Write code...",
    requirements="5+ years experience",
    salary="USD 100,000 - 150,000",
    posted_at="2026-08-14T00:00:00Z",
    apply_method="web_form",  # or "email"
    apply_url="https://...",
)

print(job.title)       # "Senior Engineer"
print(job.company)     # "TechCorp"
print(job.salary)      # "USD 100,000 - 150,000"


# 6. COMMON PATTERNS

# Get connector for platform
def get_platform_connector(platform_name: str):
    """Get or create connector for platform."""
    from job_agent.connectors import create_connector
    return create_connector(platform_name)

# List all available platforms
def list_platforms():
    """List all available platforms."""
    from job_agent.connectors import list_connectors
    return list(list_connectors().keys())

# Check if platform supported
def is_platform_available(platform_name: str) -> bool:
    """Check if platform has a connector."""
    from job_agent.connectors import is_connector_registered
    return is_connector_registered(platform_name)

# Use with SessionManager
async def search_jobs_on_platform(platform: str, profile):
    """Search for jobs on a platform."""
    from job_agent.core.session_manager import get_session_manager
    from job_agent.connectors import create_connector
    
    session_manager = get_session_manager()
    connector = create_connector(platform)
    
    # Get authenticated page
    page = await session_manager.get_page(platform)
    connector.set_page(page)
    
    # Search
    await connector.open_search(profile)
    await connector.apply_search_filters(profile)
    
    # Collect jobs
    links = await connector.collect_job_links()
    
    # Read details
    jobs = []
    for link in links:
        job = await connector.read_job_details(link)
        jobs.append(job)
    
    return jobs


# 7. ERROR HANDLING

try:
    connector = create_connector("linkedin")
    
    # Set page context
    from playwright.async_api import Page
    await connector.set_page(page)
    
    # Collect with error handling
    links = await connector.collect_job_links()
    if not links:
        print("No jobs found")
    
    # Read details with validation
    for link in links:
        try:
            job = await connector.read_job_details(link)
            if job.title:
                print(f"Found: {job.title}")
        except Exception as e:
            print(f"Error reading {link}: {e}")

except Exception as e:
    print(f"Connector error: {e}")


# 8. RUNNING TESTS

# In terminal:
# pytest tests/test_phase2_connectors.py -v
# 
# Output:
# test_phase2_connectors.py::TestGenericATSConnector::test_initialization PASSED
# test_phase2_connectors.py::TestGenericATSConnector::test_capabilities PASSED
# test_phase2_connectors.py::TestConnectorRegistry::test_registry_initialization PASSED
# ...
# ======= 20 passed in 0.42s =======


# 9. PHASE 3 PREVIEW

# Phase 3 will add:
# - Job search and filtering
# - Deduplication logic
# - Hard filter evaluation
# - Job storage to database
# - Dashboard search UI

# Connectors already have the foundation!
# Just need:
# - fill_application() implementation
# - submit_application() implementation
# - More platform-specific connectors (LinkedIn, Greenhouse, Indeed, etc.)

