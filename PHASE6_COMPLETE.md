# Phase 6 Complete: Auto-Submit Path & Clean Submissions Gate

**Status:** ✅ Complete
**Date:** 2026-08-15
**Tests:** 44 new Phase 6 tests; 337 passing in the hermetic suite
**Verified on:** Python 3.14.6, Playwright 1.62 + Chromium

---

## What Phase 6 Delivers

The agent can now submit an application — the first genuinely irreversible thing it does.
A submitted form reaches a real employer under the user's name and cannot be recalled, so
most of this phase is the machinery that decides when that's allowed and proves it worked.

```
Application ──▶ SubmissionGate ──▶ ApplicationSubmitter ──▶ SubmissionRecorder
                (may this be           (click, wait,           (status, confirmation,
                 submitted, and         verify)                 audit, clean count)
                 by which path?)
```

---

## Two Bars, Not One

Submission has two paths, and conflating them would be the mistake:

| | **User-directed** | **Unattended auto-submit** |
|---|---|---|
| Who's watching | The user, looking at the form | Nobody |
| Requires | Approved application, required questions answered, documents verified | All of that, **plus** opt-in mode, connector capability, clean-submission threshold, daily limit |
| Endpoint | `POST /api/v1/review/{id}/submit` | Gate consulted by the run loop (Phase 8) |

`GET /api/v1/review/{id}/eligibility` returns **both** verdicts with the checks that
passed and the reason anything is blocked, so the state of the gate is never a mystery.

---

## The Clean-Submissions Gate

`clean_submissions_count` is the agent's track record on a platform. Until it reaches the
threshold (default 3, `settings.clean_submissions_threshold`), **every** application on
that platform goes to review regardless of automation mode. A connector that fills forms
subtly wrong should be caught by a human on its first attempts, not its fiftieth.

Three conditions must all hold for the count to rise:

1. **A human reviewed the application.** An auto-submitted application never increments
   the counter — otherwise the gate would certify itself, ratcheting from three human
   reviews into unlimited unattended submission on its own say-so.
2. **The submission was confirmed.** An unverified submission is not a clean one.
3. **No validation errors** were found on the page afterwards.

Observed end-to-end against a live form:

```
attempt                 auto-allowed  clean count  confirmation
#1 (manual review)      False         1            ACME-99240
#2 (manual review)      False         2            ACME-13373
#3 (manual review)      False         3            ACME-29320
#4 (auto)               True          4            ACME-97813

after lowering daily limit to 4 -> ['Daily apply limit reached for generic_ats (4/4)']
```

### Everything the auto path checks

- Not already submitted, not discarded
- Every **required** question answered
- No attached document carrying fabrication flags from Phase 4
- Automation mode is `search_fill_submit`
- The connector declares `can_submit_automatically` (and its ToS risk note is surfaced)
- `clean_submissions_count >= threshold`
- Under `daily_apply_limit` for today

Optional sensitive questions left blank produce a **warning, not a block** — declining to
answer a voluntary demographic question is a valid choice and the common default. The
user is told it happened.

---

## Confirmation Over Optimism

A click that silently failed validation looks identical to one that succeeded unless the
page is inspected afterwards. An application the user believes was sent — but wasn't — is
worse than an obvious error. So `ApplicationSubmitter`:

1. Screenshots the form **before** submitting (what was sent)
2. Clicks the submit control
3. Waits for the page to settle
4. Looks for evidence: confirmation wording, a reference number, a URL change with the
   form gone
5. Screenshots **after** (what came back)

Validation-error wording is checked *first*, because a form that rejected the submission
must never be read as a success. When nothing can be confirmed, the application is still
marked `SUBMITTED` (the click did happen — pretending otherwise risks a duplicate
application) but with `confirmed=False`, an audit entry of `result="partial"`, a warning
in the API response, and **no** increment to the clean count.

Three real-page behaviours are covered by test: a form that confirms with a reference, a
form that rejects with a validation error, and a form that silently does nothing.

---

## Guards at the HTTP Boundary

Two failure modes specific to a browser-driven agent:

- **No live browser session** → `409`, rather than appearing to succeed.
- **The browser has navigated away from the filled form** → `409`. What the user reviewed
  must be what gets sent; submitting whatever happens to be on screen would send an
  unreviewed page.

---

## Acceptance Criteria

| Criterion | Status | Evidence |
|-----------|--------|----------|
| Three manual submissions raise `clean_submissions_count` to 3 | ✅ | `test_three_reviewed_submissions_reach_the_threshold` |
| 4th application auto-submits under `search_fill_submit` | ✅ | Same test; end-to-end run above |
| Under a fill-only mode it queues for review instead | ✅ | `test_fill_only_mode_blocks` |
| Confirmation captured and linked to the record | ✅ | `test_confirmation_is_captured` (ref `ACME-88421` from a real page) |
| Status transitions to submitted | ✅ | `test_successful_submission_updates_the_application` |
| Daily counter stops at the configured limit | ✅ | `test_daily_limit_blocks`, `test_yesterdays_submissions_do_not_count` |

---

## Test Coverage

### `tests/test_phase6_submission.py` — 35 tests

Gate logic for both paths (18), the recorder and clean-count rules (10), and real-browser
submission against confirming, rejecting, and silent forms (7).

### `tests/test_phase6_submit_api.py` — 9 tests

Eligibility reporting and every refusal at the HTTP boundary: unreviewed, incomplete,
unverified documents, already submitted, no browser, wrong page.

---

## A Phase 5 Test That Had to Change

`test_submit_application_refuses` asserted the connector always refused to submit — true
in Phase 5, obsolete now that Phase 6 implements it. Rather than deleting the coverage it
became `test_filling_never_submits`, which pins the invariant that still holds: filling a
form must never trigger its submission. Filling and submitting are separate acts.

---

## Known Limitations

- **Confirmation detection is wording-based.** A site confirming only with a visual
  checkmark, or in a language the patterns don't cover, will be recorded as
  `confirmed=False`. That is the safe direction — it under-claims rather than
  over-claims — but it means such platforms never accumulate a clean-submission record
  and will always require review.
- **No duplicate-submission detection beyond this record.** If a platform accepted an
  application the agent couldn't confirm, a retry could double-apply. The status is set
  to `SUBMITTED` precisely to make that less likely, at the cost of an occasional
  false positive.
- **The daily limit counts submissions, not attempts.** A failed submission doesn't
  consume budget, which is intended but means a broken connector could retry repeatedly
  within one day.
- **`can_submit_automatically` is currently False on the generic connector**, so auto-submit
  is unreachable in practice until a platform-specific connector declares it in Phase 7.
  The gate is built and tested ahead of the connectors that will use it.
- **No CAPTCHA or MFA handling mid-submission** — that arrives in Phase 9.

---

## Next: Phase 6b — Email-Based Applications

Detecting `apply_method="email"`, composing a draft with the tailored documents attached,
Mail.app automation with IMAP/SMTP fallback, and reply monitoring. Email drafts always go
to the review queue regardless of automation mode — the clean-submissions gate governs
web forms, not outbound mail from the user's own address.
