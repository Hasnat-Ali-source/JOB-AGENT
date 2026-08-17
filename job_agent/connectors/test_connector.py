"""
Test connector with stub HTML page (Phase 1).

For testing the entire connection flow without needing real job boards.
Simulates a job site that requires login and shows a job listing.

Usage:
    1. Start the test server: python scripts/test_server.py
    2. Create TestConnector instance
    3. Call connect() — opens browser to test page
    4. User can "sign in" (just clicks a button)
    5. Agent detects authenticated state
    6. Connection saved to database + Keychain

The stub page serves a simple job listing for testing the read_job_details flow.
"""

import logging
from typing import List, TYPE_CHECKING

from job_agent.connectors.base import (
    ConnectedPlatformConnector,
    PlatformCapabilities,
    JobPosting,
    ApplicationSession,
    SubmissionResult,
    ConnectionStatus,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from job_agent.models.database import SearchProfile



# Stub HTML page served by test server (see scripts/test_server.py)
TEST_PAGE_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Test Job Site</title>
    <style>
        * { font-family: -apple-system, BlinkMacSystemFont, sans-serif; }
        body { max-width: 1000px; margin: 0 auto; padding: 20px; }
        .login-form { display: block; border: 1px solid #ccc; padding: 20px; margin-bottom: 20px; }
        .login-form button { padding: 10px 20px; background: #0066cc; color: white; border: none; cursor: pointer; }
        .user-menu { display: none; padding: 10px; background: #f0f0f0; margin-bottom: 20px; }
        .user-menu.authenticated { display: block; }
        .job-listing { border: 1px solid #ddd; padding: 15px; margin-bottom: 10px; }
        .job-title { font-weight: bold; font-size: 18px; }
        .job-company { color: #666; }
        .job-details { margin-top: 10px; font-size: 14px; }
        button { padding: 5px 10px; }
    </style>
</head>
<body>
    <h1>Job Agent Test Site</h1>
    
    <div class="login-form" id="loginForm">
        <h2>Sign In</h2>
        <p>Click to simulate login (no credentials needed):</p>
        <button onclick="simulateLogin()">Sign In</button>
    </div>
    
    <div class="user-menu" id="userMenu">
        <strong>Logged in as: Test User</strong>
        <button onclick="simulateLogout()">Sign Out</button>
    </div>
    
    <div id="jobsContainer"></div>
    
    <script>
        // Load jobs when page loads (if authenticated)
        window.addEventListener('load', function() {
            if (localStorage.getItem('authenticated')) {
                document.getElementById('loginForm').style.display = 'none';
                document.getElementById('userMenu').classList.add('authenticated');
                loadJobs();
            }
        });
        
        function simulateLogin() {
            localStorage.setItem('authenticated', 'true');
            document.getElementById('loginForm').style.display = 'none';
            document.getElementById('userMenu').classList.add('authenticated');
            loadJobs();
        }
        
        function simulateLogout() {
            localStorage.removeItem('authenticated');
            document.getElementById('loginForm').style.display = 'block';
            document.getElementById('userMenu').classList.remove('authenticated');
            document.getElementById('jobsContainer').innerHTML = '';
        }
        
        function loadJobs() {
            const jobs = [
                {
                    id: '1',
                    title: 'Senior Backend Engineer',
                    company: 'TechCorp',
                    location: 'San Francisco, CA',
                    type: 'full_time',
                    salary: '$120,000 - $160,000',
                    description: 'We are looking for a Senior Backend Engineer to join our team.',
                    requirements: 'Python, PostgreSQL, AWS, 5+ years experience',
                    posted: '2 days ago'
                },
                {
                    id: '2',
                    title: 'Machine Learning Engineer',
                    company: 'AIStart',
                    location: 'Remote',
                    type: 'full_time',
                    salary: '$140,000 - $180,000',
                    description: 'Build ML models and deploy to production.',
                    requirements: 'Python, TensorFlow, MLOps, 3+ years experience',
                    posted: '1 day ago'
                },
                {
                    id: '3',
                    title: 'Frontend Developer',
                    company: 'WebDesign Inc',
                    location: 'New York, NY',
                    type: 'full_time',
                    salary: '$100,000 - $130,000',
                    description: 'Build user interfaces for web applications.',
                    requirements: 'React, TypeScript, CSS, 2+ years experience',
                    posted: '3 days ago'
                }
            ];
            
            const container = document.getElementById('jobsContainer');
            container.innerHTML = '<h2>Available Jobs</h2>';
            
            jobs.forEach(job => {
                const jobDiv = document.createElement('div');
                jobDiv.className = 'job-listing';
                jobDiv.innerHTML = `
                    <div class="job-title">${job.title}</div>
                    <div class="job-company">${job.company}</div>
                    <div class="job-details">
                        <strong>Location:</strong> ${job.location}<br>
                        <strong>Type:</strong> ${job.type}<br>
                        <strong>Salary:</strong> ${job.salary}<br>
                        <strong>Posted:</strong> ${job.posted}<br>
                        <div style="margin-top: 10px;">
                            <strong>Description:</strong> ${job.description}<br>
                            <strong>Requirements:</strong> ${job.requirements}
                        </div>
                        <button onclick="openApplication('${job.id}')">Apply</button>
                        <a href="/job/${job.id}" style="margin-left: 10px;">View Details</a>
                    </div>
                </div>
            `;
                container.appendChild(jobDiv);
            });
        }
        
        function openApplication(jobId) {
            alert('Application form would open here');
        }
    </script>
</body>
</html>
"""


class TestConnector(ConnectedPlatformConnector):
    """
    Test connector for Phase 1 development and testing.
    
    Implements a stub job site that:
    - Shows a login form
    - Allows simulated "sign in" (just localStorage update)
    - Displays 3 test jobs after login
    - Can read job details
    
    Useful for testing:
    - Connection flow (browser launch, detection of auth state)
    - Session persistence (Playwright context)
    - Keychain metadata storage
    - Database integration
    
    Not for production — real connectors (LinkedIn, Greenhouse, etc.)
    will be implemented in Phase 2–7.
    """

    # Name starts with "Test" but this is a connector, not a pytest test class
    __test__ = False


    def __init__(self, platform_name: str = "test_connector"):
        """
        Initialize test connector.

        Args:
            platform_name: Platform identifier (registry passes this in)
        """
        capabilities = PlatformCapabilities(
            can_search=True,
            can_filter=False,
            can_read_details=True,
            can_start_application=False,  # Not implemented in test
            can_fill_standard_fields=False,  # Not implemented in test
            can_upload_documents=False,
            can_process_custom_questions=False,
            can_submit_automatically=False,
            requires_manual_signin=True,
            requires_manual_review_first_n=3,
            tos_risk_note=None,
        )
        
        super().__init__(platform_name, capabilities)
        
        # Test data
        self.test_jobs = [
            {
                "id": "1",
                "title": "Senior Backend Engineer",
                "company": "TechCorp",
                "location": "San Francisco, CA",
                "job_type": "full_time",
                "salary": "$120,000 - $160,000",
                "description": "We are looking for a Senior Backend Engineer to join our team. You will work on distributed systems, microservices, and cloud infrastructure.",
                "requirements": "Python, PostgreSQL, AWS, 5+ years experience with backend systems",
                "posted_at": "2026-08-12T10:00:00Z",
            },
            {
                "id": "2",
                "title": "Machine Learning Engineer",
                "company": "AIStart",
                "location": "Remote",
                "job_type": "full_time",
                "salary": "$140,000 - $180,000",
                "description": "Build ML models and deploy to production. Join a team that's pushing the boundaries of AI.",
                "requirements": "Python, TensorFlow, MLOps, 3+ years experience with production ML systems",
                "posted_at": "2026-08-13T14:30:00Z",
            },
            {
                "id": "3",
                "title": "Frontend Developer",
                "company": "WebDesign Inc",
                "location": "New York, NY",
                "job_type": "full_time",
                "salary": "$100,000 - $130,000",
                "description": "Build user interfaces for web applications. Create amazing experiences for millions of users.",
                "requirements": "React, TypeScript, CSS, 2+ years experience with modern web technologies",
                "posted_at": "2026-08-11T09:15:00Z",
            },
        ]
    
    async def check_session(self) -> 'ConnectionStatus':
        """
        Check if user is authenticated on the test site.
        
        Looks for localStorage.authenticated flag set by the stub page.
        """
        logger.info("Checking session on test connector")
        
        # For test purposes, assume connected if context exists
        # In real connectors, this would check page elements
        return 'connected'
    
    async def open_search(self, search_profile: 'SearchProfile') -> None:
        """
        Navigate to the test site's job search page.
        
        For test purposes, just navigate to the main page.
        
        Args:
            search_profile: Search filters (ignored for test site)
        """
        logger.info("Opening test site job search page")
        # In real implementation, would navigate to actual search page
        # For now, this is a placeholder
    
    async def apply_search_filters(self, search_profile: 'SearchProfile') -> None:
        """
        Apply search filters on the test site.
        
        For test purposes, no filters available on stub page.
        
        Args:
            search_profile: Search filters
        """
        logger.info("Applying filters on test site (no-op for test)")
    
    async def collect_job_links(self) -> List[str]:
        """
        Collect job links from the test site.
        
        Returns links to the 3 test jobs.
        
        Returns:
            List of job URLs
        """
        logger.info("Collecting job links from test site")
        
        # Test URLs (would be real URLs in production)
        return [
            "http://localhost:8001/job/1",
            "http://localhost:8001/job/2",
            "http://localhost:8001/job/3",
        ]
    
    async def read_job_details(self, job_url: str) -> 'JobPosting':
        """
        Read job details from a test job posting.
        
        Args:
            job_url: Job URL (format: http://localhost:8001/job/{id})
        
        Returns:
            JobPosting with parsed details
        """
        # Extract job ID from URL
        try:
            job_id = job_url.split("/job/")[-1]
            job = next((j for j in self.test_jobs if j["id"] == job_id), None)
            
            if not job:
                logger.warning(f"Job {job_id} not found in test data")
                job = self.test_jobs[0]  # Return first job as fallback
            
            logger.info(f"Read details for test job {job['id']}")
            
            return JobPosting(
                platform=self.platform_name,
                external_id=job["id"],
                title=job["title"],
                company=job["company"],
                location=job["location"],
                job_type=job["job_type"],
                description=job["description"],
                requirements=job["requirements"],
                salary=job["salary"],
                posted_at=job["posted_at"],
                apply_method="web_form",
                apply_url=job_url,
            )
        except Exception as e:
            logger.error(f"Error reading job details: {e}")
            # Return a minimal job posting
            return JobPosting(
                platform=self.platform_name,
                external_id="unknown",
                title="Test Job",
                company="Test Company",
                location="Unknown",
                description="Test job posting",
            )
    
    async def begin_application(self, job: 'JobPosting') -> 'ApplicationSession':
        """
        Begin an application for a test job.
        
        Args:
            job: JobPosting to apply for
        
        Returns:
            ApplicationSession
        """
        logger.info(f"Beginning application for {job.title}")
        
        return ApplicationSession(
            job=job,
            platform_account_id=0,  # Not set for test
            form_url="http://localhost:8001/apply",
        )
    
    async def fill_application(
        self,
        session: 'ApplicationSession',
        candidate_profile: dict,
        application_package: dict
    ) -> 'ApplicationSession':
        """
        Fill in application fields.
        
        Not implemented for test connector.
        """
        logger.info("Fill application not implemented for test connector")
        return session
    
    async def submit_application(self, session: 'ApplicationSession') -> 'SubmissionResult':
        """
        Submit the application.
        
        Not implemented for test connector.
        """
        logger.info("Submit application not implemented for test connector")
        
        return SubmissionResult(
            success=False,
            error_message="Test connector does not support submission",
        )
