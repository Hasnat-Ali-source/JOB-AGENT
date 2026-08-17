# Architecture & Design Overview

## System Architecture Diagram

```
┌──────────────────────────────────────────────────────────────┐
│                  Dashboard GUI (Phase 10)                      │
│  Connected Accounts · Search Profiles · Review Queue · History │
│                        (FastAPI + HTMX/React)                  │
└──────────────┬──────────────────────┬──────────────────────────┘
               │                      │
       ┌───────▼─────────┐    ┌──────▼──────────┐
       │   Scheduler      │    │ Review Queue    │
       │  (launchd job)   │    │ Manual Gate     │
       │   (Phase 8)      │    │ (Phase 5)       │
       └───────┬─────────┘    └──────┬──────────┘
               │                     │
       ┌───────▼─────────────────────▼─────────┐
       │    Orchestrator (Phase 8)              │
       │ Loads profile → picks connectors →     │
       │ runs pipeline → writes results →       │
       │ queues for review                      │
       └───────┬──────────────┬─────────────────┘
               │              │
     ┌─────────▼──────┐  ┌────▼──────────┐
     │ Connector       │  │ Fit Scoring   │
     │ Framework       │  │ & Document    │
     │ (Phase 2)       │  │ Generation    │
     │ + Impls         │  │ (Phase 4)     │
     │ (Phase 7)       │  │               │
     └─────────┬──────┘  └────┬──────────┘
               │              │
     ┌─────────▼──────────────▼──────────┐
     │   Browser Automation              │
     │   Playwright (persistent contexts) │
     │   Per-platform profiles            │
     │   (Phase 1)                        │
     └─────────┬──────────────────────────┘
               │
     ┌─────────▼───────────────────────────┐
     │   Local Storage                      │
     ├──────────────────────────────────────┤
     │ • SQLite DB (jobs, applications)     │
     │ • macOS Keychain (session metadata)  │
     │ • Profile directories (browser data) │
     │ • PDFs (resumes, cover letters)      │
     └──────────────────────────────────────┘
```

## Data Flow: Job Search & Application

```
1. User selects search profile (e.g., "Senior Backend - Remote")
2. Orchestrator → for each enabled platform:
   a. Open persistent browser context → check_session()
   b. Navigate to search, apply filters
   c. Collect job links → respect daily_search_limit
   d. For each link:
      - read_job_details() → JobPosting
      - Compute dedup_hash → skip if duplicate
      - Run hard filters (salary, location, keywords)
      - Compute fit_score (local LLM or Claude API)
   e. Store new jobs to SQLite with fit_score
3. For jobs above fit_score_threshold:
   a. Generate tailored resume + cover letter
   b. begin_application() → open form
   c. fill_application() → fill known fields, defer unknowns
   d. Capture screenshot
   e. Queue for Review Queue if this is first N submissions
   f. Auto-submit if clean_submissions_count >= threshold & mode allows
4. Write audit log entries for every step
5. Return summary: jobs found, new, duplicates, submitted, queued, errors
```

## Connector Architecture

Each platform has a **connector** class inheriting from `ConnectedPlatformConnector`:

```python
class LinkedInConnector(ConnectedPlatformConnector):
    capabilities = PlatformCapabilities(
        can_search=True,
        can_filter=True,
        can_read_details=True,
        can_submit_automatically=False,  # Default for consumer boards
        tos_risk_note="Automated activity may violate LinkedIn's ToS"
    )
    
    async def check_session(self) → ConnectionStatus
    async def open_search(search_profile) → None
    async def apply_search_filters(search_profile) → None
    async def collect_job_links() → List[str]
    async def read_job_details(url) → JobPosting
    async def begin_application(job) → ApplicationSession
    async def fill_application(session, profile, package) → ApplicationSession
    async def submit_application(session) → SubmissionResult
```

Each method either:
- **Succeeds** and returns data
- **Fails with clear error** (wrong page, network error, etc.)
- **Pauses for manual intervention** (MFA, CAPTCHA, unknown field)

## Database Schema

### Core Tables

| Table | Purpose |
|-------|---------|
| `search_profiles` | Reusable search filters (titles, location, salary, etc.) |
| `platform_accounts` | User's authenticated sessions per platform |
| `jobs` | Discovered job postings (deduplicated across sources) |
| `applications` | User's applications (filled forms, submissions, status) |
| `audit_log` | Complete audit trail (every action, who, when, result) |
| `email_threads` | Tracking for email-based applications + replies |

### Relationships

```
SearchProfile
  ↓
  (N:M via join table, future)
  
PlatformAccount
  ↓ 1:N
  Application
  ↓ N:1
  Job
  
Job
  ↓ 1:N
  Application
  ↓ 1:1 (optional)
  EmailThread

AuditLog (references platform, action, result — no foreign keys)
```

## Security & Privacy

### What's Stored Locally

✓ Job postings (title, company, description, URL)  
✓ Application details (filled fields, status, confirmation)  
✓ Audit log (timestamps, actions, errors)  
✓ Session metadata (platform name, connection time, profile path)  
✓ PDFs (tailored resumes, cover letters)  

### What's NEVER Stored

✗ Passwords or account credentials  
✗ Session cookies or tokens (Playwright manages these in profile dir)  
✗ MFA codes or recovery keys  
✗ Payment information  
✗ Personal health or sensitive demographic data (unless user fills in a form field)  

### Keychain Usage

The `keyring` package stores:
- Platform connection metadata: `{platform_name, connected_at, last_verified_at}`
- App-specific email passwords (for IMAP/SMTP only, never real account passwords)

Keychain is macOS's native encrypted credential storage — no passwords are ever logged or transmitted.

## Automation Modes & Review Gates

### Per-Platform Account

Each platform account has:
- **Automation mode:** Search-only, Search+analyze, Search+prepare, Fill+auto-submit, Manual
- **Daily limits:** Max searches, max applications, max messages
- **Clean submissions count:** Tracks how many auto-submitted applications passed QA
- **First-N gate:** Minimum N clean submissions before auto-submit eligible

### Example Flow

**Day 1, LinkedIn, mode=Fill+auto-submit:**
1. Find 42 jobs → score → prepare 5 applications
2. Fill form for job #1 → screenshot → Queue for Review
   - User reviews & approves → submit
   - count = 1, not yet auto-submit eligible
3. Jobs #2–#4 → same as #1 (manually reviewed)
   - count = 4, now auto-submit eligible ✓
4. Job #5 → auto-fill → auto-submit (no review) ✓
5. Job #6 → daily limit reached, stop

**Day 2:**
- count resets? No — count is lifetime per platform, represents trust level
- Auto-submit continues for job #7, #8, etc. (unless mode changed)

## Async/Await Pattern

All connector methods are `async`:

```python
async def main():
    connector = LinkedInConnector()
    
    # Check session
    status = await connector.check_session()
    if status != ConnectionStatus.CONNECTED:
        # Reconnect or show error
        return
    
    # Run pipeline
    await connector.open_search(profile)
    await connector.apply_search_filters(profile)
    links = await connector.collect_job_links()
    
    for link in links:
        job = await connector.read_job_details(link)
        session = await connector.begin_application(job)
        session = await connector.fill_application(session, candidate, package)
        # Queue for review or auto-submit
```

Enables:
- Non-blocking I/O (network, browser automation)
- Concurrent tasks per platform (Phase 8)
- Clean error handling & timeouts

## Rate Limiting & Human-Like Behavior

To avoid triggering anti-bot detection or ToS violations:

```python
import asyncio
import random

async def paced_request(action):
    """Execute action with human-like delay."""
    min_delay = settings.request_delay_min  # 1.0s
    max_delay = settings.request_delay_max  # 5.0s
    delay = random.uniform(min_delay, max_delay)
    
    await asyncio.sleep(delay)
    return await action()
```

Applied to:
- Between search result page clicks
- Between applications
- Between email sends
- Between job detail reads

## Error Handling & Recovery (Phase 9)

### Session Expiry

1. check_session() detects auth failure → status = SESSION_EXPIRED
2. Dashboard shows "Reconnect" button
3. User clicks → browser opens to login
4. Playwright waits for auth indicators
5. On success → resume queued tasks for that platform

### CAPTCHA/MFA Mid-Run

1. Playwright detects challenge (no progress, specific indicators)
2. Pause that platform's task immediately
3. Bring browser window to foreground / notify user
4. Wait for explicit "I've completed it" confirmation
5. check_session() verifies success
6. Resume from paused step

### Network/Timeout Error

1. Log error to audit_log
2. Mark application status = "error"
3. Surface in dashboard with retry option
4. User can retry or skip

## Testing Strategy

### Phase 0 (Current)

- [x] Smoke test: Keychain access
- [x] Smoke test: Database initialization
- [x] Smoke test: Import all modules

### Phase 1

- [ ] Test: Persistent browser context creation
- [ ] Test: Session status detection (logged-in vs. login page)
- [ ] Test: Connect/disconnect flow with test platform

### Phase 2+

- [ ] Unit tests: Connector base class
- [ ] Integration tests: Generic ATS connector
- [ ] End-to-end: Test on real Greenhouse job posting
- [ ] Mocking: Playwright for speed (avoid real browser in CI/CD)

## File Organization

```
job_agent/
├── __init__.py
├── main.py                    # CLI entry point
├── config.py                  # Settings/configuration
├── models/
│   ├── __init__.py
│   └── database.py            # SQLModel schema
├── connectors/
│   ├── __init__.py
│   ├── base.py                # Base class
│   ├── generic_ats.py         # Generic fallback (Phase 2)
│   └── [platform].py          # LinkedIn, Greenhouse, etc. (Phase 7)
├── core/
│   ├── __init__.py
│   ├── session_manager.py     # Browser session handling (Phase 1)
│   ├── orchestrator.py        # Main pipeline (Phase 8)
│   ├── document_gen.py        # Resume/cover letter (Phase 4)
│   └── fit_scorer.py          # LLM scoring (Phase 3)
├── utils/
│   ├── __init__.py
│   ├── keychain.py            # Keychain integration (Phase 0)
│   ├── browser.py             # Playwright helpers (Phase 1)
│   └── dedup.py               # Job deduplication (Phase 3)
├── dashboard/
│   ├── __init__.py
│   ├── main.py                # FastAPI app
│   ├── routes/
│   │   ├── accounts.py        # Account management (Phase 1)
│   │   ├── search.py          # Search profiles (Phase 3)
│   │   ├── review_queue.py    # Review queue (Phase 5)
│   │   └── history.py         # Job history & audit log (Phase 10)
│   └── static/                # Frontend (HTML/CSS/JS)
└── scripts/
    ├── setup.py               # Phase 0 setup (all steps automated)
    ├── init_db.py             # Database initialization
    └── [test helpers]
```

## Configuration Hierarchy

1. **Defaults** → `config.py` BaseSettings defaults
2. **Environment variables** → Override via .env or shell
3. **Runtime** → CLI flags override everything

Example:
```bash
# Use defaults
python -m job_agent dashboard

# Override port via env var
DASHBOARD_PORT=8001 python -m job_agent dashboard

# Override via CLI flag
python -m job_agent dashboard --port 8001
```

## Next Phase

Phase 1: **Session Manager & Account Connection**

See [PHASES.md](PHASES.md#phase-1-session-manager--account-connection) for details.

---

**Last Updated:** 2026-08-14  
**Current Phase:** 0 (Environment & Infrastructure) ✓  
**Next Phase:** 1 (Session Manager)
