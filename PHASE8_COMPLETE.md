# Phase 8 Complete: Scheduling & Automation Loop

**Status:** ✅ Complete
**Date:** 2026-08-15
**Tests:** 46 new Phase 8 tests; 615 passing in the hermetic suite

Also closes two Phase 7 limitations: **pagination** and **CAPTCHA/MFA handling**.

---

## What Phase 8 Delivers

The loop that ties every prior phase together, and the launchd schedule that runs it.

```
for each connected platform:
    skip if paused, disconnected, or out of daily budget
    search    (Phase 3)  → jobs stored, deduped, filtered, scored
    generate  (Phase 4)  → tailored documents for the best matches
    check for a wall     → CAPTCHA / MFA / expired session pauses that platform
write the run summary
```

Verified end-to-end:

```
Run #1 completed, 42 jobs found, 3 new, 2 duplicates, 6 filtered out — skipped ashby, indeed
  ran    : ['greenhouse', 'lever']
  skipped: ashby  — daily search limit reached (0/0)
  skipped: indeed — session expired — reconnect this platform
  submitted: 0 (runs never submit)
```

---

## A Run Never Submits

Phase 6 built submission behind a gate whose entire purpose is that the first
applications on a platform are seen by a human. A scheduled run happens while the user is
asleep — so this loop **prepares work and stops**. `applications_submitted` is
structurally 0, and a test asserts it.

Submission remains a separate, user-initiated act through the review queue. That is the
difference between an agent that finds and drafts overnight, and one that applies to
forty jobs in your name before you wake up.

---

## One Platform's Failure Is Not the Run's Failure

A CAPTCHA on Indeed pauses Indeed and leaves Greenhouse to finish. Each platform is
wrapped so an exception, an interruption, or an exhausted budget is contained to it, and
the run reports what happened per platform:

- `platforms_run` — what actually searched
- `platforms_skipped` — `{platform: reason}`, in plain language
- `interruptions` — CAPTCHAs and walls, with guidance
- `errors` — everything else

A run with any of these is `PARTIAL` rather than `COMPLETED`; a run where *every* platform
was skipped is `FAILED`, because "0 jobs found" and "nothing could run" are different
outcomes and shouldn't look alike in a summary.

---

## Closed: Pagination (Phase 7 limitation)

Boards previously yielded only their first page. `HostedATSConnector.collect_job_links()`
now walks up to `MAX_PAGES` (5), trying in order:

1. The platform's page query parameter — LinkedIn and Indeed page by result offset
   (`start`, from 0), ZipRecruiter and Dice by page number
2. A next-page control (`a[rel=next]`, `aria-label*=next`, …)
3. A "load more" button, which appends rather than navigating

It stops early when a page yields no new links, which also covers a pagination control
that silently does nothing. Verified against a paginated fixture: **12 postings collected
across 3 pages** at a page size of 5.

Five pages is a cap, not a limit to raise casually: an unbounded walk on a board with
thousands of postings would run for hours and read far more than any daily limit permits.
The Phase 3 budget still truncates whatever comes back.

---

## Closed: CAPTCHA / MFA Handling (Phase 9 pulled forward)

An agent that doesn't notice a CAPTCHA keeps clicking, keeps reloading, and keeps
tripping the same detection — which is what turns a challenge into a suspended account.
Since Phase 8 runs unattended, detection could not wait for Phase 9.

`InterruptionDetector` recognizes five kinds — `CAPTCHA`, `MFA`, `SIGNIN_REQUIRED`,
`RATE_LIMITED`, `BLOCKED` — from structural signals first (an embedded reCAPTCHA/hCaptcha
widget, a Cloudflare challenge form, a one-time-code input) then page wording. Structural
signals rank first because page *text* can mention "verify you're human" in a help
article.

One deliberate exception: a bare `input[type=password]` is **not** treated as a wall. Job
boards show a sign-in box beside their results; a password field only means a wall when
the page text agrees.

**The agent never attempts to solve a challenge.** CAPTCHAs exist to establish that a
human is present; defeating one would be both a terms violation and a lie told on the
user's behalf. The guidance says so explicitly:

> Complete the challenge yourself in the open browser window, then mark it resolved.
> The agent will never solve a CAPTCHA for you.

When a wall is hit, the platform is screenshotted, marked `SESSION_EXPIRED` with the
reason, and **skipped on subsequent runs** until the user resolves it. That last part is
what stops a retry loop into a challenge.

---

## Scheduling

`LaunchdScheduler` writes, loads and unloads the launchd agent. Installing and enabling
are **separate operations**:

```bash
python -m job_agent schedule install 09:00 18:00 --profile "Backend - Remote"
python -m job_agent schedule enable
python -m job_agent schedule status
```

Writing a plist should never be what starts an agent applying for jobs, so `install()`
does not load it and `status()` reports `installed` and `enabled` independently.

`RunAtLoad` is deliberately `False`: a missed run must not fire the moment the Mac wakes,
when the user is mid-something and a browser window opening unprompted is exactly the
surprise this project avoids elsewhere.

The job runs with `sys.executable`, so a venv install keeps working from launchd.

### A bug this caught

The generated command is `python -m job_agent run --scheduled` — and the package had no
`__main__.py`. Every scheduled run would have failed with *"No module named
job_agent.__main__"*, silently, in a log file, at 9 AM. Added, with a test asserting the
entry point exists.

---

## API and CLI

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/v1/runs` | Run now across connected platforms |
| GET | `/api/v1/runs` | Run history with summaries |
| GET | `/api/v1/runs/{id}` | One run in detail |
| GET | `/api/v1/schedule` | Installed and enabled state |
| POST | `/api/v1/schedule` | Install a schedule (does not enable) |
| POST | `/api/v1/schedule/enable` / `disable` | Start / stop running on schedule |
| DELETE | `/api/v1/schedule` | Remove the schedule |

```bash
python -m job_agent run --profile "Backend - Remote" --documents
```

---

## Acceptance Criteria

| Criterion | Status | Evidence |
|-----------|--------|----------|
| Orchestrator loop: profile → platforms → pipeline → limits → audit | ✅ | `test_run_aggregates_across_platforms` |
| launchd enable/disable + schedule configuration | ✅ | `TestLaunchdScheduler` (12 tests) |
| Schedule a daily search at 9 AM and 6 PM | ✅ | `test_install_writes_a_valid_plist` |
| Each run respects `daily_search_limit` per platform | ✅ | `test_daily_search_limit_skips_a_platform` |
| Run summary reports found / new / duplicates / queued | ✅ | `test_summary_reads_like_the_spec` |
| Pagination beyond the first page | ✅ | 12 postings across 3 pages, live browser |
| CAPTCHA/MFA pauses rather than retries | ✅ | `TestInterruptionsInARun` |

---

## Test Coverage

### `tests/test_phase8_orchestrator.py` — 46 tests

The run loop and aggregation (7), platform isolation (2), skip reasons including every
connection status and the daily budget (6), interruption detection across structural and
text signals (9), interruptions inside a run (2), schedule-time parsing (5), and the
launchd scheduler (12).

---

## Known Limitations

- ~~**`queue_applications` is accepted but not implemented.**~~ **Resolved in Phase 9** —
  runs now open, fill and queue applications for review, respecting `daily_apply_limit`,
  stopping on a wall, and skipping platforms that can't fill. Still never submits.
- **Document generation is opt-in per run** (`--documents`) and only covers jobs above
  `fit_score_threshold` that passed the hard filters. With no master resume uploaded it
  skips with a log line.
- **Interruption detection is pattern-based** and English-only. A challenge in another
  language, or a purely visual one, won't be recognized — the run would then look like a
  platform returning no jobs.
- ~~**The launchd path is untested end-to-end.**~~ **Resolved in Phase 9** — the real plist
  is validated with `plutil -lint`, the load/list/unload cycle is exercised against real
  launchctl using `/usr/bin/true` as the program, and the scheduled command itself is
  invoked in a subprocess. Nothing is left loaded.
- **No run cancellation.** A long run finishes or crashes; there's no stop button.
- **Schedules are per-machine.** A laptop asleep at 09:00 simply misses that run, by
  design (`RunAtLoad: False`).

---

## Next: Phase 9 — Session Monitoring & Recovery

The detection half already exists. Phase 9 adds the recovery half: a dashboard
notification with a Reconnect button, the reconnect workflow that reopens the browser at
the login page, and resuming tasks queued for a platform once its session is healthy
again.
