# Phase 6b Complete: Email-Based Applications

**Status:** ✅ Complete
**Date:** 2026-08-15
**Tests:** 67 new Phase 6b tests; 404 passing in the hermetic suite
**Verified on:** Python 3.14.6

---

## What Phase 6b Delivers

Many postings don't have a form at all — they say "send your CV to careers@…". This phase
handles that path end to end:

```
posting ──▶ EmailApplicationDetector   (which address, and why?)
                    │
                    ▼
            EmailComposer              (subject, body from the verified cover letter,
                    │                    resume + letter as PDF attachments)
                    ▼
              EmailDraft ──▶ user reviews ──▶ user approves ──▶ EmailSender
                                                                    │
                                                    EmailThread ◀───┘ ──▶ reply polling
```

---

## Email Always Stops for Review

A web form is submitted inside a site's own workflow. An email leaves the user's personal
address, in their name, to a named person, and **cannot be unsent**. So:

- Every draft is created in `DRAFT` status and stops. The platform's automation mode is
  irrelevant — there is no configuration that makes email send unattended.
- **There is no code path from `DRAFT` to `SENT`.** `send()` raises unless the draft is
  `APPROVED` *and* `reviewed_by_user` is true.
- The Phase 6 clean-submissions gate deliberately does **not** apply here. "The agent did
  this correctly three times" doesn't transfer to a different recipient reading a
  different message.
- **Editing revokes approval.** An approval applies to the text the user actually read;
  changing the subject or body after approving returns the draft to `DRAFT`.

Sending is rate-limited to `settings.email_rate_limit` per hour, checked at send time
rather than trusted to the caller — a loop bug that fired a hundred applications at one
company would be unrecoverable reputationally.

---

## Recipient Detection

Emailing the wrong address means sending the user's resume to a stranger, so detection is
scored and always explains itself. Addresses are judged on two signals:

- **The local part** — `careers`, `jobs`, `hiring`, `talent` score positively;
  `noreply`, `unsubscribe`, `privacy`, `press`, `support`, `billing` score negatively.
  Matching tries the whole local part, the separator-stripped form, and each token, so
  `no-reply` is caught (its tokens `no` and `reply` are individually innocuous) and
  `careers-emea` is still recognized.
- **The surrounding text** — "send your CV to", "email your application to" score
  positively; privacy and unsubscribe wording scores negatively.

Below a confidence threshold nothing is proposed and the user is asked to supply the
address. `GET /api/v1/email/detect` shows every candidate with its score and reasoning,
so a wrong pick is visible rather than silent:

```
✓ chosen    careers@acme-robotics.test    score= 11
✗ rejected  press@acme-robotics.test      score= -9
✗ rejected  no-reply@acme-robotics.test   score= -9
```

### Two detection bugs found while building this

1. **`no-reply@` was accepted.** Token-only matching split it into `no` + `reply`, neither
   of which is in the blocklist. Now matched three ways.
2. **Neighbouring addresses contaminated each other.** A fixed 120-character window around
   `careers@acme.test` reached into the following "Unsubscribe: no-reply@…" text, so the
   *correct* address inherited the penalty and the user was shown contradictory reasons for
   the same pick. Context is now clipped at both the neighbouring address and the end of
   the address's own sentence. Score for the correct address went from 5 to 11.

---

## Composition

The body is the **tailored cover letter from Phase 4**, which has already been verified
against the user's master document. Nothing new is written at send time — composing fresh
prose about the candidate would bypass that verification entirely.

With no cover letter available, the body falls back to a short factual note that makes no
claim about the candidate's experience beyond their name and the role ("My resume is
attached"), and the draft carries a warning to edit it. Unverified documents and missing
PDFs are surfaced as warnings on the draft.

---

## Sending: Never the Account Password

| Backend | When | Credentials |
|---|---|---|
| **Mail.app** (preferred) | macOS, `mail_app_enabled` | **None.** Uses the account the user already configured; the message lands in their Sent mailbox and threads normally with replies |
| **SMTP** (fallback) | `imap_smtp_enabled` | An **app-specific password** from the Keychain |

The user's actual account password is never requested, stored, or accepted. App-specific
passwords can be revoked individually and can't be used to sign in to the account. When
one is missing, the error says so explicitly rather than inviting the user to paste their
real password.

AppleScript is written to a temp file rather than passed via `osascript -e`: cover letters
are multi-line and contain quotes, and shell-quoting them is exactly the kind of thing
that silently mangles a user's letter. Escaping handles backslashes before quotes (order
matters) and converts real newlines, which would otherwise break the string literal.

---

## Reply Monitoring

After sending, an `EmailThread` is opened in `AWAITING_REPLY`. `POST /api/v1/email/check-replies`
polls IMAP, matches messages by sender against threads still awaiting a reply, stores a
200-character snippet, and links it back to the application.

---

## API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/v1/email/detect` | Which address would be used, with all candidates and scores |
| POST | `/api/v1/email/drafts` | Compose a draft (detects the recipient, or takes an override) |
| GET | `/api/v1/email/drafts` | List drafts, with the hourly quota remaining |
| GET | `/api/v1/email/drafts/{id}` | The draft exactly as it would be sent |
| PATCH | `/api/v1/email/drafts/{id}` | Edit subject/body/recipient (revokes approval) |
| POST | `/api/v1/email/drafts/{id}/approve` | Approve sending |
| POST | `/api/v1/email/drafts/{id}/send` | Send an approved draft |
| POST | `/api/v1/email/drafts/{id}/discard` | Discard without sending |
| GET | `/api/v1/email/threads` | Sent applications and replies |
| POST | `/api/v1/email/check-replies` | Poll the inbox |

Responses use `status` for the draft's own state and `action` for what just happened —
they were previously the same key, and the action result was silently overwritten by the
state when the summary was spread into the response.

---

## Acceptance Criteria

| Criterion | Status | Evidence |
|-----------|--------|----------|
| "Email resume to: X" is detected | ✅ | `test_detects_the_application_address` |
| Draft composes with subject, cover-letter body, resume attached | ✅ | `TestComposition` (7 tests) |
| Draft appears for review rather than sending | ✅ | `test_draft_is_created_for_review`, audited as `paused` |
| Approval then send logs message-id and sent_at | ✅ | `test_approved_draft_sends` |
| Reply polling links replies to the application | ✅ | `check_replies`, `test_reply_is_surfaced` |
| Rate limiting caps sends per hour | ✅ | `TestRateLimiting` (3 tests) |
| No account password is ever used | ✅ | `test_smtp_without_keychain_password_names_the_fix` |

---

## Test Coverage

### `tests/test_phase6b_email.py` — 50 tests

Recipient detection including every rejected inbox type and the two bugs above (12),
composition (7), the draft → approve → send lifecycle with its refusals (11), application
and thread linkage (2), rate limiting (3), and sender internals including AppleScript
escaping and address validation (15).

### `tests/test_phase6b_email_api.py` — 17 tests

Detection preview, draft CRUD, approval revocation on edit, every send refusal, and
thread listing.

---

## Known Limitations

- **Detection is heuristic.** A posting saying "drop us a line at hello@startup.test" with
  no other cue scores too low to propose, so the user must supply the address. That is the
  safe direction — proposing a wrong address is worse than asking.
- **Mail.app sending is untested on this machine.** The AppleScript path is unit-tested for
  construction and escaping, but no real message was sent; doing so would email a real
  recipient. The SMTP path is likewise tested only through a fake sender.
- **Reply matching is by sender address**, not `In-Reply-To`/`References` headers. A reply
  from a different address at the same company (a hiring manager rather than the careers
  inbox) won't be linked automatically.
- **IMAP polling is manual** — there is no scheduled poll until Phase 8's run loop.
- **No attachment size check.** A large PDF could be rejected by the recipient's server;
  the send would fail with whatever error the server returns.
- **Plain-text bodies only.** No HTML alternative part.

---

## Next: Phase 7 — Additional Connectors

Greenhouse, Lever, Ashby, Workday, SmartRecruiters/Workable, then the browser-assisted
consumer boards (LinkedIn, Indeed) with auto-submit disabled by default. Each connector
declares its own capabilities honestly, and the Phase 6 gate means each has to earn
unattended submission on its own track record.
