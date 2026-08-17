# Phase 9 Complete: Session Monitoring & Recovery

**Status:** ✅ Complete
**Date:** 2026-08-15
**Tests:** 46 new Phase 9 tests; 665 passing in the hermetic suite

Also closes the two Phase 8 limitations: **`queue_applications`** and the **untested launchd
path**.

---

## What Phase 9 Delivers

Phase 8 built the detection half — a run notices a wall and pauses that platform. This is
the other half: noticing before a run, telling the user what needs doing, and getting the
platform working again.

```
run hits a CAPTCHA ──▶ PlatformInterruption recorded, platform paused
                              │
        GET /api/v1/health ───┤  "indeed needs_reconnect: Complete the challenge yourself"
                              │
  POST .../reconnect ─────────┤  browser opens at the login page; the user signs in
                              │
  POST .../resume ────────────┘  session verified → interruptions closed → work resumes
```

---

## Three Ideas

**An interruption is a task, not a log line.** `PlatformInterruption` outlives the run
that hit it, and the platform stays paused until that row is resolved. A CAPTCHA that
scrolled past in a log is a CAPTCHA nobody handles. Duplicates aren't stacked either —
three CAPTCHAs on one platform is one thing for the user to do.

**Resolution needs evidence.** `resolve()` checks the session before closing an
interruption, because a user who says "I've done it" but hasn't would send the next run
straight back into the same wall:

```
POST /api/v1/interruptions/3/resolve
→ 409  "indeed still looks blocked (captcha still showing). Resolve it in the
        browser first, or resolve without verification if you're sure."
```

`verify=false` overrides it, and the record says which happened
(`"Marked resolved without verification"`). The agent never decides on its own that a
challenge passed — retrying into a live challenge is what escalates it into a blocked
account.

**Health is per platform.** A blocked Indeed leaves Greenhouse running. The run loop now
consults open interruptions directly, so a platform whose *status* says `CONNECTED` but
which has an unresolved CAPTCHA is still skipped — proven by
`test_open_interruption_skips_the_platform`.

---

## Reconnect Never Touches Credentials

`reconnect()` opens a browser window at the platform's login page and stops. The user
signs in themselves, exactly as in Phase 1. The response says so:

> Sign in to indeed in the open browser window, then confirm — the agent never enters
> credentials for you.

`resume()` then verifies the session, closes the interruptions that were blocking it, and
reports the work waiting (`pending_jobs` — jobs found on that platform with no application
yet).

### API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/v1/health` | Every platform's state; `needs_reconnect` drives the button |
| POST | `/api/v1/health/check` | Re-check sessions now |
| GET | `/api/v1/interruptions` | What needs the user, with guidance |
| GET | `/api/v1/interruptions/{id}/screenshot` | What the agent saw |
| POST | `/api/v1/interruptions/{id}/resolve` | "I've handled it" (verified by default) |
| POST | `/api/v1/platforms/{p}/reconnect` | Open a browser to sign in |
| POST | `/api/v1/platforms/{p}/resume` | Bring the platform back into service |

---

## Closed: `queue_applications` (Phase 8 limitation)

Previously accepted but recorded intent. Now implemented: for each eligible job the run
opens the application, fills it with the Phase 5 classifier — which leaves demographic and
compensation questions blank — screenshots it, and queues it for review.

Guards that matter for something running unattended:

- **Never submits.** `applications_submitted` stays 0; queueing ends at the review queue.
- **Implies `generate_documents`.** Queueing an application with no tailored resume would
  attach nothing, so requesting one enables the other rather than producing empty
  applications.
- **Respects `daily_apply_limit`**, counting everything created today — not just
  submissions. Forty drafts waiting for review is its own kind of overreach.
- **Stops on a wall.** A CAPTCHA partway through breaks the loop and pauses the platform;
  repeatedly opening application forms into a challenge is how a session gets flagged.
- **Skips platforms that can't fill.** LinkedIn and Workday prepare documents but queue
  nothing, because their connectors don't claim `can_fill_standard_fields`.

```bash
python -m job_agent run --profile "Backend - Remote" --queue
```

## Closed: launchd Verified End-to-End (Phase 8 limitation)

The concern was that loading a schedule would start running the agent on this Mac. Both
halves are now tested without that ever happening:

- **`plutil -lint` on the real generated plist** — proves launchd can parse what we write.
- **A real `launchctl load` / `list` / `unload` cycle** against a plist whose program is
  `/usr/bin/true` with `RunAtLoad: false` — proves `enable()`, `is_loaded()` and
  `disable()` work against the real system without executing anything.
- **The scheduled command is invoked for real** — `python -m job_agent run --help` in a
  subprocess. Asserting `__main__.py` exists wasn't enough; an import error inside the CLI
  would still have failed at 9 AM.

Verified afterwards: `launchctl list | grep jobagent` returns nothing — the tests leave
nothing loaded.

---

## Acceptance Criteria

| Criterion | Status | Evidence |
|-----------|--------|----------|
| Session expiry detection | ✅ | `SessionMonitor.check_session`, `monitor()` |
| Mid-run, one platform pauses and others continue | ✅ | `test_other_platforms_still_run` |
| Dashboard notification with a Reconnect button | ✅ | `GET /api/v1/health` → `needs_reconnect` |
| Reconnect opens the browser at login, then Connected | ✅ | `test_resume_restores_the_platform` |
| Queued work for that platform resumes | ✅ | `test_resume_clears_interruptions_and_reports_work` |
| CAPTCHA/MFA: pause, notify, wait for "I've completed it" or a session check | ✅ | `TestResolution` (6 tests) |

---

## Test Coverage

### `tests/test_phase9_recovery.py` — 29 tests

Recording and de-duplicating interruptions (6), health reporting including pending work
(7), resolution with and without verification (6), reconnect and resume (7), and the run
loop refusing to run an interrupted platform (3).

### `tests/test_phase9_health_api.py` — 17 tests

The health endpoint, interruption listing and resolution over HTTP, reconnect, resume,
and every 404/409 case.

---

## Known Issues

~~**An intermittent native crash in WeasyPrint's stack.**~~ **Resolved in Phase 10** —
WeasyPrint now renders in a subprocess, so a native fault becomes a retryable error and
the run continues on ReportLab. Original report: One full-suite run out of roughly
six aborted with a fatal error inside `libffi` (`ffi_call_int`), with `_cffi_backend`,
PIL and fontTools loaded — WeasyPrint's rendering dependencies. It did not reproduce in
three consecutive runs afterwards (665 passing each), and no test failed; the process
died.

This matters more than a flaky test would, because PDF rendering happens inside
unattended scheduled runs, where a crash kills the run silently. The mitigation is a
one-setting change that avoids the native stack entirely:

```bash
PDF_ENGINE=reportlab
```

Verified: with that set, the engine selector returns `reportlab` and all 84 document tests
pass. ReportLab is pure Python, so nothing in that path can segfault. Recommended for
scheduled runs until the crash is understood; WeasyPrint remains the better-looking
default for interactive use.

## Known Limitations

- **Reconnect assumes the browser is visible.** It opens a window and returns; there is no
  polling loop that notices the user finishing. They call `resume` when done.
- **`check_session()` depends on connector honesty.** Most connectors return "connected"
  unconditionally (the generic one does), so expiry detection leans on the interruption
  detector rather than per-platform logged-in indicators. A connector that silently
  returns stale pages would look healthy.
- **Resume reports pending work but doesn't start it.** It says "3 jobs are waiting; start
  a run to prepare them" rather than launching one, because a resume triggered from a
  dashboard click shouldn't silently start browser automation.
- **Interruption text matching is English-only** (carried over from Phase 8).
- **No notification transport.** "Dashboard notification" is an API endpoint; there is no
  push, email or macOS notification when a run pauses at 3 AM.

---

## Next: Phase 10 — Audit Log & Export

Complete audit coverage, CSV export for jobs / applications / audit log, a filterable
audit view, and the settings page for review thresholds, daily limits and the email rate
limit. Most of the audit writing already exists across phases 1–9; Phase 10 is largely
surfacing and exporting it.
