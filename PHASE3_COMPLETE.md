# Phase 3 Complete: Job Search & Pipeline

**Status:** ✅ Complete
**Date:** 2026-08-14
**Tests:** 82 new Phase 3 tests; 120 passing across the whole suite
**Verified on:** Python 3.14.6, Playwright 1.62 + Chromium

---

## What Phase 3 Delivers

The agent can now run a search on a connected platform, read every posting it finds,
recognize jobs it has already seen, apply hard filters, score fit, and store everything
to SQLite — with the whole run recorded in the audit log and exposed over the dashboard API.

```
SearchProfile ──▶ SearchPipeline ──▶ Connector (open_search → filters → links → details)
                        │
                        ├─▶ JobDeduplicator   (dedup_hash, then fuzzy near-match)
                        ├─▶ FilterEvaluator   (salary, exclusions, location, date, job type)
                        ├─▶ FitScorer         (heuristic today, LLM at Phase 4)
                        └─▶ SQLite            (Job rows + AuditLog entries)
```

---

## Components

### 1. Search Pipeline — `job_agent/core/search_pipeline.py`

The orchestrator. One `search()` call runs an entire pass over a platform.

- **Daily search limit.** Before opening a search it computes the remaining budget for
  the day by summing `jobs_found` from today's `SEARCH_RUN` audit entries for that
  platform. Collected links are truncated to that budget, and a run with no budget left
  short-circuits without opening the search page at all. `limit_reached` is reported back.
- **Two-pass deduplication.** Exact `dedup_hash` lookup first; if that misses, the
  posting is fuzzy-compared against the 200 most recently seen jobs. Duplicates are
  skipped and recorded as `JOB_DEDUPED` audit entries.
- **Hard filters don't delete data.** A job that fails a filter is still stored, with
  `hard_filter_pass=False` and `status="filtered_out"`, so the user can see what was
  rejected and why. Only duplicates are dropped.
- **Per-job error isolation.** A posting that fails to read is logged into
  `result.errors` and the run continues with the next link.
- **Injectable connector.** `search(account, profile, connector=...)` accepts a
  pre-built connector, which is how the pipeline is tested without a browser. In
  production the connector is built from the registry and bound to an authenticated
  page from `SessionManager`.

`SearchResult` reports: `jobs_found`, `new_jobs`, `duplicates_skipped`,
`hard_filters_failed`, `limit_reached`, `errors`, and the stored `Job` objects.

### 2. Deduplication — `job_agent/services/job_deduplicator.py`

`generate_dedup_hash()` normalizes before hashing: lowercases, strips company suffixes
(Inc/LLC/Ltd/GmbH/…), strips seniority prefixes (Senior/Sr./Lead/Staff/…), and collapses
remote variants ("Work From Home", "Remote - US" → "remote"). So
`TechCorp Inc. / Senior Backend Engineer / Remote` and
`techcorp / backend engineer / REMOTE` hash identically.

`is_duplicate()` compares company, title, and location **independently** rather than
averaging them. The blended average previously used was unsafe: `Company1` and
`Company2` are 87% similar, so with an identical title and location the average cleared
any reasonable threshold and merged two unrelated employers' postings. Now the employer
must match at ≥95%, and title and location must each clear the threshold (default 85%)
on their own.

`find_duplicate_job()` runs that check across a list of existing records.

### 3. Hard Filters — `job_agent/services/filter_evaluator.py`

Binary pass/fail on salary minimum, excluded keywords, location/remote preference,
date posted, and job type. Missing data is never treated as failure: a posting with no
salary or no posted date passes rather than being silently dropped. Salary parsing
handles `$100,000`, `100k-150k`, and `$100,000 - $150,000`.

### 4. Fit Scoring — `job_agent/services/fit_scorer.py`

Heuristic scorer over title, seniority, location, keywords, and exclusions.

**Bug fixed this phase:** the old implementation started from a 0.5 base, added weighted
components, then divided by the *sum of weights* — so a title-only profile scored
`(0.5 + 0.3×0.3) / 0.3 ≈ 1.97`, clamped to 1.0. Every job scored 1.0 regardless of
match quality. It is now a true weighted average over whichever dimensions the profile
specifies, returning 0.5 when a profile specifies nothing to match on.

Phase 4 replaces `FitScorer.score_job()` with LLM scoring behind the same interface.

### 5. Date Normalization — `job_agent/utils/dates.py`

`parse_posted_at()` turns whatever a job board prints into a naive UTC datetime:
ISO 8601 (with `Z` or offset), relative text ("2 days ago", "Posted 1 week ago",
"today", "yesterday"), and human formats ("August 12, 2026", "12 Aug 2026", "08/12/2026").
Unparseable input returns `None`; the raw text is preserved in `Job.posted_at_text`.

### 6. Dashboard API — `job_agent/dashboard/routes/search.py`, `deps.py`

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/v1/search/profiles` | List search profiles |
| POST | `/api/v1/search/profiles` | Create a search profile |
| POST | `/api/v1/search/run` | Run the pipeline on a platform |
| GET | `/api/v1/search/stats` | Totals, per-platform/status breakdown, today's run metrics |
| GET | `/api/v1/jobs` | List jobs (filter by platform, status, hard_filter_pass, min_fit_score) |
| GET | `/api/v1/jobs/{job_id}` | Full job details |

`dashboard/deps.py` is new and provides one shared engine + session dependency.
Previously `main.py` and `accounts.py` each built their own engine from
`str(settings.database_url)` — which stringifies to `"None"` when `DATABASE_URL` is
unset. Both now use `settings.database_url_computed`, which falls back to
`~/Library/Application Support/job-agent/job_agent.db`.

---

## Fixes to Earlier Phases

These blocked Phase 3 and were repaired in place:

1. **`job_agent/config.py`** — `database_url: str = None`, `database_path: Path = None`,
   `anthropic_api_key: str = None`, and `log_file: Path = None` are invalid under
   Pydantic v2; importing `settings` raised `ValidationError`, so *nothing* in the
   package could be imported. Now `Optional[...]`.
2. **`TestConnector.__init__`** took no arguments, but the registry constructs
   connectors as `connector_class(platform_name)` — so `create_connector("test_connector")`
   silently returned `None`. It now accepts `platform_name`.
3. **`set_page()` existed only on `GenericATSConnector`**, though the pipeline calls it
   on every connector. Moved to `ConnectedPlatformConnector` along with a `page` property.
4. **`GenericATSConnector.read_job_details()`** parsed whatever page happened to be open
   instead of the URL it was given. It now navigates first.
5. **`requirements.txt`** listed `python-version >= 3.11` as a dependency, which is not a
   package — `pip install -r requirements.txt` failed on the first line. Now a comment.
6. **The whole pinned dependency set predated Python 3.14** (pydantic 2.5.0, sqlmodel
   0.0.14, lxml 4.9.3, …) and would not build. Every pin was re-resolved and verified by
   actually installing and importing it on 3.14.6; dev/test tools moved to
   `requirements-dev.txt`.
7. **`datetime.utcnow()`** (deprecated, removal scheduled) was used in nine places
   including every model's `default_factory`. Replaced with `utils.dates.utcnow()`,
   which returns a naive UTC datetime so it stays comparable with the naive datetime
   columns already in the schema.
8. **Pydantic's class-based `Config`** is deprecated for v3 removal; `Settings` now uses
   `SettingsConfigDict`.
9. **The virtualenv location was inconsistent** — `scripts/setup.py` created `venv/`
   while `.gitignore` and the docs referenced both. Standardized on `.venv/`.

---

## Test Coverage

### `tests/test_phase3_generic_search.py` — 43 tests

Search navigation, split across three layers:

- **Strategy (no browser, 22):** query/location term building, template substitution and
  URL-encoding, the three navigation strategies, submit-button-over-Enter, filter
  application, and the honest no-op when nothing matches.
- **JSON-LD and salary shapes (16):** every nesting variant real sites emit — the three
  regressions above are pinned here.
- **Live browser (5):** real Chromium against real markup and a threaded HTTP career
  site — full pipeline search → collect → parse → dedup → filter → store, plus a timing
  assertion that an unparseable page returns in under 5s. These skip automatically when
  no browser is installed.

### `tests/test_phase3_search.py` — 39 tests

- **Deduplication (7):** hash stability, normalization of company/title/location variants,
  distinct jobs staying distinct, fuzzy near-match, fuzzy rejection of unrelated jobs,
  candidate-list search.
- **Hard filters (9):** the $80k-vs-$100k acceptance case, salary above minimum, missing
  salary tolerance, `k` notation, exclusion keywords, remote preference, stale and recent
  postings, job type mismatch.
- **Fit scoring (5):** bounded output, title match beating mismatch, alt-title ranking,
  proportional keyword coverage, exclusion penalty.
- **Date parsing (6):** ISO with `Z`, relative days, prefixed relative text, human format,
  today/yesterday, unparseable input.
- **Pipeline (12):** 10 jobs collected and stored, field population, duplicate within a
  run, duplicate across runs, hard-filter failures stored not dropped, daily limit capping
  collection, daily limit shared across runs in a day, exhausted limit short-circuiting,
  run metrics in the audit log, dedup events audited, per-job read failure isolation,
  connector hooks called.

Run them:

```bash
.venv/bin/python -m pytest tests/ -v
```

---

## Acceptance Criteria

| Criterion | Status | Evidence |
|-----------|--------|----------|
| Run a search against a test platform, collect 10 jobs, store to SQLite | ✅ | `test_collects_ten_jobs_and_stores_them` |
| Same posting from two search results → one deduplicated | ✅ | `test_duplicate_from_second_result_is_skipped`, `test_duplicate_across_runs_is_skipped` |
| salary min=$100k → $80k job marked `hard_filter_pass=False` | ✅ | `test_hard_filter_failure_is_stored_not_dropped` |
| Dashboard shows jobs_found, new_jobs, duplicates_skipped | ✅ | `/api/v1/search/run` response + `/api/v1/search/stats` `today` block |
| daily_search_limit respected during collection | ✅ | `test_daily_search_limit_caps_collection`, `test_daily_limit_is_shared_across_runs` |
| The whole chain works against a live career site | ✅ | `TestLivePipeline::test_full_search_to_storage` — browser + HTTP server, 4 found / 3 stored / 1 deduped / $80k filtered |

---

## Generic Connector Search (closed out before Phase 4)

`open_search()` and `apply_search_filters()` were shipped as no-ops in Phase 2 and are
now implemented. `open_search()` picks one of three strategies:

1. **Templated URL** — `search_url` contains `{query}`/`{location}` placeholders
   (`https://acme.com/careers?q={query}&loc={location}`). Terms are URL-encoded and the
   page is opened directly. Most reliable; prefer this for any known career site.
2. **Plain URL** — navigate to `search_url`, then drive the page's own search form.
3. **No URL** — drive the search form on whatever page is already open.

If none can run, the current page is left untouched and a warning is logged;
`last_search_url` stays `None` so callers can tell a real search from a page read as-is.

`apply_search_filters()` is best-effort across the common patterns — a date-posted
`<select>`, a job-type `<select>`, a remote checkbox — and records what actually
succeeded in `applied_filters`. Filters that don't match cost recall, not correctness:
the hard-filter pass re-checks every criterion afterward.

The search entry point is configured per account via the new
`platform_accounts.search_url` column, passed to the connector by the pipeline.
`scripts/init_db.py` gained an idempotent additive-column migration so existing
databases pick it up.

### Bugs this surfaced

Driving a real browser against a live HTTP career site exposed three defects that no
amount of stubbed testing would have caught:

1. **JSON-LD parsing crashed on valid postings.** `identifier` and `hiringOrganization`
   were assumed to be objects (`.get("value")`), but schema.org allows plain strings —
   `'str' object has no attribute 'get'`, and every such posting silently fell back to
   HTML scraping. Parsing now handles string/object/list shapes, `@graph`, `ItemList`,
   list-valued `@type`, and multiple JSON-LD blocks per page.
2. **Salary was never extracted.** Spec-compliant markup nests the figures in a
   `QuantitativeValue` under `baseSalary.value`; the parser only read the top level, so
   `salary` was always `None` — which silently disabled the salary hard filter. Both
   shapes plus scalar values now work.
3. **One unparseable posting stalled a run for ~35 seconds.** The HTML fallback probed
   ten speculative selectors with `text_content()`, which auto-waits Playwright's 30s
   default per call. Existence is now checked with `count()` (immediate) before any
   text is fetched; the same fix applies to `collect_job_links()`. Measured: 35s → under
   1s for a page with no job markup.

## Known Limitations

- **Fit scoring is heuristic, not semantic.** Keyword and substring matching only —
  it cannot tell that "Golang" satisfies a "Go" requirement, and a "Junior Backend
  Engineer" scores identically to a senior one against a "Backend Engineer" profile.
  Phase 4 replaces it.
- **Generic search still depends on a configured `search_url` per account.** Without
  one, the connector can only search a page that already has a recognizable form.
  Site-specific search navigation arrives per-platform in Phase 7.
- **Filter selectors cover common patterns only.** Sites using custom dropdowns,
  React comboboxes, or faceted sidebars won't match; those runs fall back to the
  hard-filter pass, which is correct but pulls more pages than necessary.
- **The daily budget is derived from audit entries, not a counter table.** Accurate for
  normal operation; a manually deleted audit row would reopen budget.
- **The fuzzy dedup window is the 200 most recent jobs**, not the full table, to keep
  per-posting cost bounded. Older near-duplicates are caught only by exact hash.
- **`Job.salary` remains free text.** Filtering parses it per-comparison; no currency
  normalization, so a €90,000 posting is compared numerically against a dollar minimum.

---

## Next: Phase 4 — Document Generation

Resume/cover-letter templating, PDF export, tailoring prompts, and version storage
linked to `applications.resume_version` / `cover_letter_version`.
