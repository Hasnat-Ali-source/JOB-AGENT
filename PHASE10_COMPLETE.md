# Phase 10 Complete: Audit Log & Export

**Status:** ✅ Complete — **all 10 phases done**
**Date:** 2026-08-15
**Tests:** 52 new Phase 10 tests; 722 passing in the hermetic suite

Also resolves the Phase 9 **WeasyPrint native crash**.

---

## What Phase 10 Delivers

Phases 1–9 wrote to the audit log at every step. Phase 10 surfaces it, exports it, and
adds the settings page.

```
2026-08-14 20:12:40  greenhouse  search_run             success  search_run collected=42 new=3
2026-08-14 19:12:40  greenhouse  application_submitted  success  application_submitted ...
2026-08-14 18:12:40  indeed      run_interrupted        paused   run_interrupted ...
```

`GET /api/v1/audit` filters by platform, action, result and date; `/audit/actions` lists
only the values actually present, so filters built from it always match something;
`/audit/summary` counts what needs attention (`paused` + `failure`).

---

## Export: Your Data Leaves As Easily As It Arrived

Five CSV exports — applications, jobs, audit, runs, emails. A local SQLite file is only
useful if it opens in a spreadsheet, goes to someone else, or outlives the tool.

```
application_id,job_id,platform,company,title,location,salary,...,status,...,confirmation_ref
1,1,greenhouse,Acme Robotics,Senior Backend Engineer,Remote,"USD 180,000 - 210,000",...,submitted,...,ACME-88421
```

Two details that make an export trustworthy rather than merely present:

- **CSV round-trips.** Job descriptions contain commas and newlines; a naive export turns
  one job into several spreadsheet rows. Values are flattened and quoted, and a test reads
  the output back with `csv.reader` and asserts the row count.
- **Order is stable.** Every export has an id tiebreaker, so two exports of unchanged data
  are byte-identical and can be diffed. Without it, rows with identical timestamps
  reordered between runs — found by a test that assumed row order and was right to fail.

The applications export also carries `unanswered_required` and `documents_verified`, so a
spreadsheet shows which applications went out incomplete or with unverified documents.

---

## Settings Split in Two

| Editable via API | Read-only, from `.env` |
|---|---|
| `daily_search_limit`, `daily_apply_limit`, `daily_message_limit` | `clean_submissions_threshold` |
| `automation_mode`, `search_url` | `fit_score_threshold`, `email_rate_limit` |

Per-platform limits are operational and belong in the UI. The safety thresholds are not:
**a running agent should not be able to lower its own review threshold through an API
call.** They're displayed with a note saying where to change them.

Raising `automation_mode` to `search_fill_submit` doesn't bypass the gate either, and the
response says so:

> Submission still requires 3 reviewed submissions on this platform (currently 2).

---

## Resolved: The WeasyPrint Native Crash

The Phase 9 crash — one full-suite run in ~6 dying inside `libffi` — is now unable to kill
a run.

**Diagnosis first.** 450 renders across three processes produced no crash, so it isn't
simple repetition; it needs whatever else the suite has loaded. Chasing an intermittent
native fault further wasn't the best use of the time, so the fix targets the consequence
instead of the cause.

**WeasyPrint now renders in a subprocess.** A fault in the native stack becomes a negative
return code, and the existing ReportLab fallback takes over:

```
WeasyPrint render failed (WeasyPrint crashed with signal 11 (isolated, so the run
continues)) — retrying with ReportLab
→ engine: reportlab | PDF written: True
```

Tested by simulating SIGSEGV and a hang, both of which now produce a document instead of a
dead interpreter. A 60s timeout covers a child that hangs rather than crashes.

**A bug this surfaced.** The first version of the worker couldn't `import job_agent` —
the subprocess doesn't inherit the parent's `sys.path`, so it was silently falling back to
ReportLab on *every* render while appearing 3× faster. `PYTHONPATH` is now set explicitly
from the package root, with a test that renders from a different working directory.

**Cost:** ~0.9s per document versus 81ms in-process, dominated by importing WeasyPrint in
a fresh interpreter. Acceptable against the browser work a run does anyway. Tests set
`pdf_isolate_weasyprint=False` (the suite renders a lot of PDFs — 94s became 172s), with
the isolated path covered explicitly. Set `PDF_ENGINE=reportlab` to skip the native stack
altogether.

---

## Acceptance Criteria

| Criterion | Status | Evidence |
|-----------|--------|----------|
| Complete audit coverage across every phase | ✅ | `TestAuditCoverage` — 12 phase-spanning actions, plus every action exports |
| CSV export: jobs, applications, audit log | ✅ | Five exports, all round-tripped through `csv.reader` |
| Applications export has job_id, company, title, submitted_at, status, confirmed_ref | ✅ | `test_required_columns_are_present`, `test_values_are_exported` |
| Audit log filterable by platform, date, action type | ✅ | `TestAuditApi` filter tests |
| Settings: review threshold, daily limits, email rate limit | ✅ | `GET /api/v1/settings`, `PATCH /settings/platforms/{p}` |

---

## Test Coverage

### `tests/test_phase10_audit_export.py` — 52 tests

Application export including unanswered-question and verification columns (5), job export
with CSV round-tripping and stable ordering (6), audit/run/email exports (6), the audit
API and its filters (8), the export API (6), settings including the read-only thresholds
and rejected values (7), and audit coverage across phases (14).

---

## Known Limitations

- **Exports build the whole CSV in memory** before returning. Rows stream from the
  database, but `to_csv()` joins them; a hundred thousand audit rows would be a large
  response. Fine at single-user scale.
- **No date-range filter on the jobs or applications exports** — only audit has one.
- **`detail_json` is exported as a flattened Python repr**, not JSON. Readable in a
  spreadsheet, not machine-parseable; a consumer wanting structure should read the
  database.
- **Settings changes aren't audited.** Changing a daily limit through the API leaves no
  audit entry, which is inconsistent with the rest of the system.
- **No settings UI**, only the API — as with every phase, this project ships endpoints.

---

## All Ten Phases

| Phase | Delivered |
|---|---|
| 0 | Environment, SQLite schema, Keychain |
| 1 | Session manager, per-platform browser profiles, connect/disconnect |
| 2 | Connector framework, capabilities, registry |
| 3 | Search pipeline: dedup, hard filters, fit scoring, daily limits |
| 4 | Document generation with fabrication verification |
| 5 | Form filling, sensitive-field refusal, review queue |
| 6 | Submission behind the clean-submissions gate |
| 6b | Email applications, approval-gated, app-passwords only |
| 7 | 13 platform connectors with honest capability declarations |
| 8 | Orchestrated runs, launchd scheduling, pagination, interruption detection |
| 9 | Session monitoring, reconnect and resume |
| 10 | Audit log, CSV export, settings |

**722 tests**, 55 API endpoints, 15 connectors.

The consistent thread: the agent prepares work and stops at every point where an action
would reach a real employer under the user's name. It won't answer a demographic question,
won't submit a document containing a claim the user's resume doesn't support, won't solve
a CAPTCHA, won't send an email without approval, and won't submit unattended on a platform
it hasn't proven itself on under human review.
