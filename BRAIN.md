# Brain — pick up here

Working notes for continuing this project in a fresh session. Not a summary of
what was built; the code and commit messages carry that. This is the things
that are **not obvious from reading the code**, the traps that cost hours, and
the state of what is actually verified against reality.

Branch: `fix-tailoring-answers-stations-wire` (8 commits ahead of `main`).
1020 tests pass. Nothing is merged.

---

## Read this first: there are three separate browsers

Most of the confusion in this project has come from this one fact.

| Browser | Signed in? | What it is |
|---|---|---|
| The user's everyday Chrome | Yes | Where they check the site by hand |
| The Claude/agent-tooling browser pane | No | What an assistant drives to look at pages |
| **The agent's own Playwright profile** | **Yes** | `~/Library/Application Support/job-agent/profiles/<platform>/` |

Signing into a site in one does **nothing** for the others. "I'm already signed
in" nearly always means Chrome, while the failing thing is the agent's profile
— or the reverse, as happened here: the agent's profile *was* signed in
(52 SimplyHired/Indeed cookies) while both other browsers were not, and the
conclusion drawn from looking at the wrong one was wrong.

**Check it properly**, don't infer:

```bash
PYTHONPATH=. .venv/bin/python -c "
import asyncio
from job_agent.core.session_manager import get_session_manager
async def m():
    sm = await get_session_manager()
    p = await sm.get_page('simply_hired', needs_signin=False)
    await p.goto('https://www.simplyhired.com/')
    await p.wait_for_timeout(3000)
    c = await sm.contexts['simply_hired'].cookies()
    print('cookies:', len([x for x in c if 'indeed' in x.get('domain','') or 'simplyhired' in x.get('domain','')]))
asyncio.run(m())"
```

The dashboard holds the profile open. **Kill it first** or you get an EPIPE
crash: `lsof -ti:8000 | xargs kill; pkill -f "job-agent/profiles"`.

---

## The single most important design fact

**Fit is decided by which jobs get searched for, not by how well a document is
written afterwards.**

A user complaining that the fit analyser is broken is usually looking at a wire
full of jobs their resume cannot answer. On this install the wire held 59
postings and **zero** shared a job title with the active resume — every score
was 0-7%, and the analyser was right.

So the resume drives the search (`resume_profile.py` → `resume_sync.py`), and
the wire is filtered to `Job.matched_master_id`. Before debugging a low score,
check whether the postings match the resume's titles at all.

`POST /api/v1/documents/masters/resync` re-derives and re-runs.

---

## Boundaries that must not be relaxed

The user has asked, explicitly and more than once, for the agent to **add
skills the resume does not have** so every job scores 90-100. That was declined
and should stay declined. These documents go to real employers under the user's
name and from their email; a resume claiming Ruby they have never written is a
false statement they carry, not a config option.

Do not weaken any of these to raise a number:

- `anchored_rewrite.accept_rewrite` — per-block checks
- `fit_rewrite._summary_is_honest` — summary reframing checks
- `fabrication_check` — document-level net
- `can_submit_automatically = False` — the agent never presses submit

The honest levers, all built: resume-driven matching (by far the biggest),
summary reframing, employer vocabulary, evidence reordering. `fit_rewrite`
names blocking technologies **specifically so the real advice — a closer
posting — stays visible**.

If asked again: say it in a sentence, point at those levers, move on. Don't
re-litigate.

---

## Architecture decisions worth knowing

### Resumes are rewritten block-by-block, never whole-document

`anchored_rewrite.py`. Asked to tailor a whole resume in one turn, llama3.2
returned the *job posting's own responsibilities* in past tense as the
candidate's career — every real employer gone, and it looked like a finished
resume. `verify_no_fabrication` can't catch that, because posting text is
legitimately quotable, so copying the posting is "supported" by construction.

Headings, contact lines, employers, dates, skills, project names, degrees and
languages are copied **verbatim**. Only prose blocks go to a model, one at a
time, anchored to their own source. Worst case output = the master.

New safety checks belong in `accept_rewrite` (per block), **not** in
`TailoringService._is_usable` (per document) — the document-level completeness
check misreads genuine rewording as deletion, which is why `_is_usable` takes
`anchored=True`.

### Fit thresholds were measured, not guessed

`semantic_match.py`. Local embeddings (`nomic-embed-text` via Ollama). Three
findings, each the result of being wrong first:

1. **Absolute similarity is worthless alone** — any two pieces of professional
   English score ~0.5. A requirement must also stand out from *that resume's
   own baseline* (`margin`).
2. **Evidence must be whole sentences.** PDF resumes hard-wrap; a fragment is
   half a thought, which is close to everything.
3. **Embeddings cannot verify a named technology.** It matched "Deep experience
   with Ruby and Rails" to "Comfortable working independently" on shared
   phrasing. Named technologies absent from the resume block outright and count
   double.

`tests/test_semantic_match.py` holds the labelled pairs. If a model change
moves the distribution, that test fails rather than scores quietly becoming
flattery. **Keep it that way.**

### Each station decides how to apply

`ApplyStrategy` on `PlatformAccount`:

- `tailored` — write documents, attach, fill, hold for review
- `platform_profile` — the board holds the documents (Indeed SmartApply,
  LinkedIn Easy Apply); applying is driving its flow, and tailoring is wasted

Guessed from the address on add; a dropdown on the station card. This was the
fix for "the workflow limits my agent" — the pipeline used to assume one shape.

---

## Verified working (against live sites, not fixtures)

- **SimplyHired search** — 20 postings/page with full details. The blocker was
  the *user agent*: Playwright's default contains "HeadlessChrome" and
  Cloudflare 403s it. A normal desktop UA gets 200.
- **Careers discovery** — `acme.com` → the listings page. Live on gitlab,
  vercel, linear, ramp, posthog.
- **Multi-step form walking** — `form_walker.py`, on a real 3-step form:
  Next → Save and continue → stopped at Submit, correctly avoiding Back.
- **Apply route** — posting → "Quick Apply" → Indeed SmartApply step 1
  (33%, address fields + Continue). 2 of 5 postings.
- **Resume-driven pipeline** — search terms re-derived, wire refilled.
- **Answer carry** — 36 answers onto 3 applications from 56 saved.
- **Fit** — 100% on a genuinely fitting posting; 43% naming aws/kubernetes/
  postgresql; 12% for VP of Data. Sensible spread.

---

## NOT working — the honest list

### 1. No application has ever been completed end to end

The blocker is **not the agent**. Indeed SmartApply's first step asks for a US
address (street / city / postal code) and validates it against the user's
**Malaysian** Indeed profile. The filler fills all three; the form refuses to
advance; the walker correctly reports "the form did not move on" and hands
over rather than retrying.

**This is user data, not code.** Fixing the address on the Indeed profile the
agent's browser is signed into is the unblock. Everything downstream
(33% → 66% → 100% → Submit) is built and unit-tested but has never had a form
let it through.

The questions the user most wants tested ("that's the real deal") are at steps
2-3, behind this gate. **They have never been reached.**

### 2. Apply route reaches only 2 of 5 postings

The other three land somewhere `ApplyRouteFinder` doesn't recognise. Not yet
diagnosed — the next thing to chase. Debug with
`scratchpad/walk5.py`-style scripts: print `page.url` and the visible controls
after each hop.

### 3. No recency sort

The user asked for "20 most recent jobs a day". The daily cap exists
(`daily_search_limit`) but postings come back in the board's **relevance**
order. Sorting by date is not implemented.

### 4. `remote_com` station is `session_expired`

Runs skip it and say so.

---

## Traps that cost real time

- **`evaluate` throws mid-navigation** ("execution context was destroyed").
  That is the *normal* state of an apply flow bouncing through redirects.
  Catching it and giving up made every hop land on nothing. Catch and **keep
  polling**.
- **Stability is not arrival.** SmartApply is stable for seconds on a
  "Preparing review" screen carrying only Exit; the Submit appears at ~18s.
  Wait *goal-directed* — poll for what you're after.
- **Client-rendered controls need a minimum settle.** "Quick Apply" doesn't
  exist on first paint; reading immediately finds a posting with no way to
  apply.
- **Tracking redirects must be clicked, not navigated.** `/out?r=…` bounces to
  the homepage when opened directly. And it may open a **new tab** — click
  inside `context.expect_page()`.
- **Greenhouse embeds an invisible reCAPTCHA on every form.** Its badge is a
  *visible* 256×60 iframe. Presence is not a challenge: `/anchor` is the badge,
  `/bframe` is the real one.
- **"Any input exists" is not an application form.** Every board's header has a
  search box — that mistake queued an application whose one filled field was
  "City, State, ZIP or Remote" set to "Malaysia". Look for *applicant* fields.
- **Migrations run at engine open** (`models/migrations.py`), not only from
  `init_db.py`. Add a column there or an existing install breaks at runtime.
- **Don't smuggle data into `filled_fields`.** Half a dozen places iterate it
  as form fields. `form_walk` has its own column for exactly this reason.

---

## Current state of the user's data

```
stations:  greenhouse   CONNECTED  paused   tailored
           remote_com   CONNECTED  paused   tailored   (session expired)
           simply_hired CONNECTED  active   platform_profile
masters:   2 Hasnat_Ali_CSR_Resume        (in use)
           3 Hasnat_Ali_Cover_Letter_CSR  (in use)
           1 Hasnat_Tahir_CV, 4 Hasnat_resume_detailed  (stored)
search:    "customer support", remote
```

---

## Running it

```bash
.venv/bin/python -m job_agent dashboard      # the only command; :8000
npm run build --prefix frontend              # after UI source changes
.venv/bin/python -m pytest tests/ -q         # ~6 min, 1020 tests
```

There is **no `package.json` at the repo root** — the frontend is in
`frontend/`. `npm run dev` from the root fails; it's `--prefix frontend`, and
you rarely want it since the UI is built into the FastAPI static dir.

Optional local models: `ollama serve`, then `ollama pull nomic-embed-text`
(semantic fit) and a chat model (tailoring). Both degrade gracefully.

---

## First things to do next session

1. **Get the Indeed profile address fixed**, then re-run the apply walk. That
   single change is what unblocks every remaining claim.
2. **Diagnose the 3 of 5 postings** the route finder misses.
3. **Run `/code-review ultra`** on this branch before merging. It's 8 commits
   and several thousand lines written across many sessions, largely
   self-reviewed — exactly the case where a second pass earns its keep.
4. Then merge.
