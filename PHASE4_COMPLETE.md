# Phase 4 Complete: Document Generation

**Status:** ✅ Complete
**Date:** 2026-08-15
**Tests:** 84 Phase 4 tests; 204 passing in the hermetic suite, plus 5 opt-in live-Ollama tests
**Verified on:** Python 3.14.6, WeasyPrint 69 + ReportLab 5.0, python-docx 1.2, pypdf 6.16,
Ollama `llama3.2:3b`

---

## What Phase 4 Delivers

The agent can take the user's real resume, produce a job-specific variant, prove that
variant doesn't claim anything the original didn't, render it to PDF, and hand it back
for review.

```
upload ──▶ DocumentParser ──▶ MasterDocument        (.docx / .pdf / .txt / .md → text + sections)
                                    │
job ────▶ TailoringService ─────────┤               (Ollama → Claude → deterministic)
                                    ▼
                            FabricationCheck        (does the master support every claim?)
                                    ▼
                             PdfRenderer            (ReportLab → PDF)
                                    ▼
                           DocumentVersion          (text, pdf_path, notes, flags)
```

---

## The Safety Property

A resume tailor that invents experience is not a bug — it is a tool for committing
fraud in the user's name, from their email address, against a real employer. Asked to
"tailor this resume to the job", a model will cheerfully close a gap by adding a
technology the candidate has never used or inventing a metric that sounds plausible.

Three layers guard against that:

1. **The prompt** states the rule in both the system turn and the user turn:
   reorder, drop, and rephrase — never add. Stated once, a model drifts from it over a
   long document.
2. **`FabricationCheck` verifies the output** rather than trusting it. Every generated
   variant — including the deterministic one — is compared against its master. It
   reports numbers, named entities, emails, and links that appear in the variant but
   not in the master. Terms from the job posting are permitted, so a cover letter may
   name the company it addresses.
3. **The deterministic path cannot fabricate at all**, by construction: it only reorders
   lines that already exist in the master.

Flags never block generation — they attach to the record. `DocumentVersion.is_verified`
is false, the API returns `review_required: true`, and an audit entry is written with
`result="paused"`. The user sees exactly what the model added and decides.

```python
verify_no_fabrication(master, "- Reduced infra costs by 60% at Globex Industries")
# ["Name 'Globex Industries' does not appear in the master document",
#  "Number '60%' does not appear in the master document"]
```

This is a safety net, not a proof. It catches added facts of the kinds that are
checkable in text; it cannot detect a subtly overstated responsibility.

---

## Components

### 1. Parsing — `services/document_parser.py`

Accepts `.docx` (paragraphs *and* table cells — many resume templates lay content out in
tables), `.pdf` (pypdf), `.txt`, and `.md`. Produces normalized text plus detected
sections.

Section detection is heuristic because resumes have no standard format: short,
punctuation-free lines are matched against ~30 heading spellings ("Work Experience",
"Professional Experience", "Employment", …). Content before the first heading becomes
`header` — on a resume that's the name and contact block.

A scanned PDF with no text layer raises a clear error naming OCR as the cause, rather
than storing an empty master.

### 2. Rendering — `services/pdf_renderer.py`, `services/document_layout.py`

**Two engines, one layout.** `document_layout.build_blocks()` turns document text into
a structured block list (name, contact, heading, paragraph, bullets); both engines
render from those blocks, so output is consistent whichever runs.

| Engine | Chosen when | Why |
|---|---|---|
| **WeasyPrint** | Preferred; needs pango/cairo | HTML/CSS layout — letter-spaced section rules, widow/orphan control, print margins |
| **ReportLab** | Anywhere WeasyPrint can't load | Pure Python, no native dependencies |

`weasyprint_available()` performs one real (cached) render to decide, because importing
WeasyPrint succeeds in cases where rendering still fails in the native loader.
`settings.pdf_engine` pins a choice; "auto" prefers WeasyPrint and degrades quietly. If a
render fails mid-way, the renderer retries with ReportLab rather than losing the document.

Install the system libraries with:

```bash
brew install pango cairo gdk-pixbuf libffi
```

Layout is deliberately single-column with standard fonts in both engines: ATS parsers
handle that far better than multi-column designs, and the user's original formatting is
preserved in the untouched master upload regardless.

Text is escaped before rendering — a resume mentioning `C++ & <framework>` would
otherwise be swallowed as markup (WeasyPrint) or crash the paragraph parser (ReportLab).

### 3. Tailoring — `services/tailoring.py`

Three generators, tried in order:

| Generator | When | Notes |
|---|---|---|
| `llm:ollama:<model>` | `use_ollama` and Ollama reachable | Local — nothing leaves the machine |
| `llm:anthropic:<model>` | `use_anthropic` and a key is set | Defaults to `claude-sonnet-5`; sends data off-machine |
| `deterministic` | Always available | Reorders existing content only |

**Model resolution.** The configured model is preferred, but a fresh Ollama install
rarely has exactly that one pulled, so any installed completion model is used instead of
failing. Models tagged `:cloud` are skipped unless `ollama_allow_cloud_models=true`:
they execute on Ollama's servers, which would send the user's resume and the job posting
off the machine. Local execution is the whole reason this project prefers Ollama, so
that can't happen by accident. Embedding-only models are never selected.

**Output is validated before it's accepted.** `_is_usable()` rejects model output that
is too short, still contains prompt scaffolding, reproduces the job description
verbatim, or has lost the candidate's name — and falls through to the deterministic
path. This is not hypothetical: `llama3.2:3b` initially replayed the entire prompt,
job description included, and that would have been rendered into a PDF and attached to
an application. The prompt now ends with the instruction (small models continue a long
prompt rather than acting on it), `_clean()` strips echoed scaffolding, and `_is_usable()`
catches whatever survives.

The deterministic path is not a stub. For a resume it re-ranks bullet points within each
section by overlap with job keywords, preserving section order and dropping nothing. For
a cover letter it assembles the letter from the master's own sentences, with only the
greeting and closing as boilerplate. It runs whenever no LLM is reachable or usable — so
generation never fails just because `ollama serve` isn't running.

### 4. Orchestration — `services/document_builder.py`

`upload_master()` parses, stores, and **copies the original into managed storage**, so
the record stays valid after the user moves or deletes the file they uploaded. Uploading
a new master of a type deactivates the previous one; resumes and cover letters are
tracked independently.

`build_variant()` generates → verifies → renders → stores. If PDF rendering fails, the
tailored text is still saved with the failure recorded in the notes, rather than losing
the work.

Storage layout under `~/Library/Application Support/job-agent/documents/`:

```
masters/<doc_type>/<id>_<name>.<ext>       original upload, never modified
versions/<job_id>/<doc_type>_v<id>.pdf     generated variants
```

### 5. API — `dashboard/routes/documents.py`

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/v1/documents/masters` | Upload a master (multipart) |
| GET | `/api/v1/documents/masters` | List masters |
| GET | `/api/v1/documents/masters/{id}` | Master detail with parsed text |
| POST | `/api/v1/documents/masters/{id}/activate` | Switch the active master |
| POST | `/api/v1/documents/generate` | Generate `resume`, `cover_letter`, or `package` |
| GET | `/api/v1/documents/versions` | List variants (`verified_only` filter) |
| GET | `/api/v1/documents/versions/{id}` | Variant text, notes, flags |
| GET | `/api/v1/documents/versions/{id}/pdf` | Download the rendered PDF |

---

## Schema Additions

- **`master_documents`** — the user's uploaded originals: type, name, source path and
  format, extracted text, detected sections, active flag.
- **`document_versions`** — generated variants: master and job links, tailored text,
  PDF path, `generator`, `tailoring_notes`, `fabrication_flags`.
- **`applications.resume_version_id` / `cover_letter_version_id`** — link the existing
  path fields to the version records behind them. Added to the additive-column
  migration in `scripts/init_db.py`, so existing databases pick them up.

---

## Acceptance Criteria

| Criterion | Status | Evidence |
|-----------|--------|----------|
| User uploads a master resume (DOCX or PDF); it's parsed and stored | ✅ | `test_parses_docx`, `test_parses_pdf`, `test_upload_stores_and_parses` |
| Given a job posting, a tailored resume PDF variant is generated | ✅ | `test_generates_a_linked_pdf`, `test_pdf_is_readable_and_contains_the_content` |
| PDF is linked in the record; user can download / review before submit | ✅ | `test_pdf_downloads_as_a_real_pdf` |
| Tailored content never asserts what the master doesn't support | ✅ | `TestFabricationCheck` (8), `test_llm_output_is_verified_not_trusted` |
| Generation works with no LLM installed | ✅ | `test_unreachable_ollama_falls_back` |

Verified end-to-end over HTTP with a real `.docx` resume: uploaded (5 sections
detected), tailored for a streaming-data role, verified clean, rendered to a 2.3 KB PDF,
downloaded as `application/pdf`. The Snowflake cost bullet was promoted above the Kafka
bullet to match the posting's emphasis, and the on-call bullet sank to last.

---

## Test Coverage

### `tests/test_phase4_documents.py` — 48 tests

Parsing (9: txt/docx/pdf, section detection, heading rejection, unsupported format,
empty and missing files), rendering (5: real PDF bytes, directory creation, markup
escaping, extraction round-trip), fabrication check (8: faithful rewrites pass; invented
metrics, employers, degrees and altered contact details are flagged; job terms and
letter boilerplate are allowed), tailoring (10), and the builder (16).

### `tests/test_phase4_documents_api.py` — 18 tests

Upload validation and errors, master activation, generation for resume / cover letter /
package, version listing and detail, PDF download, and the missing-file case.

### `tests/test_phase4_ollama_live.py` — 5 tests (opt-in)

Runs against a real local model: model resolution, the cloud-model exclusion, usable
output, verification, and PDF rendering. Skips automatically when Ollama isn't running.

Every other test forces the deterministic path via `tests/conftest.py`. Without that,
any test touching document generation depends on whether `ollama serve` happens to be
up — the same test then takes minutes instead of milliseconds and can fail because a 3B
model phrased something unexpectedly.

```bash
.venv/bin/python -m pytest tests/ -v
```

---

## Known Limitations

- **The fabrication check is textual, not semantic.** It catches invented numbers,
  organizations, and contact details. It cannot catch "led a team" where the master says
  "worked on a team" — an overstated responsibility using words already present. Read
  what you send.
- **Small models are unreliable at this task.** `llama3.2:3b` (the model installed here)
  needs the prompt hardening and output validation described above to produce a usable
  resume at all, and its rewriting is conservative. An 8B-class model
  (`ollama pull llama3.1:8b`) follows document instructions considerably better. The
  guards mean a weak model degrades to deterministic reordering rather than producing
  something unusable — but that is a floor, not a substitute for a capable model.
- **Section detection is heuristic.** A resume using unusual headings ("Where I've
  Worked") gets one `header` section. Content is preserved either way; only bullet
  re-ranking is affected.
- **PDF output is plain single-column.** Deliberate for ATS parsing, but it does not
  reproduce a designed resume's typography. The original upload is retained unmodified
  for cases where the user wants to submit their own formatting.
- **The two engines are visually close but not identical.** WeasyPrint gives finer
  control over letter-spacing and page breaks. Both were rendered and inspected as
  images during development; content is identical and asserted equal by test.
- **Scanned PDFs are rejected**, not OCR'd. The error says so explicitly.
- **Cover letter tone is not configurable yet** — the deterministic letter uses a fixed
  structure.

---

## Next: Phase 5 — Application Filling & Review Gate

`ApplicationSession` state tracking, `fill_application()` for known fields, unknown and
sensitive field detection (compensation history, demographics), screenshot capture, and
the Review Queue. Phase 4's `DocumentVersion` records become the attachments; a variant
with `is_verified == false` should never reach an employer without the user reading it.
