# Build Phases & Roadmap

## Phase 0: Environment & Infrastructure ✓

**Deliverables:**
- [ ] Python 3.11+ venv configured
- [ ] Playwright installed (+ browsers: chromium, firefox)
- [ ] SQLite schema migrations (SQLModel models)
- [ ] Keychain access module via `keyring`
- [ ] launchd plist scaffold (disabled by default)
- [ ] Project structure initialized

**Acceptance:**
- `python --version` → 3.11+
- `playwright install` succeeds
- `sqlite3` & SQLModel models load without error
- Keychain test write/read succeeds
- launchd plist exists but is not loaded

---

## Phase 1: Session Manager & Account Connection ✅ COMPLETE

**Deliverables:**
- [x] `SessionManager` class for launching persistent Playwright contexts
- [x] Per-platform profile directory logic (`~/Library/Application Support/<app>/profiles/<platform>/`)
- [x] Connection status detection (logged-in indicators per platform)
- [x] Keychain metadata storage (platform name, timestamp, profile path — NOT credentials)
- [x] Connect/Disconnect UI endpoints & workflows
- [x] Test connector (local stub HTML page simulating a real job site)
- [x] Browser helper utilities (wait patterns, screenshots, element detection)
- [x] Integration tests for entire connection flow
- [x] Test server (stub job site at http://localhost:8001)

**Acceptance:**
- [x] User clicks "Connect <Platform>" → visible browser window opens, user signs in manually
- [x] On successful login, agent stores platform + timestamp + profile dir in Keychain + SQLite
- [x] `check_session()` detects authenticated state without reading credential fields
- [x] User can disconnect, which deletes profile dir + Keychain entry completely
- [x] All four steps (1–4 in §3) work end-to-end
- [x] SessionManager singleton pattern (get_session_manager, close_session_manager)
- [x] Full error handling and logging throughout
- [x] 100% type hints and docstrings

**Status:** ✅ Complete — See [PHASE1_COMPLETE.md](PHASE1_COMPLETE.md) for full details

**Files Created/Modified:**
- `job_agent/core/session_manager.py` (500+ lines) — SessionManager class with Playwright lifecycle
- `job_agent/utils/browser.py` (200+ lines) — Browser helper utilities
- `job_agent/connectors/test_connector.py` (250+ lines) — Test connector implementation
- `job_agent/dashboard/routes/accounts.py` (300+ lines) — Account management API endpoints
- `job_agent/dashboard/main.py` (UPDATED) — Integrated SessionManager lifespan events
- `job_agent/dashboard/routes/__init__.py` (NEW) — Routes package
- `scripts/test_server.py` (250+ lines) — Stub job site for testing
- `tests/test_phase1_integration.py` (200+ lines) — Integration tests

---

## Phase 2: Connector Framework ✅ COMPLETE

**Deliverables:**
- [x] `ConnectedPlatformConnector` base class (Phase 0)
- [x] `PlatformCapabilities` dataclass (Phase 0)
- [x] Generic ATS/company-career-site connector (works on public job JSON-LD schema + common career-site patterns)
- [x] Connector Registry for easy registration and lookup

**Acceptance:**
- [x] Base class has all 8 methods in spec (§5)
- [x] Capabilities struct declares what this platform can actually do
- [x] Generic ATS connector can:
  - [x] Read job title, company, location, description, requirements, salary from a typical career site
  - [x] Identify if the apply method is web form or email
  - [x] Reach an "apply" button and open the form (no fill yet)

**Status:** ✅ Complete — See [PHASE2_COMPLETE.md](PHASE2_COMPLETE.md) for full details

**Files Created/Modified:**
- `job_agent/connectors/generic_ats.py` (450+ lines) — Generic ATS connector with JSON-LD and HTML parsing
- `job_agent/connectors/registry.py` (300+ lines) — Connector registry with registration and lookup
- `job_agent/connectors/__init__.py` (UPDATED) — Export registry functions
- `tests/test_phase2_connectors.py` (300+ lines) — Integration tests for Phase 2

---

## Phase 3: Job Search & Pipeline ✅ COMPLETE

**Deliverables:**
- [x] Search profile model (titles, alt_titles, job_type, seniority, country, region, remote_pref, keywords, exclusions, salary, date_posted_within) — Phase 0 model, wired to API in Phase 3
- [x] Per-platform search API wiring (create profile, apply filters, run search)
- [x] Link collection from search results (respects daily_search_limit, shared across runs in a day)
- [x] Job detail reading (title, company, location, type, description, requirements, salary, posted_at, apply_method, recruiter_contact)
- [x] Dedup logic (dedup_hash primary pass + rapidfuzz near-duplicate pass on company + title + location)
- [x] Hard filter pass/fail (exclusions, salary, location, date posted, job type)
- [x] Fit scoring placeholder (heuristic; LLM swaps in at Phase 4 behind the same interface)
- [x] SQLite storage of all discovered jobs, including hard-filter failures
- [x] Generic connector search navigation (templated URL, plain URL, or on-page form) — closes the Phase 2 no-op
- [x] Dependency set re-pinned and verified on Python 3.14

**Acceptance:**
- [x] Run a search against a test platform, collect 10 jobs, store to SQLite
- [x] Duplicate detection: the same posting from two search results is deduplicated (exact + fuzzy)
- [x] Hard filters work: with salary min=$100k, an $80k job is stored with hard_filter_pass=False
- [x] Dashboard shows: jobs_found, new_jobs, duplicates_skipped (per run and per day)
- [x] Posting dates in any format ("2 days ago", ISO, "August 12, 2026") normalize to datetime
- [x] A read failure on one posting doesn't abort the run
- [x] Every search run and dedup event lands in the audit log

**Status:** ✅ Complete — See [PHASE3_COMPLETE.md](PHASE3_COMPLETE.md) for full details

**Files Created/Modified:**
- `job_agent/core/search_pipeline.py` (REWRITTEN) — Pipeline with daily limits, two-pass dedup, filter/score/store
- `job_agent/services/job_deduplicator.py` (UPDATED) — Per-field fuzzy matching + `find_duplicate_job()`
- `job_agent/services/filter_evaluator.py` — Hard filter evaluation
- `job_agent/services/fit_scorer.py` (UPDATED) — Fixed weighted-average normalization
- `job_agent/services/__init__.py` — Services package exports
- `job_agent/utils/dates.py` (NEW) — `parse_posted_at()` for free-text posting dates
- `job_agent/dashboard/deps.py` (NEW) — Shared engine + session dependency
- `job_agent/dashboard/routes/search.py` (UPDATED) — Search + jobs API routers
- `job_agent/dashboard/main.py` (UPDATED) — Registered search/jobs routers, phase 3 status
- `job_agent/dashboard/routes/accounts.py` (UPDATED) — Uses shared session dependency
- `job_agent/connectors/base.py` (UPDATED) — `set_page()` / `page` on the base class
- `job_agent/connectors/test_connector.py` (UPDATED) — Accepts `platform_name` (registry-constructible)
- `job_agent/connectors/generic_ats.py` (UPDATED) — Real search navigation + filters; fixed JSON-LD, salary, and selector-timeout bugs
- `job_agent/models/database.py` (UPDATED) — `platform_accounts.search_url`
- `job_agent/config.py` (FIXED) — Optional-typed settings (Pydantic v2 rejected `str = None`), `SettingsConfigDict`
- `scripts/init_db.py` (UPDATED) — Idempotent additive-column migration step
- `scripts/setup.py` (UPDATED) — Standardized on `.venv/`
- `requirements.txt` (REWRITTEN) — Pins verified on Python 3.14
- `requirements-dev.txt` (NEW) — Test and lint tooling
- `pytest.ini` (NEW) — asyncio mode, warning filters
- `tests/test_phase3_search.py` (NEW, 39 tests) — Dedup, filters, scoring, dates, pipeline
- `tests/test_phase3_generic_search.py` (NEW, 43 tests) — Search strategies, JSON-LD shapes, live-browser pipeline

---

## Phase 4: Document Generation ✅ COMPLETE

**Deliverables:**
- [x] Resume templating (DOCX/PDF/TXT/MD master → parsed text + sections → versioning logic)
- [x] Cover letter templating (same pipeline, independent active master)
- [x] PDF export via WeasyPrint (HTML/CSS) with automatic ReportLab fallback
- [x] Tailoring wiring (job description → Ollama / Claude / deterministic → tailored variant)
- [x] Version storage (`document_versions` + `applications.resume_version_id` / `cover_letter_version_id`)
- [x] Fabrication verification — generated content is checked against the master, not trusted

**Acceptance:**
- [x] User uploads a master resume (DOCX or PDF); it's parsed and stored
- [x] Given a job posting, agent generates a tailored resume PDF variant
- [x] PDF is linked in the application record; user can download / review before submit
- [x] Tailored documents never assert anything the master doesn't support
- [x] Generation works with no LLM installed (deterministic path)

**Status:** ✅ Complete — See [PHASE4_COMPLETE.md](PHASE4_COMPLETE.md) for full details

**Files Created/Modified:**
- `job_agent/services/document_parser.py` (NEW) — DOCX/PDF/TXT parsing + section detection
- `job_agent/services/pdf_renderer.py` (NEW) — WeasyPrint + ReportLab engines, auto-selected
- `job_agent/services/document_layout.py` (NEW) — Shared block structure both engines render
- `job_agent/services/fabrication_check.py` (NEW) — Verifies variants against their master
- `job_agent/services/tailoring.py` (NEW) — Ollama / Claude / deterministic generators
- `job_agent/services/document_builder.py` (NEW) — Upload → tailor → verify → render → store
- `job_agent/dashboard/routes/documents.py` (NEW) — Document API (8 endpoints)
- `job_agent/models/database.py` (UPDATED) — `MasterDocument`, `DocumentVersion`, doc enums
- `job_agent/config.py` (UPDATED) — `documents_dir`, `anthropic_model`, upload size limit
- `scripts/init_db.py` (UPDATED) — Migration for the new application columns
- `tests/test_phase4_documents.py` (NEW, 66 tests) — Parsing, both render engines, verification, tailoring, builder
- `tests/test_phase4_documents_api.py` (NEW, 18 tests) — Full HTTP path
- `tests/test_phase4_ollama_live.py` (NEW, 5 tests) — Opt-in live-model checks, auto-skipped
- `tests/conftest.py` (NEW) — Forces the deterministic path so the suite stays hermetic

---

## Phase 5: Application Filling & Review Gate ✅ COMPLETE

**Deliverables:**
- [x] `ApplicationSession` tracking form state (filled, deferred, screenshot, errors)
- [x] `fill_application()` reads form fields and fills known ones (name, email, phone, links, resume, cover letter)
- [x] Unknown field detection → deferred with a reason, application pauses
- [x] Sensitive field detection (compensation, demographics, disability, veteran status) → never auto-answered
- [x] Screenshot capture of the filled form
- [x] Review Queue API (side-by-side: filled form + source job posting + documents)
- [x] Approve / answer / discard actions
- [x] NO auto-submit — `submit_application()` refuses by design
- [x] `CandidateProfile` model + remembered answers for repeat questions

**Acceptance:**
- [x] Open a job application form → agent fills known fields (name, email, phone, resume attachment)
- [x] Encounter an unknown field → application pauses, appears in the Review Queue
- [x] Screenshot shows the filled form; `filled_at` / `reviewed_at` recorded
- [x] User answers, approves or discards; form data logged awaiting manual submission
- [x] Sensitive questions are left blank on the page and never remembered

**Status:** ✅ Complete — See [PHASE5_COMPLETE.md](PHASE5_COMPLETE.md) for full details

**Files Created/Modified:**
- `job_agent/services/field_classifier.py` (NEW) — KNOWN/REMEMBERED/SENSITIVE/UNKNOWN decisions
- `job_agent/services/form_reader.py` (NEW) — Single-pass field extraction with human-readable labels
- `job_agent/services/application_filler.py` (NEW) — Fill, defer, screenshot, queue for review
- `job_agent/dashboard/routes/review.py` (NEW) — Review queue + candidate profile API
- `job_agent/connectors/generic_ats.py` (UPDATED) — Real `begin_application()` / `fill_application()`
- `job_agent/models/database.py` (UPDATED) — `CandidateProfile`, `FieldCategory`, form-context columns
- `scripts/test_server.py` (UPDATED) — Realistic application form covering all four field categories
- `scripts/init_db.py` (UPDATED) — Migration for the Phase 5 application columns
- `tests/test_phase5_filling.py` (NEW, 54 tests) — Classification, reading, live-browser filling
- `tests/test_phase5_review_api.py` (NEW, 28 tests) — Review queue over HTTP

---

## Phase 6: Auto-Submit Path & Clean Submissions Gate ✅ COMPLETE

**Deliverables:**
- [x] `clean_submissions_count` tracking per platform account
- [x] First-N-submissions mandatory review gate (default N=3, `settings.clean_submissions_threshold`)
- [x] Auto-submit eligibility logic (mode + capability + threshold + daily limit + document verification)
- [x] Actual form submission via Playwright (click submit, detect confirmation)
- [x] Confirmation reference capture (reference number, URL, message, before/after screenshots)
- [x] Application status transition (queued → submitted)
- [x] Daily apply limit enforcement
- [x] Separate bars for user-directed vs unattended submission

**Acceptance:**
- [x] Submit 3 applications through the review queue → `clean_submissions_count` = 3
- [x] 4th application auto-submits under `search_fill_submit`; queues under a fill-only mode
- [x] Confirmation captured and linked to the application record
- [x] Daily counter stops at the configured limit
- [x] An auto-submitted application never advances the clean count (the gate can't certify itself)
- [x] A document with unsupported claims can never be submitted

**Status:** ✅ Complete — See [PHASE6_COMPLETE.md](PHASE6_COMPLETE.md) for full details

**Files Created/Modified:**
- `job_agent/services/submission_gate.py` (NEW) — Both eligibility paths, with explained refusals
- `job_agent/services/submitter.py` (NEW) — Click, wait, verify; confirmation over optimism
- `job_agent/services/submission_recorder.py` (NEW) — Status, audit, and clean-count rules
- `job_agent/connectors/generic_ats.py` (UPDATED) — Real `submit_application()`
- `job_agent/dashboard/routes/review.py` (UPDATED) — `/eligibility` and `/submit` endpoints
- `tests/test_phase6_submission.py` (NEW, 35 tests) — Gate, recorder, real-browser submission
- `tests/test_phase6_submit_api.py` (NEW, 9 tests) — HTTP-boundary refusals

---

## Phase 6b: Email-Based Applications ✅ COMPLETE

**Deliverables:**
- [x] Detect the application address in a posting, with scored reasoning the user can check
- [x] Draft composer: subject, body from the verified tailored cover letter, PDF attachments
- [x] Mail.app automation via AppleScript (primary; no credentials involved)
- [x] SMTP fallback using an app-specific password from the Keychain, never the account password
- [x] Draft-to-review workflow: email always stops for review, whatever the automation mode
- [x] Email message-id & recipient logging
- [x] Reply monitoring: IMAP poll, linked back to the application and surfaced in the dashboard
- [x] Rate limiting: configurable email cap per hour, enforced at send time
- [x] Editing a draft revokes a prior approval

**Acceptance:**
- [x] "Email resume to: hiring@company.com" is detected (and no-reply/privacy inboxes rejected)
- [x] Draft composes with subject, cover-letter body, and resume PDF attached
- [x] Draft appears for review — there is no path from DRAFT to SENT
- [x] User approves → email sent; message-id + sent_at logged
- [x] Replies are polled and linked to the application

**Status:** ✅ Complete — See [PHASE6B_COMPLETE.md](PHASE6B_COMPLETE.md) for full details

**Files Created/Modified:**
- `job_agent/services/email_detector.py` (NEW) — Scored recipient detection with explanations
- `job_agent/services/email_composer.py` (NEW) — Subject/body/attachments from verified material
- `job_agent/services/email_sender.py` (NEW) — Mail.app + SMTP, app-specific passwords only
- `job_agent/services/email_service.py` (NEW) — Draft lifecycle, rate limiting, reply polling
- `job_agent/dashboard/routes/email.py` (NEW) — Email API (10 endpoints)
- `job_agent/models/database.py` (UPDATED) — `EmailDraft`, `EmailDraftStatus`, audit actions
- `job_agent/config.py` (UPDATED) — SMTP/IMAP host settings (never passwords)
- `tests/test_phase6b_email.py` (NEW, 50 tests) — Detection, composition, lifecycle, limits, sender
- `tests/test_phase6b_email_api.py` (NEW, 17 tests) — Full HTTP path

---

## Phase 7: Additional Connectors ✅ COMPLETE

Built with honest capability declarations. 13 new connectors (15 registered total).

| Sub | Platform | Search | Read | Fill | Submit | Status |
|-----|----------|:------:|:----:|:----:|:------:|--------|
| 7a | Greenhouse | ✅ | ✅ | ✅ | ✅ | ✅ Complete |
| 7b | Lever | ✅ | ✅ | ✅ | ✅ | ✅ Complete |
| 7c | Ashby | ✅ | ✅ | ✅ | ❌ | ✅ Complete — custom-question heavy, user submits |
| 7d | Workday | ✅ | ✅ | ❌ | ❌ | ✅ Complete — per-tenant wizard, opens application only |
| 7e | SmartRecruiters / Workable | ✅ | ✅ | ✅ | ✅ | ✅ Complete |
| 7f | LinkedIn | ✅ | ✅ | ❌ | ❌ | ✅ Complete — read-only, ToS risk note |
| 7g | Indeed | ✅ | ✅ | ❌ | ❌ | ✅ Complete — read-only, ToS risk note |
| 7h–j | Glassdoor, ZipRecruiter, Wellfound, Dice, JobStreet | ✅ | ✅ | ❌ | ❌ | ✅ Complete — read-only, risk notes |

**Verification caveat:** every connector is tested against local fixtures replicating each
platform's published markup — none has been run against a live tenant, since that would mean
submitting real applications. Recorded in code as `VerificationLevel.FIXTURE` and exposed via
the API as `verified_against`. The Phase 6 gate covers the gap by keeping the first three
applications on any platform under human review.

**Status:** ✅ Complete — See [PHASE7_COMPLETE.md](PHASE7_COMPLETE.md) for full details

**Files Created/Modified:**
- `job_agent/connectors/hosted_ats.py` (NEW) — Shared base + `VerificationLevel`
- `job_agent/connectors/ats_connectors.py` (NEW) — Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Workable
- `job_agent/connectors/job_boards.py` (NEW) — LinkedIn, Indeed, Glassdoor, ZipRecruiter, Wellfound, Dice, JobStreet
- `job_agent/connectors/registry.py` (UPDATED) — Registers all 13
- `job_agent/dashboard/routes/connectors.py` (NEW) — Capability catalogue API
- `tests/test_phase7_connectors.py` (NEW, 156 tests) — Capability honesty, URLs, fixtures
- `tests/test_phase7_connectors_api.py` (NEW, 9 tests) — Catalogue endpoint

---

## Phase 8: Scheduling & Automation Loop ✅ COMPLETE

**Deliverables:**
- [x] Orchestrator core loop: load search profile → iterate platforms → run pipeline → apply limits → write audit log
- [x] launchd job enable/disable + scheduling configuration (install and enable are separate)
- [x] Daily limit tracking per platform, reset at midnight UTC
- [x] Run summary generation (jobs found, new, duplicates, filtered, documents, interruptions, errors)
- [x] Per-platform isolation — one platform's failure doesn't end the run
- [x] **Pagination** (closes a Phase 7 limitation) — up to 5 pages via page param, next control, or load-more
- [x] **CAPTCHA/MFA/rate-limit detection** (pulled forward from Phase 9) — pauses the platform, never solves

**Acceptance:**
- [x] Schedule a daily job search at 9 AM and 6 PM
- [x] Each run respects daily_search_limit per platform
- [x] Run summary shows: 42 jobs found, 3 new, 2 duplicates, platforms skipped with reasons
- [x] A run never submits applications — `applications_submitted` is structurally 0

**Status:** ✅ Complete — See [PHASE8_COMPLETE.md](PHASE8_COMPLETE.md) for full details

**Files Created/Modified:**
- `job_agent/core/orchestrator.py` (NEW) — The run loop, per-platform isolation, run summaries
- `job_agent/core/scheduler.py` (NEW) — launchd plist management; install ≠ enable
- `job_agent/services/interruption_detector.py` (NEW) — CAPTCHA/MFA/sign-in/rate-limit detection
- `job_agent/connectors/hosted_ats.py` (UPDATED) — Multi-page link collection
- `job_agent/connectors/job_boards.py`, `ats_connectors.py` (UPDATED) — Per-platform page params
- `job_agent/dashboard/routes/runs.py` (NEW) — Run + schedule API
- `job_agent/__main__.py` (NEW) — `python -m job_agent`, which the launchd job invokes
- `job_agent/main.py` (UPDATED) — Real `run` command + `schedule` command group
- `job_agent/models/database.py` (UPDATED) — `AgentRun`, `RunStatus`, Phase 8 audit actions
- `tests/test_phase8_orchestrator.py` (NEW, 46 tests) — Loop, skips, interruptions, scheduler

---

## Phase 9: Session Monitoring & Recovery ✅ COMPLETE

**Deliverables:**
- [x] Session expiry detection (connector check + interruption detection)
- [x] Pause platform-specific tasks, continue others
- [x] Dashboard notification with a Reconnect signal (`GET /api/v1/health` → `needs_reconnect`)
- [x] Reconnect workflow: opens the browser at login; the agent never enters credentials
- [x] Resume: verifies the session, closes interruptions, reports pending work
- [x] CAPTCHA/MFA mid-run handling: pause, notify, wait for the user or a session check
- [x] `PlatformInterruption` persisted as a task that outlives its run
- [x] **`queue_applications` implemented** (closes a Phase 8 limitation)
- [x] **launchd verified end-to-end** (closes a Phase 8 limitation)

**Acceptance:**
- [x] Mid-run, a platform's session expires → that platform pauses, others continue
- [x] User reconnects → browser opens to login → platform shows Connected
- [x] Tasks queued for that platform resume
- [x] An interruption cannot be closed while the platform is still blocked (unless overridden)

**Status:** ✅ Complete — See [PHASE9_COMPLETE.md](PHASE9_COMPLETE.md) for full details

**Files Created/Modified:**
- `job_agent/services/session_monitor.py` (NEW) — Health, interruptions, reconnect, resume
- `job_agent/dashboard/routes/health.py` (NEW) — Health & recovery API (7 endpoints)
- `job_agent/models/database.py` (UPDATED) — `PlatformInterruption`, Phase 9 audit actions
- `job_agent/core/orchestrator.py` (UPDATED) — Real `queue_applications`; skips interrupted platforms
- `job_agent/main.py`, `routes/runs.py` (UPDATED) — `--queue` / `queue_applications`
- `tests/test_phase9_recovery.py` (NEW, 29 tests) — Interruptions, health, resolution, recovery
- `tests/test_phase9_health_api.py` (NEW, 17 tests) — HTTP boundary
- `tests/test_phase8_orchestrator.py` (UPDATED) — Real launchctl lifecycle + plutil validation

---

## Phase 10: Audit Log & Export ✅ COMPLETE

**Deliverables:**
- [x] Complete audit_log coverage (every search, read, fill, submit, error, interruption)
- [x] CSV export: applications, jobs, audit log, runs, emails
- [x] Audit view filterable by platform, action type, result and date
- [x] Settings: per-platform daily limits and automation mode (editable); review threshold,
      fit-score threshold and email rate limit (read-only, from the environment)
- [x] Stable export ordering so repeated exports are byte-identical
- [x] **WeasyPrint isolated in a subprocess** (closes the Phase 9 native-crash issue)

**Acceptance:**
- [x] Export applications as CSV with job_id, company, title, submitted_at, status, confirmed_ref
- [x] Audit log shows every action: "2026-08-14 20:12:40 greenhouse search_run collected=42 new=3"
- [x] CSV survives a round trip, including descriptions with commas and newlines

**Status:** ✅ Complete — See [PHASE10_COMPLETE.md](PHASE10_COMPLETE.md) for full details

**Files Created/Modified:**
- `job_agent/services/exporter.py` (NEW) — Five CSV exports with stable ordering
- `job_agent/dashboard/routes/audit.py` (NEW) — Audit, export and settings API
- `job_agent/services/_pdf_worker.py` (NEW) — Isolated WeasyPrint renderer
- `job_agent/services/pdf_renderer.py` (UPDATED) — Subprocess isolation with fallback
- `job_agent/config.py` (UPDATED) — `pdf_isolate_weasyprint`
- `tests/test_phase10_audit_export.py` (NEW, 52 tests) — Exports, audit API, settings
- `tests/conftest.py` (UPDATED) — In-process rendering keeps the suite fast

---

## Summary Table

| Phase | Component | Weeks | Status |
|-------|-----------|-------|--------|
| 0 | Env + DB | 1 week | ✅ Complete |
| 1 | Session mgr | 1 week | ✅ Complete |
| 2 | Framework | 3–5 days | ✅ Complete |
| 3 | Job search | 1 week | ✅ Complete |
| 4 | Documents | 1 week | ✅ Complete |
| 5 | Fill + review | 1.5 weeks | ✅ Complete |
| 6 | Auto-submit | 3–5 days | ✅ Complete |
| 6b | Email apps | 1 week | ✅ Complete |
| 7 | Connectors | 4–6 weeks | ✅ Complete |
| 8 | Scheduling | 1 week | ✅ Complete |
| 9 | Recovery | 1 week | ✅ Complete |
| 10 | Logging + export | 1 week | ✅ Complete |
| | **TOTAL** | **~18 weeks** | **✅ All phases complete — 722 tests, 55 endpoints, 15 connectors** |

Each phase is testable independently before moving to the next.
