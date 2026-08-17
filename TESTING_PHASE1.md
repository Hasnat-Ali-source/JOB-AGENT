"""
Phase 1 Testing Guide — Quick Reference

Test the complete Phase 1 flow (Session Manager & Account Connection).
"""

# ============================================================================
# STEP-BY-STEP TESTING (Manual)
# ============================================================================

"""
REQUIREMENTS:
- macOS (for Keychain)
- Python 3.11+
- Playwright installed (playwright install)
- Job Agent installed locally

SETUP:
1. Initialize database:
   python -m job_agent init-db

2. Verify setup:
   python -m job_agent version
"""


# ============================================================================
# OPTION A: Test with Test Server (Recommended for Phase 1)
# ============================================================================

"""
STEP 1: Start Test Server (Port 8001)
   In Terminal 1:
   
   python scripts/test_server.py
   
   Output: "Starting test server on http://localhost:8001"
   
   This serves a stub HTML page with:
   - Login form (click "Sign In" button to simulate auth)
   - 3 test jobs after login
   - No real credentials needed


STEP 2: Start Dashboard (Port 8000)
   In Terminal 2:
   
   python -m job_agent dashboard --port 8000
   
   Output: "Uvicorn running on http://127.0.0.1:8000"
   
   Dashboard has account management endpoints:
   - GET /api/v1/accounts
   - POST /api/v1/accounts/connect
   - GET /api/v1/accounts/{platform}/status
   - POST /api/v1/accounts/{platform}/disconnect


STEP 3: Connect test_connector
   In Terminal 3:
   
   # Start connection flow
   curl -X POST "http://127.0.0.1:8000/api/v1/accounts/connect?platform=test_connector"
   
   Output:
   {
       "status": "browser_opened",
       "platform": "test_connector",
       "message": "Browser opened for test_connector. Please sign in."
   }
   
   → Browser window opens automatically to http://localhost:8001


STEP 4: In the Browser Window
   - See login form with "🔐 Sign In" button
   - See message: "How to use: Click the button below to simulate a login"
   - Click "Sign In" button
   - Page shows "✓ Logged in as: Test User"
   - 3 test jobs appear:
     * Senior Backend Engineer (TechCorp, SF)
     * Machine Learning Engineer (AIStart, Remote)
     * Frontend Developer (WebDesign Inc, NY)
   
   → Authentication is now complete


STEP 5: Verify Connection Status
   
   # Check if connection was saved
   curl "http://127.0.0.1:8000/api/v1/accounts/test_connector/status"
   
   Output:
   {
       "status": "connected",
       "platform": "test_connector",
       "automation_mode": "search_and_analyze",
       "daily_search_limit": 100,
       "daily_apply_limit": 5,
       "clean_submissions_count": 0,
       "last_verified_at": null,
       "last_error": null
   }


STEP 6: List All Accounts
   
   curl "http://127.0.0.1:8000/api/v1/accounts"
   
   Output: Array with test_connector account


STEP 7: Disconnect
   
   # Full disconnect (deletes profile dir + Keychain metadata + DB record)
   curl -X POST "http://127.0.0.1:8000/api/v1/accounts/test_connector/disconnect"
   
   Output:
   {
       "status": "disconnected",
       "platform": "test_connector",
       "message": "test_connector is now disconnected. All local data deleted."
   }
   
   → Browser window closes
   → Profile directory deleted (~/.../profiles/test_connector/)
   → Keychain entry deleted
   → Database record deleted


✅ Phase 1 Flow Complete!
"""


# ============================================================================
# OPTION B: Run Automated Integration Tests
# ============================================================================

"""
QUICK TEST:
   pytest tests/test_phase1_integration.py -v
   
   Output shows:
   - TestSessionManagerInit::test_profile_root_directory_exists ✓
   - TestSessionManagerInit::test_get_profile_dir_creates_platform_dir ✓
   - TestSessionManagerPlaywright::test_start_playwright ✓
   - TestSessionManagerPlaywright::test_stop_playwright ✓
   - TestSessionManagerConnectionDetection::test_check_session_status_no_context ✓
   - TestKeychainIntegration::test_store_and_retrieve_metadata ✓
   - TestKeychainIntegration::test_delete_metadata ✓
   - TestSingletonPattern::test_singleton_get_session_manager ✓
   - TestSingletonPattern::test_close_session_manager ✓
   - TestProfileDirManagement::test_profile_dir_structure ✓
   - test_full_connection_flow ✓


SPECIFIC TEST:
   pytest tests/test_phase1_integration.py::test_full_connection_flow -v
   
   This runs the complete end-to-end integration test.
"""


# ============================================================================
# OPTION C: Test with curl Commands
# ============================================================================

"""
LIST ACCOUNTS:
   curl -s http://127.0.0.1:8000/api/v1/accounts | jq .


CONNECT:
   curl -X POST "http://127.0.0.1:8000/api/v1/accounts/connect?platform=test_connector" | jq .


CHECK STATUS:
   curl http://127.0.0.1:8000/api/v1/accounts/test_connector/status | jq .


DISCONNECT:
   curl -X POST http://127.0.0.1:8000/api/v1/accounts/test_connector/disconnect | jq .


UPDATE AUTOMATION MODE:
   curl -X PATCH "http://127.0.0.1:8000/api/v1/accounts/test_connector/automation-mode?mode=search_only" | jq .
"""


# ============================================================================
# VERIFYING PHASE 1 ACCEPTANCE CRITERIA
# ============================================================================

"""
✅ Acceptance Criterion 1:
   "User clicks 'Connect <Platform>' → visible browser window opens, 
    user signs in manually"
   
   VERIFIED BY:
   - Running: curl -X POST "...connect?platform=test_connector"
   - Browser window appears automatically
   - User can see login form and sign in


✅ Acceptance Criterion 2:
   "On successful login, agent stores platform + timestamp + profile dir 
    in Keychain + SQLite"
   
   VERIFIED BY:
   - After signing in browser
   - Check curl /status → shows "status": "connected"
   - Keychain has entry: security find-generic-password -l platform_test_connector
   - SQLite has record: sqlite3 ~/.job-agent/job_agent.db "SELECT * FROM platformaccount;"


✅ Acceptance Criterion 3:
   "check_session() detects authenticated state without reading credential fields"
   
   VERIFIED BY:
   - SessionManager.check_session_status() uses CSS selectors
   - Never reads password fields
   - Never logs credentials
   - Only checks for visible elements (.user-menu, .login-form, etc.)


✅ Acceptance Criterion 4:
   "User can disconnect, which deletes profile dir + Keychain entry completely"
   
   VERIFIED BY:
   - Run: curl -X POST "...disconnect"
   - Response shows disconnected
   - Check: ls ~/.job-agent/profiles/test_connector/
     → Directory does not exist
   - Check: security find-generic-password -l platform_test_connector
     → Returns error (not found)
   - Check: sqlite3 db "SELECT COUNT(*) FROM platformaccount;"
     → Shows 0 records


✅ Acceptance Criterion 5:
   "All four steps (1–4 in §3) work end-to-end"
   
   VERIFIED BY:
   - Running the full test flow (Option A) start to finish
   - No errors or failures
   - All operations succeed as expected
"""


# ============================================================================
# TROUBLESHOOTING
# ============================================================================

"""
ERROR: "Browser failed to launch"
   SOLUTION:
   - Make sure Playwright is installed: playwright install
   - Check Chrome/Chromium is available

ERROR: "Failed to save metadata for test_connector"
   SOLUTION:
   - Check Keychain access: security list-keychains
   - May need to unlock Keychain: security unlock-keychain

ERROR: "Port 8001 already in use"
   SOLUTION:
   - Kill existing process: lsof -ti:8001 | xargs kill -9
   - Or use different port: python scripts/test_server.py --port 8002

ERROR: "Database locked"
   SOLUTION:
   - Make sure no other processes are using job_agent.db
   - Delete the .db file if corrupted: rm ~/.job-agent/job_agent.db
   - Reinitialize: python -m job_agent init-db

ERROR: Test browser doesn't connect to localhost:8001
   SOLUTION:
   - Make sure test server is running in Terminal 1
   - Check http://localhost:8001 directly in a browser
   - Check firewall: sudo lsof -i :8001
"""


# ============================================================================
# FILES & DIRECTORIES
# ============================================================================

"""
DATABASE:
   ~/.job-agent/job_agent.db
   
   Check accounts table:
   sqlite3 ~/.job-agent/job_agent.db "SELECT id, platform, status FROM platformaccount;"

PROFILE DIRECTORIES:
   ~/.job-agent/profiles/{platform}/
   
   Contains Playwright persistent context data:
   - Cookies
   - LocalStorage
   - IndexedDB
   - Cache
   
   Example: ~/.job-agent/profiles/test_connector/

KEYCHAIN ENTRIES:
   Stored as: platform_{platform}
   
   List all job-agent entries:
   security find-generic-password -l | grep platform_
   
   View one entry:
   security find-generic-password -l platform_test_connector

LOGS:
   Check SessionManager logs in terminal output:
   - "Launched browser for {platform}"
   - "Closed context for {platform}"
   - "Connection metadata saved for {platform}"
   - "Deleted Keychain metadata for {platform}"
"""


# ============================================================================
# NEXT STEPS (Phase 2)
# ============================================================================

"""
Phase 2 will implement real platform connectors:
1. LinkedIn Connector
   - LinkedIn login page detection
   - Job search results parsing
   - Job detail extraction

2. Greenhouse Connector
   - Greenhouse jobs page parsing
   - Company career pages

3. Indeed Connector
   - Indeed search and filtering

Each will extend ConnectedPlatformConnector base class
and implement the 8 required methods.

The SessionManager and account connection flow (Phase 1)
provides the foundation for all future connectors.
"""
