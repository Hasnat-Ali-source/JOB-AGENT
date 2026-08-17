# Job Agent — Product Context

## What it is

A local macOS agent that searches job boards, reads postings, tailors the user's real
resume to each one, fills application forms, and stops for the user before anything
reaches an employer. Everything runs on the user's own machine, through their own
signed-in browser sessions, against a local SQLite database.

## Platform

`web` — a local browser UI at `http://127.0.0.1:8000`, backed by the existing FastAPI
app. Single user, single machine, not deployed or multi-tenant.

## Stack

**React + Vite** (chosen by the user over vanilla and HTMX). Built assets are served by
the existing FastAPI app so `python -m job_agent dashboard` remains the one command to
run everything. The Python API is unchanged — 58 JSON endpoints across ten built phases.

## Primary user

The owner of the machine, running their own job search. There is no second role, no
admin, no team. They are technical enough to have installed a Python CLI but want to
stop driving it through Swagger.

## The job they're doing

Applying for jobs without spending every evening on it — while staying able to answer for
everything sent in their name. They alternate between two situations:

- **Checking in** (most common): the agent has prepared work; the user approves, edits, or
  discards it. **This is the home screen.**
- **Setting up** (occasional): connecting a platform, uploading a resume, defining what
  they're looking for.

## What the product makes possible

The distinguishing mechanism is *where it stops*. Comparable tools maximize applications
sent. This one refuses to:

- answer demographic, disability, veteran or compensation questions — ever, whatever the
  profile contains
- submit a document containing a claim the user's master resume doesn't support
- solve a CAPTCHA
- send an email without explicit per-message approval
- submit unattended on a platform until three applications there have been reviewed by a
  human and confirmed clean

Those refusals are the product, not friction around it. **The UI's job is to make them
legible** — the user should see what the agent did, what it wouldn't do, and why, without
reading a log.

## Priority workflows (user-confirmed)

1. **Web form applications** — the core path: found → tailored → filled → reviewed →
   submitted.
2. **Email applications** — postings that say "send your CV to careers@…": drafted →
   reviewed → sent.

Lower priority for this surface, though the API supports both: scheduled background runs,
and LLM-tailored documents. Build them in, but don't lead with them.

## Terminology (used by the API and the user)

- **Platform** — a job board or ATS the user has connected (greenhouse, lever, linkedin…)
- **Search profile** — saved criteria (titles, location, salary floor, exclusions)
- **Job** — a discovered posting, with a fit score and a hard-filter verdict
- **Master document** — the user's real resume or cover letter, the source of truth
- **Document version** — a tailored variant for one job, verified against the master
- **Application** — a filled form awaiting review, or submitted
- **Deferred field** — a question the agent declined to answer, with the reason
- **Interruption** — a CAPTCHA, MFA prompt or expired session pausing a platform
- **Run** — one pass across connected platforms
- **Clean submissions** — reviewed, confirmed submissions that unlock unattended sending

## Constraints future work must preserve

- **Nothing is sent without an explicit user action.** No "approve all", no bulk submit
  that skips reading. Approving must require having seen what's being approved.
- **Sensitive questions stay unanswered and visibly so.** They are never grouped with
  "fields the agent couldn't map" — they're a different category with a different reason.
- **Unverified documents must be visible at the moment of approval**, not buried.
- **The agent never handles credentials.** Connecting opens a browser; the user signs in.
- **Terms-of-service risk notes** on consumer boards must be shown before automation is
  enabled there.
- Local only: no analytics, no external calls from the UI, no fonts or scripts from a CDN.

## Accessibility

Standard web expectations: keyboard-operable, visible focus, real form semantics,
sufficient contrast in both themes. No specific assistive-tech requirement stated.

## Open decisions

- No brand, logo, or existing visual identity — this is the first UI the project has had.
- Voice is undecided; the API's existing copy is plain, specific and non-marketing, and
  the UI should match it.
