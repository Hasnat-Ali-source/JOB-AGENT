#!/usr/bin/env python3
"""
Test server for Phase 1 testing (stub job site).

Serves the test HTML page that simulates a job site requiring login.
Useful for testing the entire Phase 1 flow:
1. User clicks "Connect test_connector"
2. Browser opens to http://localhost:8001
3. User sees login page
4. User clicks "Sign In" to simulate authentication
5. Agent detects authenticated state
6. Connection saved to database + Keychain

Run this server in a separate terminal:
    python scripts/test_server.py

Then connect to it via:
    POST /api/v1/accounts/connect?platform=test_connector
"""

import logging
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(title="Job Agent Test Server", version="0.1.0")

# Add CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Stub HTML page (same as in test_connector.py)
TEST_PAGE_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Test Job Site</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { font-family: -apple-system, BlinkMacSystemFont, sans-serif; }
        body { max-width: 1000px; margin: 0 auto; padding: 20px; background: #f5f5f5; }
        .container { background: white; border-radius: 8px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        h1 { color: #333; }
        .login-form { display: block; border: 1px solid #ddd; padding: 20px; margin-bottom: 20px; border-radius: 8px; background: #fafafa; }
        .login-form button { padding: 12px 24px; background: #0066cc; color: white; border: none; cursor: pointer; border-radius: 4px; font-size: 16px; font-weight: 500; }
        .login-form button:hover { background: #0052a3; }
        .user-menu { display: none; padding: 15px; background: #e8f5e9; margin-bottom: 20px; border-radius: 8px; border-left: 4px solid #4caf50; }
        .user-menu.authenticated { display: block; }
        .user-menu button { padding: 8px 16px; background: #f44336; color: white; border: none; cursor: pointer; border-radius: 4px; font-size: 14px; }
        .user-menu button:hover { background: #d32f2f; }
        .job-listing { border: 1px solid #e0e0e0; padding: 15px; margin-bottom: 10px; border-radius: 8px; background: white; }
        .job-listing:hover { box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .job-title { font-weight: bold; font-size: 18px; color: #0066cc; margin-bottom: 5px; }
        .job-company { color: #666; font-size: 14px; margin-bottom: 8px; }
        .job-details { margin-top: 10px; font-size: 14px; line-height: 1.5; }
        .job-details strong { color: #333; }
        .job-details a { color: #0066cc; text-decoration: none; }
        .job-details a:hover { text-decoration: underline; }
        button { padding: 5px 10px; margin-right: 5px; }
        #jobsContainer { display: none; }
        #jobsContainer.loaded { display: block; }
        .jobs-header { display: none; margin-top: 20px; margin-bottom: 15px; }
        .jobs-header.loaded { display: block; }
        .message { padding: 10px; border-radius: 4px; margin-bottom: 10px; }
        .message.info { background: #e3f2fd; color: #1565c0; border: 1px solid #90caf9; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🧪 Job Agent Test Site</h1>
        <p>This is a test site for Phase 1 development. Click "Sign In" below to test the authentication flow.</p>
        
        <div class="message info">
            <strong>How to use:</strong> Click the "Sign In" button below to simulate logging in. This stores an authentication flag in localStorage.
        </div>
        
        <div class="login-form" id="loginForm">
            <h2>Sign In</h2>
            <p>This is a test page. Click the button below to simulate a login:</p>
            <button onclick="simulateLogin()">🔐 Sign In</button>
        </div>
        
        <div class="user-menu" id="userMenu">
            <strong>✓ Logged in as: Test User</strong>
            <button onclick="simulateLogout()">Sign Out</button>
        </div>
        
        <h2 class="jobs-header" id="jobsHeader">Available Jobs</h2>
        <div id="jobsContainer"></div>
    </div>
    
    <script>
        // Check authentication on page load
        window.addEventListener('load', function() {
            if (localStorage.getItem('authenticated')) {
                showAuthenticatedState();
            }
        });
        
        function simulateLogin() {
            console.log('User clicked Sign In');
            localStorage.setItem('authenticated', 'true');
            showAuthenticatedState();
        }
        
        function simulateLogout() {
            console.log('User clicked Sign Out');
            localStorage.removeItem('authenticated');
            hideAuthenticatedState();
        }
        
        function showAuthenticatedState() {
            document.getElementById('loginForm').style.display = 'none';
            document.getElementById('userMenu').classList.add('authenticated');
            loadJobs();
        }
        
        function hideAuthenticatedState() {
            document.getElementById('loginForm').style.display = 'block';
            document.getElementById('userMenu').classList.remove('authenticated');
            document.getElementById('jobsContainer').innerHTML = '';
            document.getElementById('jobsHeader').classList.remove('loaded');
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
                    description: 'We are looking for a Senior Backend Engineer to join our core infrastructure team. You will work on distributed systems, microservices, and cloud infrastructure.',
                    requirements: 'Python, PostgreSQL, AWS, 5+ years experience with backend systems',
                    posted: '2 days ago'
                },
                {
                    id: '2',
                    title: 'Machine Learning Engineer',
                    company: 'AIStart',
                    location: 'Remote',
                    type: 'full_time',
                    salary: '$140,000 - $180,000',
                    description: 'Build ML models and deploy to production. Join a team that is pushing the boundaries of AI and machine learning.',
                    requirements: 'Python, TensorFlow, MLOps, 3+ years experience with production ML systems',
                    posted: '1 day ago'
                },
                {
                    id: '3',
                    title: 'Frontend Developer',
                    company: 'WebDesign Inc',
                    location: 'New York, NY',
                    type: 'full_time',
                    salary: '$100,000 - $130,000',
                    description: 'Build beautiful user interfaces for web applications. Create amazing experiences for millions of users worldwide.',
                    requirements: 'React, TypeScript, CSS, 2+ years experience with modern web technologies',
                    posted: '3 days ago'
                }
            ];
            
            const container = document.getElementById('jobsContainer');
            container.innerHTML = '';
            
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
                            <strong>Description:</strong><br>
                            ${job.description}<br>
                            <strong>Requirements:</strong><br>
                            ${job.requirements}
                        </div>
                        <div style="margin-top: 10px;">
                            <button onclick="openApplication('${job.id}')">Apply Now</button>
                            <a href="/job/${job.id}">View Full Details</a>
                        </div>
                    </div>
                `;
                container.appendChild(jobDiv);
            });
            
            container.classList.add('loaded');
            document.getElementById('jobsHeader').classList.add('loaded');
        }
        
        function openApplication(jobId) {
            alert('Application would open for job ' + jobId);
        }
    </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve the main test page."""
    return TEST_PAGE_HTML


@app.get("/job/{job_id}", response_class=HTMLResponse)
async def job_detail(job_id: str):
    """Serve job detail page (stub)."""
    job_titles = {
        "1": "Senior Backend Engineer",
        "2": "Machine Learning Engineer",
        "3": "Frontend Developer",
    }
    
    title = job_titles.get(job_id, "Job Details")
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>{title}</title>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 1000px; margin: 0 auto; padding: 20px; }}
            a {{ color: #0066cc; text-decoration: none; }}
            a:hover {{ text-decoration: underline; }}
        </style>
    </head>
    <body>
        <h1>{title}</h1>
        <p>Job ID: {job_id}</p>
        <p><a href="/">← Back to Job List</a></p>
    </body>
    </html>
    """
    return html


APPLICATION_FORM_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Apply — Senior Backend Engineer</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 680px; margin: 0 auto; padding: 24px; color: #1a1a1a; }
        fieldset { border: 1px solid #ddd; border-radius: 8px; padding: 16px 20px; margin-bottom: 18px; }
        legend { font-weight: 600; padding: 0 6px; }
        label { display: block; margin-top: 12px; font-size: 14px; font-weight: 500; }
        input[type=text], input[type=email], input[type=tel], input[type=url], select, textarea {
            width: 100%; padding: 9px; margin-top: 4px; border: 1px solid #ccc; border-radius: 4px; font-size: 14px;
        }
        .radio-row { margin-top: 6px; font-weight: 400; }
        .note { color: #666; font-size: 13px; margin-top: 4px; }
        button { padding: 11px 22px; background: #0066cc; color: white; border: none; cursor: pointer; border-radius: 4px; font-size: 15px; }
    </style>
</head>
<body>
    <h1>Apply — Senior Backend Engineer</h1>
    <form id="applicationForm">

        <fieldset>
            <legend>Your details</legend>
            <label for="fullName">Full name *</label>
            <input type="text" id="fullName" name="full_name" required>

            <label for="emailAddr">Email address *</label>
            <input type="email" id="emailAddr" name="email" required>

            <label for="phoneNum">Phone number</label>
            <input type="tel" id="phoneNum" name="phone">

            <label for="cityField">Location (city)</label>
            <input type="text" id="cityField" name="location">

            <label for="linkedinField">LinkedIn profile</label>
            <input type="url" id="linkedinField" name="linkedin">

            <label for="githubField">GitHub profile</label>
            <input type="url" id="githubField" name="github">
        </fieldset>

        <fieldset>
            <legend>Documents</legend>
            <label for="resumeUpload">Resume / CV *</label>
            <input type="file" id="resumeUpload" name="resume" required>

            <label for="coverUpload">Cover letter</label>
            <input type="file" id="coverUpload" name="cover_letter">
        </fieldset>

        <fieldset>
            <legend>About you</legend>
            <label for="whyUs">Why do you want to work here? *</label>
            <textarea id="whyUs" name="why_us" rows="4" required></textarea>

            <label for="heardAbout">How did you hear about this role?</label>
            <select id="heardAbout" name="heard_about">
                <option value="">Please select</option>
                <option>LinkedIn</option>
                <option>A friend</option>
                <option>Our careers page</option>
                <option>Other</option>
            </select>

            <label for="noticeField">Notice period</label>
            <input type="text" id="noticeField" name="notice_period">

            <label for="authField">Are you authorized to work in this country? *</label>
            <select id="authField" name="work_authorization" required>
                <option value="">Please select</option>
                <option>Yes</option>
                <option>No</option>
            </select>
        </fieldset>

        <fieldset>
            <legend>Compensation</legend>
            <label for="currentSalary">Current salary</label>
            <input type="text" id="currentSalary" name="current_salary">

            <label for="expectedSalary">Expected salary *</label>
            <input type="text" id="expectedSalary" name="expected_salary" required>
        </fieldset>

        <fieldset>
            <legend>Voluntary self-identification</legend>
            <p class="note">
                Completing this section is entirely voluntary. It is used for
                equal-opportunity reporting and does not affect your application.
            </p>

            <label for="genderField">Gender</label>
            <select id="genderField" name="gender">
                <option value="">I prefer not to say</option>
                <option>Female</option>
                <option>Male</option>
                <option>Non-binary</option>
            </select>

            <label for="raceField">Race / ethnicity</label>
            <select id="raceField" name="race_ethnicity">
                <option value="">I prefer not to say</option>
                <option>Asian</option>
                <option>Black or African American</option>
                <option>Hispanic or Latino</option>
                <option>White</option>
                <option>Two or more races</option>
            </select>

            <label for="veteranField">Protected veteran status</label>
            <select id="veteranField" name="veteran_status">
                <option value="">I prefer not to say</option>
                <option>I am a protected veteran</option>
                <option>I am not a protected veteran</option>
            </select>

            <label for="disabilityField">Do you have a disability?</label>
            <select id="disabilityField" name="disability_status">
                <option value="">I prefer not to say</option>
                <option>Yes</option>
                <option>No</option>
            </select>
        </fieldset>

        <button type="submit">Submit Application</button>
    </form>

    <script>
        document.getElementById('applicationForm').addEventListener('submit', function (e) {
            e.preventDefault();
            document.body.insertAdjacentHTML('beforeend',
                '<p id="submitted">Application received. Reference: TEST-12345</p>');
        });
    </script>
</body>
</html>
"""


@app.get("/apply", response_class=HTMLResponse)
async def apply():
    """
    Serve a realistic application form for Phase 5 testing.

    Deliberately mixes the four field categories the agent must tell apart:
    fields it can fill from the profile, document uploads, questions only the
    user can answer (compensation, demographics, disability, veteran status),
    and open questions it has no basis to answer ("Why do you want to work
    here?").
    """
    return APPLICATION_FORM_HTML


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "service": "test-server"}


if __name__ == "__main__":
    import uvicorn
    
    logger.info("Starting test server on http://localhost:8001")
    logger.info("Visit http://localhost:8001 to see the test page")
    logger.info("Use this with Phase 1: python -m job_agent connect test_connector")
    
    uvicorn.run(app, host="127.0.0.1", port=8001, log_level="info")
