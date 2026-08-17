# Phase 5 Complete: Application Filling & Review Gate

**Status:** ✅ Complete
**Date:** 2026-08-15
**Tests:** 82 new Phase 5 tests; 285 passing in the hermetic suite
**Verified on:** Python 3.14.6, Playwright 1.62 + Chromium

---

## What Phase 5 Delivers

The agent opens an application form, fills what it actually knows, refuses to answer
what isn't its to answer, screenshots the result, and stops. Everything lands in a
review queue for the user.

```
begin_application ──▶ FormReader      (read every field + the label a human reads)
                          │
                          ▼
                   FieldClassifier    (KNOWN / REMEMBERED / SENSITIVE / UNKNOWN)
                          │
                ┌─────────┴──────────┐
                ▼                    ▼
        fill from profile      leave blank, record why
                └─────────┬──────────┘
                          ▼
                    screenshot → Application(QUEUED_FOR_REVIEW)
                          ▼
                    Review Queue → user answers → approve / discard
```

**Nothing is submitted.** Every application ends in `QUEUED_FOR_REVIEW` regardless of the
platform's automation mode, and `submit_application()` returns `success=False` by design.
Submission arrives in Phase 6 behind the clean-submissions gate.

---

## The Line the Agent Does Not Cross

Four categories of question are never answered automatically, and having a plausible
value available does not change that:

| Category | Why the agent must not answer |
|---|---|
| **Demographics** (gender, race, ethnicity) | Voluntary self-identification under EEO rules. An agent selecting an answer fabricates protected-characteristic data on a legal document — and the user may have good reasons to decline entirely. |
| **Disability status** | Same, plus it can determine accommodation handling. |
| **Veteran status** | Same, and "protected veteran" has a specific legal meaning the agent can't verify. |
| **Compensation** (history and expectation) | Salary history questions are unlawful for employers to ask in several US states, and a wrong number permanently damages the user's negotiating position. |
| **Criminal history, citizenship, age** | Consequences the agent cannot weigh. |

The check runs *first* in `FieldClassifier.classify()`, before any profile lookup, so a
sensitive field is deferred even when a matching value exists. Two tests pin this
specifically: `test_sensitive_wins_even_when_the_profile_could_answer` and
`test_sensitive_is_not_remembered_away`.

**Deferred fields are left blank on the page.** Not a placeholder, not "N/A" — filling
anything risks it being submitted verbatim if something downstream goes wrong. Blank is
the safer failure.

**Sensitive answers are never remembered.** Ordinary answers ("Why do you want to work
here?") are cached on the profile so the same question isn't re-asked on every future
form. Demographic and compensation answers are re-asked every time rather than being
cached and replayed onto other employers' forms.

---

## Components

### 1. `services/form_reader.py`

Extracts every fillable field in a single `page.evaluate()` call. A long form has 30+
inputs; per-field Playwright queries would be slow and prone to the auto-wait stalls that
bit the Phase 3 connector.

For each field it captures the text a *human* reads — associated `<label>`, `aria-label`,
`<legend>`, placeholder, or nearby text. That matters because sensitivity is decided
from the label, not the HTML `name`: a field named `q_12345` asking "Are you a protected
veteran?" has to be recognized from what it says on screen. Hidden, disabled and
submit-type inputs are skipped, and radio groups collapse to one question rather than one
per option.

### 2. `services/field_classifier.py`

Maps a field to one of four outcomes. Beyond the sensitive rules above:

- **KNOWN** — mapped to a `CandidateProfile` value (name, email, phone, location, links,
  notice period, work authorization). Split first/last name fields are derived from
  `full_name`.
- **REMEMBERED** — the user answered this exact question before.
- **UNKNOWN** — no confident mapping. Notably, a mapped field whose profile value is
  *empty* becomes UNKNOWN rather than being filled blank: the user is asked instead.

### 3. `services/application_filler.py`

Fills, defers, screenshots, and queues. Multiple-choice checkboxes and radios are always
deferred — a yes/no answer is a decision, not a lookup. A dropdown whose options don't
contain the profile value is deferred rather than force-matched. Every deferral is
written to the audit log with `result="paused"`.

### 4. `models` — `CandidateProfile`

The user's own details, containing only what they typed themselves. Demographic fields
are deliberately absent from the schema: there is no field for the agent to fill them
from. `remembered_answers` holds non-sensitive answers keyed by a normalized question.

### 5. `dashboard/routes/review.py` — the Review Queue

| Method | Path | Purpose |
|--------|------|---------|
| GET/POST | `/api/v1/review/profile` | The candidate profile forms are filled from |
| GET | `/api/v1/review` | Queue with counts of what needs answers |
| GET | `/api/v1/review/{id}` | Filled fields + deferred questions + **the source job posting** |
| GET | `/api/v1/review/{id}/screenshot` | The filled form as the agent saw it |
| POST | `/api/v1/review/{id}/answers` | Answer deferred questions |
| POST | `/api/v1/review/{id}/approve` | Mark reviewed and ready |
| POST | `/api/v1/review/{id}/discard` | Throw it away |

The detail endpoint separates `sensitive_questions` from `other_questions` — they are not
the same thing. One is a gap in the agent's knowledge; the other is a question only the
user may answer. It also surfaces `unverified_documents`, so a resume that Phase 4
flagged for unsupported claims is visible at the moment of review.

**Approval cannot proceed while required questions are unanswered** — approving a form
with a blank required field produces a rejected or half-complete application. Approval
records the user's decision; it does not transmit anything.

---

## Acceptance Criteria

| Criterion | Status | Evidence |
|-----------|--------|----------|
| Fills known fields (name, email, phone, resume) on a real form | ✅ | `test_known_fields_are_filled`, `test_resume_is_attached` |
| Unknown field pauses into the Review Queue | ✅ | `test_unknown_question_is_deferred`, `test_application_is_queued_not_submitted` |
| Screenshot shows the filled form with timestamps | ✅ | `test_screenshot_is_captured` (PNG verified), `filled_at` recorded |
| User reviews, edits, approves | ✅ | `TestAnsweringQuestions`, `TestApproveAndDiscard` |
| Approved form data logged, awaiting manual submission | ✅ | `test_approval_does_not_submit` |
| No auto-submit anywhere | ✅ | `test_submit_application_refuses` |
| Sensitive fields never auto-answered | ✅ | `TestSensitiveFieldDetection` (19 cases) |

Verified end-to-end against a real form in Chromium. Of 18 fields: 9 filled (contact
details, links, resume, notice period, work authorization), 9 deferred — 6 sensitive
(both salary questions, gender, race, veteran status, disability) and 3 unknown (cover
letter not generated, "Why do you want to work here?", "How did you hear about this
role?"). The screenshot confirms every compensation and self-identification field was
left untouched.

---

## Test Coverage

### `tests/test_phase5_filling.py` — 54 tests

Sensitive detection across 16 real-world label phrasings, known-field mapping, remembered
answers, form reading against real markup, filling in a live browser, screenshot capture,
queueing, and the connector integration. Browser tests skip automatically without
Chromium.

### `tests/test_phase5_review_api.py` — 28 tests

Profile CRUD, queue listing and counts, the side-by-side detail payload, screenshot
download, answering (including that sensitive answers are *not* remembered), approval
gating on required answers, and discard.

---

## Bug Found During Verification

The form page rendered its title as `Apply â€" Senior Backend Engineer` — UTF-8 bytes
decoded as a legacy charset. This was more than cosmetic: label text is the input to
sensitive-question matching, so a mojibaked label could silently fail to match. Fixed
with an explicit `<meta charset="utf-8">` and a charset on the test server's
`Content-Type`, and pinned by `test_labels_decode_as_utf8`.

---

## Known Limitations

- **Classification is pattern-based.** A form asking "Tell us about your background"
  meaning ethnicity would not be caught. The patterns cover the standard phrasings used
  by Greenhouse, Lever, Workday and the EEOC's own wording, but an unusual form can slip
  through — which is exactly why every application stops for human review.
- **Multi-step forms are read one page at a time.** A wizard's later steps aren't visible
  until navigated to; there is no step-through logic yet.
- **Custom widgets are not handled.** React comboboxes, rich-text editors, and drag-drop
  uploads don't present as native `<input>`/`<select>`, so they're invisible to the
  reader and simply won't be filled.
- **The screenshot is full-page at the current viewport.** Fields below a lazy-loading
  boundary may not appear.
- **`remembered_answers` is global, not per-company.** A remembered "Why do you want to
  work here?" answer naming one company would be wrong for the next — the field is
  pre-filled, and the user is expected to read it during review.

---

## Next: Phase 6 — Auto-Submit Path & Clean Submissions Gate

`clean_submissions_count` tracking, the mandatory first-N review gate (default 3), actual
form submission via Playwright with confirmation capture, and daily apply limits. The
review queue built here is the thing that gate counts against: only applications a human
actually reviewed should advance the counter.
