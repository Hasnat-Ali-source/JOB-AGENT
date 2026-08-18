# Running the Job Agent in VS Code

## Start here

```bash
.venv/bin/python -m job_agent dashboard
```

Then open **http://127.0.0.1:8000** — the operator's desk. The raw API is still at
`/docs` if you want it.

The UI is a React app in `frontend/`, built into `job_agent/dashboard/static/` and served
by the same FastAPI process, so there is only ever one command to run. If you pull a fresh
copy and the desk says "not built":

```bash
npm install --prefix frontend && npm run build --prefix frontend
```

For UI work with hot reload, `npm run dev --prefix frontend` serves on :5173 and proxies
`/api` to the Python process on :8000 — run both.

### The six sections

| Section | What it's for |
|---|---|
| **Outgoing tray** | Applications the agent prepared. Open one, answer what it refused, release it. Home screen. |
| **The wire** | Postings it found, ranked by fit. Prepare documents or an email from here. |
| **Dispatch** | Application emails, drafted and held until you send them. |
| **Stations** | Platform connections, health, CAPTCHAs waiting on you, and what each connector can actually do. |
| **The desk** | Your details, your master resume, what you're looking for, and the run button. |
| **Register** | Everything the agent has done, filterable, plus CSV exports. |

### Before your first run

The agent only searches platforms you have connected, so start at **Stations**: press
**Connect** on a platform, sign in in the window that opens, then press **I have signed
in**. The agent checks the page for a way to sign out before it records the connection —
if it can't find one it says so rather than reporting a station that isn't really there.
Until at least one station is on line, a run has nothing to search and the desk says so.

### Which resume is in use

The desk marks one master resume **In use**; every tailored version is built from that
one, and the others sit as **Stored** with a *Use this one* button. Uploading a resume
makes it the one in use, so check the marker after an upload.

Before a run, the desk compares the resume in use against the search profile's job titles
and says so if they have nothing in common. Tailoring reorders and rewords what a resume
already says — it will not add experience the resume doesn't contain, so a customer-support
resume aimed at front-end roles produces honest applications that cannot land.

### How a resume is tailored

The tailored resume is your master, reworded — never a new document. The master is split
into blocks first, and only prose blocks are sent to a model, one at a time, anchored to
their own text. Section headings, contact details, employers, dates, the skills list,
project names, degrees and languages are **copied across untouched**: the model never
gets the chance to alter them.

Each reworded block is then read back against the block it came from, and refused if it
loses half the meaning, drops or invents a figure, drops a qualification, claims work at
the company you are applying to, or adds material the master does not support. A refused
block keeps your own wording. So the worst case is the master itself, and the best case
is the master in the employer's vocabulary — it cannot come back shorter.

Each document's notes say exactly how many passages were reworded and why any were
refused. If they say *no model could reword this resume*, Ollama is not reachable or has
no usable model installed, and only the bullet ordering changed:

```bash
ollama serve
```

A larger model follows the instruction more often than a 3B one — `ollama pull llama3.1:8b`
is worth it if tailoring keeps refusing passages.

### Adding your own stations

The fifteen built-in connectors are a fixed list, and plenty of jobs are on boards that
aren't on it. **Stations → Add a station** takes a company by its address:

| Field | What to put in it |
|---|---|
| Name | Whatever you want to call it — becomes the station's name |
| Company website or board URL | `acme.com` is enough |
| Needs sign-in | Tick only if the listings are behind a login |

**The company's own address is enough.** Paste `acme.com` and the agent follows the
site's careers link to the listings, and on to the Greenhouse, Lever, Ashby or Workday
board behind it if there is one. It then tells you which page the station will actually
read — check that line, because a station pointed at the wrong page is otherwise only
discoverable by running a search and getting nothing back. If it landed somewhere wrong,
remove the station and add it again with the exact listings URL.

An exact board URL is still taken as given: `https://job-boards.greenhouse.io/COMPANY`
works and skips the walk entirely.

Put `{query}` and `{location}` in the URL where the site's own search terms go
(`https://acme.com/careers?q={query}&l={location}`) and each run fills them in from your
search profile. A URL written with placeholders is used exactly as it stands — you have
said which page you mean, so nothing goes looking for another one.

Public boards are searched straight away — nothing to sign into. Some consumer boards
refuse automated requests; if one does, the run says the site refused rather than
reporting no jobs found.

**Use the page a stranger can see.** Your own account area is not a listings page —
`my.greenhouse.io/dashboard` and `app.greenhouse.io` are employer logins with no
postings on them. The test: open the URL in a private window. If you still see a list of
jobs, the agent will too. A run that lands on a page with no postings says so, and names
the page it read.

### Answering the same question fifteen times

Every form asks for your country, your notice period, your work authorisation. Answer
them on one application, leave **"save these answers"** ticked, and they are written onto
every other application waiting in the tray — marked *Your earlier answer* so you can see
where they came from and change any you disagree with. The same thing happens again when
an application is submitted.

Two things are never carried: a question already answered on that application, and an
answer that is not one of the choices the other form offers. A question naming the
employer ("Have you previously worked at GitLab?") only ever matches that same employer's
forms, so a company-specific answer cannot end up on someone else's application.

Answers you saved before this existed are not lost — **Use my saved answers** at the top
of the tray applies the whole backlog in one go.

---

## Setup (once)

1. **Open the folder** in VS Code: `File → Open Folder…` → `JOB-AGENT`.

2. **Select the interpreter.** `Cmd+Shift+P` → *Python: Select Interpreter* →
   `./.venv/bin/python` (3.14.6). `.vscode/settings.json` already points at it, but
   selecting it once makes the test explorer and IntelliSense work.

3. **Install the Python extension** if VS Code doesn't prompt: `ms-python.python` plus
   `ms-python.debugpy`.

Dependencies and the database are already installed on this machine. On a fresh one:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/playwright install chromium firefox
.venv/bin/python -m job_agent init-db
```

`brew install pango cairo gdk-pixbuf libffi` is optional — it enables the nicer PDF
engine. Without it, PDFs still render via ReportLab.

---

## Running it

Press **F5** and pick a configuration, or use the Run and Debug panel:

| Configuration | What it does |
|---|---|
| **Dashboard (API on :8000)** | Starts the API. Open http://127.0.0.1:8000/docs |
| **Test job site (stub, :8001)** | A fake job board to test against — no real employers |
| **Dashboard + test job site** | Both at once (compound) — start here |
| **Run pipeline (search only)** | One search pass across connected platforms |
| **Run pipeline (+ tailored documents)** | Same, plus tailored resumes for good matches |
| **Initialize / migrate database** | Safe to re-run; adds any new columns |
| **Debug the current test file** | Runs whichever test file is open |

From a terminal instead:

```bash
.venv/bin/python -m job_agent dashboard
```

---

## A first walkthrough

Start **Dashboard + test job site**, open http://127.0.0.1:8000/docs, then work down:

1. **`GET /api/v1/connectors`** — see all 15 platforms, what each can do, and the
   terms-of-service notes on the consumer boards.

2. **`POST /api/v1/review/profile`** — your details, used to fill forms:
   ```json
   {"full_name": "Your Name", "email": "you@example.com", "phone": "+1 555 0100",
    "location": "Singapore", "linkedin_url": "https://linkedin.com/in/you"}
   ```

3. **`POST /api/v1/documents/masters`** — upload your real resume (`.docx`/`.pdf`/`.txt`),
   with `doc_type=resume`. Check `GET /api/v1/documents/masters/{id}` to see what it
   parsed.

4. **`POST /api/v1/search/profiles`** — what you're looking for:
   ```json
   {"name": "Backend - Remote", "target_titles": ["Backend Engineer"],
    "remote_pref": "remote", "salary_min": 100000, "exclusions": ["unpaid"]}
   ```

5. **`POST /api/v1/accounts/connect?platform=test_connector`** — opens a browser window at
   the stub site. Sign in there, then **`POST /api/v1/accounts/test_connector/check-status`**
   to record the connection. Real platforms work the same way, and you sign in yourself.
   In the UI this is the **Connect** button on Stations, followed by **I have signed in**.

6. **`POST /api/v1/runs?profile_id=1&generate_documents=true`** — a full pass: search,
   dedup, filter, score, and tailor documents. Returns the run summary.

7. **`GET /api/v1/jobs`** — what it found. **`GET /api/v1/documents/versions`** — what it
   wrote, and whether each passed the fabrication check.

8. **`GET /api/v1/review`** — anything waiting for you, with the questions the agent
   refused to answer and why. **`GET /api/v1/review/{id}`** carries the tailored
   documents' full text alongside the answers, so the resume that will be attached can
   be read without opening a file.

9. **`GET /api/v1/review/{id}/analysis`** — the last read before anything is sent: the
   resume, the letter, the answers and the attachments checked together against the
   posting. `blockers` stop the submission; `warnings` are yours to weigh. A blocker
   marked `overridable` is the one judgement rather than a defect — that the resume
   evidences too little of what the posting asks for — and
   `POST /api/v1/review/{id}/submit?accept_weak_fit=true` sends anyway, recording that
   you chose to.

   The floors live in `.env`: `ANALYST_MIN_ATS_SCORE`, `ANALYST_MIN_FIT_SCORE`, and
   `ANALYST_ENABLED=false` to turn the whole read off.

10. **`GET /api/v1/audit`** — everything it did. **`GET /api/v1/exports/applications`** —
    the CSV.

Nothing in that sequence submits an application or sends an email. Both require an
explicit approval step you have to call yourself.

### Testing against a real company, safely

Point a platform at a real board and run search only — no applications:

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/settings/platforms/greenhouse" \
  -H "Content-Type: application/json" \
  -d '{"search_url": "https://job-boards.greenhouse.io/SOME_COMPANY"}'
```

Then `POST /api/v1/runs`. Reading public job boards is what any browser does; the agent
stays read-only unless you explicitly go further.

---

## Tests

- **Test explorer:** the flask icon in the sidebar — tests are auto-discovered.
- **Terminal:** `Cmd+Shift+P` → *Tasks: Run Test Task* (the fast suite, ~100s, 722 tests).
- **One file:** open it and press F5 with *Debug the current test file*.

```bash
.venv/bin/python -m pytest tests/ -q --ignore=tests/test_phase4_ollama_live.py
```

The ignored file needs a running Ollama; it skips itself if one isn't there, but excluding
it keeps the suite fast.

---

## Optional: better tailoring

Ollama is installed and `llama3.2` is pulled. With `ollama serve` running, documents get
genuinely reworded rather than reordered. Without it, the deterministic path reorders your
existing bullets — which cannot invent anything, so it's the safe default.

A larger model follows document instructions noticeably better:

```bash
ollama pull llama3.1:8b
```

Every tailored resume is checked twice before it can be used: against the master for
invented names, numbers, emails and links, and for credentials the master mentions but the
rewrite dropped. A rewrite that fails either check falls back to the deterministic path
rather than being attached to an application. The PDF is laid out for machine reading —
standard section headings, no columns or tables, and the name on its own line so a resume
parser reads it as a name.

---

## If something misbehaves

- **Port 8000 in use** — `.venv/bin/python -m job_agent dashboard --port 8010`.
- **"No module named job_agent"** — the interpreter isn't the venv; re-select it, and make
  sure the terminal's working directory is the project root.
- **A browser window doesn't appear** — `HEADLESS=false` is the default and required for
  sign-in; check `~/Library/Logs/job-agent/` for scheduled runs.
- **PDF generation looks wrong** — try `PDF_ENGINE=reportlab` in `.env`.
- **A platform is stuck** — `GET /api/v1/health` says why, and
  `POST /api/v1/platforms/{platform}/reconnect` reopens the browser to sign in.
- **A run finds no jobs** — check Stations first: with nothing connected, a run searches
  nothing, and the desk says so above the run button. If postings did come in, The wire
  reports how many the filters rejected — a search profile combining a remote preference
  with a narrow location is the usual cause.
- **"I have signed in" says you haven't** — the check looks for a way to sign *out* on the
  page. If the window is showing a different page by then, navigate back to the signed-in
  view and press it again.
- **A station you added finds nothing** — open its URL in a normal browser. If the jobs
  are only there after clicking through a search, use that results URL as the station's
  URL instead of the site's front page.

Logs go to the VS Code terminal for a foreground run, and to
`~/Library/Logs/job-agent/scheduled.{out,err}.log` for scheduled ones.
