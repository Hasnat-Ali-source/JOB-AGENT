# Project Progress Tracker

## Current Phase: 0 — Environment & Infrastructure

### Checklist

- [ ] Python 3.11+ venv configured
- [ ] Playwright installed (+ browsers: chromium, firefox)
- [ ] SQLite schema migrations (SQLModel models)
- [ ] Keychain access module via `keyring`
- [ ] launchd plist scaffold (disabled by default)
- [ ] Project structure initialized
- [ ] requirements.txt pinned
- [ ] Initial git repo + .gitignore

### Completed Tasks

- ✓ Project repository initialized
- ✓ README.md created
- ✓ PHASES.md build roadmap documented

### Next Immediate Steps

1. **Create requirements.txt** with all Phase 0 dependencies
2. **Create Python package structure** (job_agent/ with __init__.py)
3. **Create SQLModel data models** (SearchProfile, PlatformAccount, Job, Application, AuditLog, EmailThread)
4. **Create Keychain module** (keyring wrapper for secure metadata storage)
5. **Create database initialization script** (init_db.py)
6. **Create Playwright session manager stub** (ready for Phase 1)
7. **Create launchd plist scaffold** (disabled, examples for scheduling)
8. **Set up git & .gitignore**

---

## Phase 1: Session Manager & Account Connection

**Status:** Not yet started  
**Planned Start:** After Phase 0 complete

### Acceptance Criteria
- [ ] User can connect platform via visible browser window
- [ ] Session metadata stored in Keychain (no credentials)
- [ ] Connection status accurately reflects authentication state
- [ ] Disconnect deletes profile dir + Keychain entry
- [ ] Test connector works end-to-end

---

## Phase 2: Connector Framework

**Status:** Not yet started  
**Planned Start:** After Phase 1 complete

### Acceptance Criteria
- [ ] Base connector class defined
- [ ] Capabilities struct honest & complete
- [ ] Generic ATS connector reads job details
- [ ] Can identify apply method (form vs. email)

---

## Notes & Decisions

### Tech Stack Confirmed
- **Language:** Python 3.11+
- **Browser:** Playwright + persistent contexts
- **DB:** SQLite + SQLModel ORM
- **Secrets:** macOS Keychain via `keyring`
- **LLM (optional):** Ollama (free/local) or Claude API
- **Email:** Mail.app (default) or IMAP/SMTP (fallback)
- **Frontend:** FastAPI + HTMX/React
- **Scheduling:** launchd (native macOS)

### Design Decisions
1. **No password storage** — Keychain stores only platform + timestamp + profile path
2. **Manual review gate first** — Auto-submit only after N clean submissions proven
3. **Consumer boards read-only by default** — Respects Terms of Service, requires explicit opt-in
4. **Paced automation** — Randomized delays to mimic human behavior
5. **Audit log everything** — Full traceability of all actions

### Known Risks & Mitigations
- **ToS violations:** Consumer boards may suspend accounts for automated activity. Mitigated by:
  - Read-only default for LinkedIn, Indeed, etc.
  - Explicit opt-in with risk warning for auto-submit
  - Paced, human-like delays
  - Full audit trail for compliance review
- **MFA/CAPTCHA:** Platform challenges mid-run. Mitigated by:
  - Visible browser windows (user can respond)
  - Pause + wait for user confirmation
  - No bypass attempts
- **Session expiry:** Connectivity loss mid-run. Mitigated by:
  - Per-platform session status tracking
  - Automatic pause of affected platform only
  - Resume capability via "Reconnect" button

---

## Timeline & Milestones

**Week 1:** Phase 0 — Environment setup  
**Week 2:** Phase 1 — Session manager  
**Week 3–4:** Phase 2–3 — Connector framework + job search  
**Week 5–6:** Phase 4–5 — Documents + application filling  
**Week 7:** Phase 6–6b — Auto-submit + email  
**Week 8–13:** Phase 7 — Platform connectors (priority order)  
**Week 14–16:** Phase 8–10 — Scheduling, recovery, audit, export  

**Est. Total:** ~18 weeks to MVP with all platforms

---

## Definitions & Terminology

**Connector:** A platform-specific class that knows how to search, read jobs, and apply on a specific platform (LinkedIn, Greenhouse, etc.)

**Search Profile:** A reusable set of filters (titles, job type, seniority, region, salary range, keywords, exclusions)

**Platform Account:** A user's authenticated session on a single job platform

**Automation Mode:** The degree to which the agent acts without manual review (search-only, fill-only, fill+submit, etc.)

**Dedup Hash:** Normalized hash of (company + title + location) to recognize the same job posting from multiple sources

**Fit Score:** LLM-computed relevance of a job to the user's resume/profile

**Clean Submissions:** The count of applications submitted successfully (no user corrections needed) on a given platform

**Review Queue:** List of applications awaiting manual approval before submission

**Audit Log:** Complete timestamped record of every action taken by the agent

---

## Repository Structure

```
JOB-AGENT/
├── README.md               # Main overview
├── PHASES.md               # Build roadmap (this file)
├── progress.md             # Phase tracking (populated weekly)
├── requirements.txt        # Python dependencies
├── .gitignore              # (exclude venv, .env, *.db, profiles/, etc.)
├── job_agent/
│   ├── __init__.py
│   ├── main.py             # CLI entry point
│   ├── connectors/
│   │   ├── __init__.py
│   │   ├── base.py         # ConnectedPlatformConnector base
│   │   ├── generic_ats.py  # Generic career site fallback
│   │   └── [platform].py   # LinkedIn, Indeed, Greenhouse, etc.
│   ├── core/
│   │   ├── __init__.py
│   │   ├── session_manager.py
│   │   ├── orchestrator.py
│   │   ├── document_gen.py
│   │   └── fit_scorer.py
│   ├── models/
│   │   ├── __init__.py
│   │   └── database.py     # SQLModel schema
│   ├── dashboard/
│   │   ├── __init__.py
│   │   ├── main.py         # FastAPI app
│   │   ├── routes/
│   │   │   ├── accounts.py
│   │   │   ├── search.py
│   │   │   ├── review_queue.py
│   │   │   └── history.py
│   │   └── static/
│   │       └── [frontend files]
│   ├── utils/
│   │   ├── __init__.py
│   │   ├── keychain.py     # macOS Keychain wrapper
│   │   ├── browser.py      # Playwright helpers
│   │   └── dedup.py        # Job deduplication
│   └── scripts/
│       ├── init_db.py      # Database initialization
│       └── test_connector.py
└── tests/
    └── [unit & integration tests]
```

---

## Contact & Support

See README.md for quick start and architecture overview.

For changes to the build plan, update this file and commit.
