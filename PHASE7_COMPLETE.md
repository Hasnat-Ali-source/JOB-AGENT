# Phase 7 Complete: Additional Connectors

**Status:** ✅ Complete — with an important caveat about verification (below)
**Date:** 2026-08-15
**Tests:** 165 new Phase 7 tests; 569 passing in the hermetic suite
**Connectors:** 15 registered (13 new)

---

## What Phase 7 Delivers

Thirteen new platform connectors, each declaring honestly what it can do.

| # | Platform | Search | Read | Fill | Submit | Note |
|---|---|:--:|:--:|:--:|:--:|---|
| 7a | **Greenhouse** | ✅ | ✅ | ✅ | ✅ | Follows embedded board iframes |
| 7b | **Lever** | ✅ | ✅ | ✅ | ✅ | `/apply` path derived directly |
| 7c | **Ashby** | ✅ | ✅ | ✅ | ❌ | Custom-question heavy; user submits |
| 7d | **Workday** | ✅ | ✅ | ❌ | ❌ | Per-tenant wizard; opens the application only |
| 7e | **SmartRecruiters** | ✅ | ✅ | ✅ | ✅ | |
| 7e | **Workable** | ✅ | ✅ | ✅ | ✅ | |
| 7f | **LinkedIn** | ✅ | ✅ | ❌ | ❌ | Read-only, ToS risk note |
| 7g | **Indeed** | ✅ | ✅ | ❌ | ❌ | Read-only, ToS risk note |
| 7h–j | **Glassdoor, ZipRecruiter, Wellfound, Dice, JobStreet** | ✅ | ✅ | ❌ | ❌ | Read-only, ToS risk notes |

4 can submit, 11 are read-only, 10 carry risk notes.

---

## Read This Before Trusting the Table

**No connector here has been run against a live tenant from this machine.** Doing so
would mean submitting real applications to real employers, or hammering job boards whose
terms restrict exactly that. Every connector is built from each platform's published URL
structure and public form markup, and tested against local fixtures replicating it.

That distinction is recorded in the code rather than buried in a doc.
`VerificationLevel` has three values — `FIXTURE`, `LIVE_READ`, `LIVE_SUBMIT` — every
connector is currently `FIXTURE`, the API exposes it as `verified_against`, and a test
asserts nothing has silently claimed more:

```python
def test_nothing_claims_live_verification_yet(self, connector_class):
    assert connector_class.VERIFIED_AGAINST == VerificationLevel.FIXTURE
```

So "the connector claims it can submit" and "submitting has been proven against the real
site" stay separable. The Phase 6 clean-submissions gate is what protects the user in the
gap: the first three applications on any platform go through human review regardless of
what a capability claims.

---

## Consumer Boards Are Read-Only by Design

Applying through Greenhouse means filling in a form the employer published for
applicants. LinkedIn, Indeed and the rest are third parties whose terms restrict
automated access — and whose enforcement lands on **the user's own account**. A suspended
LinkedIn account costs far more than the applications it saved.

So every board connector has `can_submit_automatically=False` and
`can_fill_standard_fields=False`, plus a risk note explaining why:

> LinkedIn's User Agreement prohibits automated access, and enforcement targets your
> account, not the tool. This connector searches and reads postings through your own
> signed-in session and never submits. Where a posting links to the employer's own ATS,
> apply there instead.

That last sentence is the actual workflow this enables: use the boards for **discovery**,
then apply through the employer's own ATS. Most of the value is in search and reading
anyway.

Turning submission on would be a deliberate code change to a capability declaration, not
a settings toggle — the gate consults `can_submit_automatically`, so leaving it false
makes unattended submission unreachable by construction. A test proves the gate and the
flag agree:

```
decision = SubmissionGate(session).check_auto(application, account, LinkedInConnector())
assert decision.allowed is False   # even with clean_submissions_count=99
```

---

## Where Honesty Cost a Capability

**Workday** could plausibly have claimed form filling. It doesn't. Workday is configured
per employer: the application is a multi-step wizard whose steps, field names and
required questions differ between tenants, and most require creating an account first. A
connector that fills two of five wizard steps and stops is worse than one that doesn't
start — it leaves half-complete applications and a user who thinks the job is done. It
claims search, read, and opening the application.

**Ashby** fills fields but doesn't claim submission. Its boards lean on custom questions,
and a posting whose required custom question the agent can't answer must not be submitted
half-complete.

---

## Architecture

`HostedATSConnector` extends `GenericATSConnector`, so all thirteen inherit the Phase 3–6
machinery: JSON-LD parsing with HTML fallback, the field classifier that refuses
demographic and compensation questions, submission that verifies rather than assumes.
Subclasses supply only what differs:

```python
class LeverConnector(HostedATSConnector):
    PLATFORM = "lever"
    BOARD_URL_TEMPLATE = "https://jobs.lever.co/{board}"
    JOB_LINK_PATTERN = re.compile(r"jobs\.lever\.co/[^/]+/[0-9a-f]{8}-[0-9a-f]{4}", re.I)
    APPLY_PATH_SUFFIX = "/apply"
    CAPABILITIES = PlatformCapabilities(...)
```

Two behaviours are meaningfully better than the generic connector's:

- **Link collection by URL pattern.** The generic connector guesses at listing containers
  (`div[class*='job']`); a hosted board has one recognizable posting URL shape, so links
  are filtered by regex. Navigation links are excluded rather than scraped.
- **Direct apply URLs.** Lever, Ashby and Workable derive the form URL from the posting
  URL, so there's no hunting for an apply button.

### API

`GET /api/v1/connectors` returns every connector with capabilities, risk notes,
verification level, and whether the platform is connected — this is what lets the GUI hide
unsupported actions and show a risk note before anyone enables automation.

---

## Acceptance Criteria

| Platform | Criterion | Evidence |
|---|---|---|
| 7a Greenhouse | Open search, read details, fill fields, submit | `test_greenhouse_collects_only_job_links`, `test_greenhouse_reads_job_details`, `test_greenhouse_fills_standard_fields` |
| 7b Lever | Open search, read details, fill, submit | `test_lever_collects_uuid_postings`, URL and capability tests |
| 7c Ashby | Read details, fill, detect custom questions | Capability + URL tests; custom questions deferred by the Phase 5 classifier |
| 7d Workday | Navigate tenant UI, read details | `test_workday_accepts_a_full_host`, link-pattern tests |
| 7e SmartRecruiters/Workable | Open application flow, fill, submit | `test_workable_collects_postings`, URL tests |
| 7f LinkedIn | Search, read, open application; submit opt-in only | `test_linkedin_collects_job_views_only`, `test_read_only_board_refuses_to_submit` |
| 7g Indeed | Search, read; auto-submit disabled | Capability + risk-note tests |
| 7h–j Others | Search + read; auto-submit disabled with risk note | `test_consumer_boards_never_submit` (7 platforms) |

The Greenhouse fill test is the most complete: against fixture markup using Greenhouse's
real field names, it fills first name, last name, email and phone, **defers the custom
question**, and leaves the demographic dropdown untouched and categorised `sensitive`.

---

## Test Coverage

### `tests/test_phase7_connectors.py` — 156 tests

Capability honesty checked across all 13 connectors at once (submission implies filling,
filling implies reading, verification declared, boards never submit, every board has a
risk note, every platform has a review threshold ≥ 3), URL building and board-token
round-tripping, job-link patterns accepting postings and rejecting navigation, and
browser-driven runs against fixtures for Greenhouse, Lever, Workable and LinkedIn.

### `tests/test_phase7_connectors_api.py` — 9 tests

The catalogue endpoint, filtering, connection state, and the read-only/risk-note
guarantees at the HTTP boundary.

---

## Known Limitations

- **Fixture verification only**, as above. Live sites change markup without notice; the
  first live run of any connector should be watched.
- **Board filters are not driven.** `can_filter=False` on the ATS connectors: their
  boards mostly have no filter controls, so the Phase 3 hard-filter pass does the work
  instead. This costs bandwidth (more postings read than needed), not correctness.
- **Ashby and Workday render client-side.** The inherited `networkidle` wait covers most
  of it, but a slow tenant may need a longer settle than the 10s default.
- ~~**No pagination.**~~ **Resolved in Phase 8** — `collect_job_links()` now walks up to
  5 pages via the platform's page parameter, a next-page control, or a load-more button.
  Verified at 12 postings across 3 pages.
- **LinkedIn/Indeed markup is obfuscated and A/B tested.** Their link patterns are stable
  (`/jobs/view/{id}`, `viewjob?jk=`), but detail extraction depends on JSON-LD that these
  sites do not always serve to non-authenticated sessions.
- ~~**No CAPTCHA handling.**~~ **Resolved in Phase 8** — `InterruptionDetector` recognizes
  CAPTCHA, MFA, sign-in walls, rate limiting and blocks, pauses the platform and never
  attempts to solve a challenge. Recovery (the Reconnect flow) is still Phase 9.
- **`EXTRA_FIELD_ALIASES` is declared but not yet consumed** by the field classifier.
  Lever's `urls[LinkedIn]` still classifies correctly today because the classifier reads
  the visible label rather than the field name, so this is a refinement rather than a gap.

---

## Next: Phase 8 — Scheduling & Automation Loop

The orchestrator that ties every phase together: load a search profile, iterate connected
platforms, run the pipeline, respect daily limits, write the run summary. Plus launchd
scheduling. This is where pagination and the `:cloud`-model caution start to matter,
because runs happen unattended.
