/**
 * The Outgoing Tray — applications the agent prepared, awaiting release.
 *
 * This is the home screen because it is where the product's promise is kept:
 * the agent drafted a message that will go out under the user's name, and the
 * user is the only thing that releases it.
 *
 * Two rules shape this view:
 *
 * 1. **No bulk approve.** Releasing requires opening the blank and seeing what
 *    it says. A button that approves five things at once is a button that
 *    approves five things nobody read.
 * 2. **Refused and unmapped questions are visually separate.** One is a
 *    boundary the agent holds on purpose; the other is a gap in what it knew.
 *    Collapsing them into "issues" loses the distinction that matters most.
 */

import { useState } from "react";
import { api, pdfUrl, screenshotUrl } from "../api.js";
import {
  Empty,
  FieldRow,
  Loading,
  Notice,
  QuerySlip,
  SectionHead,
  Stamp,
  stamp,
  useAsync,
} from "../components.jsx";

export default function OutgoingTray({ toast, onNavigate }) {
  const queue = useAsync(() => api.queue("queued_for_review"));
  const [openId, setOpenId] = useState(null);

  if (queue.loading) return <Loading label="Reading the outgoing tray" />;

  if (queue.error) {
    return <Notice tone="red" title="Could not read the tray">{queue.error.message}</Notice>;
  }

  const items = queue.data?.applications || [];

  if (!items.length) {
    return (
      <>
        <SectionHead
          title="Outgoing tray"
          hint="Applications the agent has prepared. Nothing leaves here without you."
        />
        <Empty
          mark="Tray empty"
          title="Nothing is waiting for you"
          action={
            <>
              <button className="btn" onClick={() => onNavigate("wire")}>
                See jobs found
              </button>
              <button className="btn btn--quiet" onClick={() => onNavigate("desk")}>
                Run a search
              </button>
            </>
          }
        >
          When the agent fills an application, it stops here so you can read it
          before anything is sent. Start a run to find jobs and prepare
          applications.
        </Empty>
      </>
    );
  }

  const needingAnswers = items.filter((item) => item.required_unanswered.length > 0);
  const ready = items.filter((item) => item.required_unanswered.length === 0);

  // Answers are carried onto the other blanks as soon as one is saved. Blanks
  // prepared before that started still ask questions the user has answered
  // elsewhere, and this is the one action that clears them.
  const applySaved = async () => {
    try {
      const result = await api.applySavedAnswers();
      toast(result.message, result.answers_filled ? "olive" : "amber");
      queue.reload();
    } catch (error) {
      toast(error.message, "red");
    }
  };

  return (
    <>
      {/* Applications built from a resume no longer in use are hidden, not
          lost. Saying so is the point: after switching resume the tray would
          otherwise look as though work had disappeared. */}
      {queue.data?.hidden_from_earlier_resumes ? (
        <Notice tone="amber" title="Not shown here">
          {queue.data.hidden_from_earlier_resumes} application
          {queue.data.hidden_from_earlier_resumes === 1 ? " was" : "s were"} prepared
          from an earlier resume and {queue.data.hidden_from_earlier_resumes === 1 ? "is" : "are"}{" "}
          hidden. Releasing one would send a document written from a resume you have
          moved away from. Prepare those postings again to use{" "}
          {queue.data.resume_in_use || "the resume in use"}.
        </Notice>
      ) : null}

      <SectionHead
        title="Outgoing tray"
        hint={`${items.length} prepared · ${needingAnswers.length} need you · ${ready.length} ready to release`}
      >
        {needingAnswers.length > 0 ? (
          <button
            className="btn btn--sm"
            onClick={applySaved}
            title="Fill every blank question these forms have already been answered elsewhere"
          >
            Use my saved answers
          </button>
        ) : null}
      </SectionHead>

      <div className="stack">
        {needingAnswers.map((item) => (
          <Blank
            key={item.id}
            summary={item}
            open={openId === item.id}
            onToggle={() => setOpenId(openId === item.id ? null : item.id)}
            onChanged={queue.reload}
            toast={toast}
          />
        ))}
        {ready.map((item) => (
          <Blank
            key={item.id}
            summary={item}
            open={openId === item.id}
            onToggle={() => setOpenId(openId === item.id ? null : item.id)}
            onChanged={queue.reload}
            toast={toast}
          />
        ))}
      </div>
    </>
  );
}

/* ==========================================================================
   One message blank
   ========================================================================== */

function Blank({ summary, open, onToggle, onChanged, toast }) {
  const outstanding = summary.required_unanswered.length;

  return (
    <article className="blank blank--feed">
      <header className="blank__head">
        <div className="blank__to">
          <span className="label">To</span>
          <h3 className="blank__title">{summary.company || "Unknown company"}</h3>
          <span className="record" style={{ fontSize: 12.5, color: "var(--ink-2)" }}>
            {summary.job_title}
          </span>
        </div>

        <div className="row" style={{ gap: "0.4rem" }}>
          {outstanding > 0 ? (
            <Stamp label={`${outstanding} query`} tone="red" struck />
          ) : (
            <Stamp label="Ready" tone="olive" struck />
          )}
        </div>
      </header>

      <div className="blank__body">
        <div className="row" style={{ gap: "1.5rem", fontSize: 12 }}>
          <span className="record muted">
            <span className="label" style={{ display: "inline" }}>Filled </span>
            {summary.filled_count} fields
            {summary.form_steps > 1 ? ` across ${summary.form_steps} steps` : ""}
          </span>
          <span className="record muted">
            <span className="label" style={{ display: "inline" }}>Deferred </span>
            {summary.deferred_count}
          </span>
          <span className="record muted">
            <span className="label" style={{ display: "inline" }}>Prepared </span>
            {stamp(summary.filled_at)}
          </span>
        </div>

        {open ? <BlankDetail id={summary.id} onChanged={onChanged} toast={toast} /> : null}
      </div>

      <footer className="blank__foot">
        <button className="btn btn--sm" onClick={onToggle} aria-expanded={open}>
          {open ? "Close blank" : "Open blank"}
        </button>
        <span className="spacer" />
        {!open ? (
          <span className="label">
            {outstanding > 0
              ? `${outstanding} question${outstanding === 1 ? "" : "s"} to answer`
              : "Open to read before releasing"}
          </span>
        ) : null}
      </footer>
    </article>
  );
}

/* ==========================================================================
   The opened blank: what was filled, what wasn't, and the release lever
   ========================================================================== */

function BlankDetail({ id, onChanged, toast }) {
  const detail = useAsync(() => api.review(id), [id]);
  const [answers, setAnswers] = useState({});
  const [busy, setBusy] = useState(false);
  // Off by default: demographic answers are never kept as a side effect of
  // saving a form. Keeping them is a choice the user makes deliberately.
  const [keepSensitive, setKeepSensitive] = useState(false);
  const [notes, setNotes] = useState("");

  if (detail.loading) return <Loading rows={1} label="Reading the blank" />;
  if (detail.error) {
    return <Notice tone="red" title="Could not read this application">{detail.error.message}</Notice>;
  }

  const data = detail.data;
  const sensitive = Object.entries(data.sensitive_questions || {});
  const other = Object.entries(data.other_questions || {});
  const filled = Object.entries(data.filled_fields || {});
  const unverified = data.unverified_documents || [];
  const analysis = data.analysis || null;
  const blockers = analysis?.blockers || [];
  // A judgement about odds is the user's to overrule; a defect is not.
  const defects = blockers.filter((finding) => !finding.overridable);
  const judgements = blockers.filter((finding) => finding.overridable);

  const outstanding = [...sensitive, ...other].filter(
    ([question, field]) =>
      field.required &&
      (field.value_entered_by_user ?? "") === "" &&
      (answers[question] ?? "") === "",
  );

  const saveAnswers = async () => {
    const pending = Object.fromEntries(
      Object.entries(answers).filter(([, value]) => value !== ""),
    );
    if (!Object.keys(pending).length) return true;

    try {
      const result = await api.answer(id, pending, true, keepSensitive);
      setAnswers({});
      detail.reload();

      // The same fifteen questions are on every form in the tray. When an
      // answer lands on the others too, say so — silently filling them looks
      // identical to not having saved anything.
      const carried = result?.carried_to_other_applications;
      if (carried?.message) {
        toast(carried.message, "olive");
        onChanged();
      }

      return true;
    } catch (error) {
      toast(error.message, "red");
      return false;
    }
  };

  const act = async (fn, successMessage, tone = "olive") => {
    setBusy(true);
    try {
      const result = await fn();
      toast(successMessage(result), tone);
      onChanged();
      detail.reload();
    } catch (error) {
      toast(error.message, "red");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stack" style={{ marginTop: "1rem" }}>
      {unverified.length > 0 ? (
        <Notice tone="red" title="A document contains claims your resume does not support">
          {unverified.map((doc) => (
            <div key={doc.version_id} style={{ marginTop: "0.35rem" }}>
              <strong style={{ display: "inline" }}>{doc.kind.replace("_", " ")}:</strong>{" "}
              {doc.fabrication_flags.join("; ")}{" "}
              <a href={pdfUrl(doc.version_id)} target="_blank" rel="noreferrer">
                Read it
              </a>
            </div>
          ))}
          <div style={{ marginTop: "0.4rem" }}>
            This cannot be submitted until you regenerate or replace it.
          </div>
        </Notice>
      ) : null}

      {/* The last read before this reaches an employer */}
      {analysis ? (
        <AnalysisPanel
          analysis={analysis}
          busy={busy}
          onRegenerate={() =>
            act(
              () => api.regenerateDocuments(id),
              (result) =>
                result.analysis.ready
                  ? "New documents attached — read them and approve again"
                  : "New documents attached, and the analyst still has objections",
            )
          }
          onFit={() =>
            act(
              () => api.fitDocumentsToJob(id),
              // The builder records what the rewrite was worth — "Fit 54% →
              // 91%" — and that is the whole point of pressing this rather
              // than plain regeneration.
              (result) =>
                result.tailoring_notes?.[0] ||
                "Documents rewritten for this posting",
            )
          }
        />
      ) : null}

      {/* The documents themselves, readable here rather than in another tab */}
      <Documents documents={data.documents || []} />

      {/* What the agent typed in */}
      <section>
        <span className="label">Filled by the agent</span>
        <div className="fields">
          {filled.length ? (
            filled.map(([question, field]) => (
              <FieldRow key={question} label={question} value={field.value} />
            ))
          ) : (
            <FieldRow label="Nothing" value="No fields were filled" />
          )}
        </div>
      </section>

      {/* Refusals — the product's boundary, kept visually distinct */}
      {sensitive.length > 0 ? (
        <section>
          <span className="label" style={{ color: "var(--red)" }}>
            Questions the agent refuses to answer ({sensitive.length})
          </span>
          <p className="muted" style={{ margin: "0.25rem 0 0.6rem", fontSize: 13 }}>
            Demographic, disability, veteran and compensation questions are yours alone.
            Leaving them blank is a valid answer where the form allows it.
          </p>

          <label
            className="row"
            style={{ gap: "0.4rem", margin: "0 0 0.7rem", fontSize: 13 }}
          >
            <input
              type="checkbox"
              checked={keepSensitive}
              disabled={busy}
              onChange={(event) => setKeepSensitive(event.target.checked)}
            />
            <span>
              Save these answers and reuse them on future applications — they stay on
              this machine, and the agent still never invents one.
            </span>
          </label>
          <div className="stack stack--tight">
            {sensitive.map(([question, field]) => (
              <QuerySlip
                key={question}
                question={question}
                detail={field}
                disabled={busy}
                value={
                  answers[question]
                  ?? field.value_entered_by_user
                  ?? field.suggested_answer
                  ?? ""
                }
                onChange={(value) => setAnswers({ ...answers, [question]: value })}
              />
            ))}
          </div>
        </section>
      ) : null}

      {/* Gaps — a different category with a different reason */}
      {other.length > 0 ? (
        <section>
          <span className="label" style={{ color: "var(--amber)" }}>
            Questions the agent could not map ({other.length})
          </span>
          <div className="stack stack--tight" style={{ marginTop: "0.6rem" }}>
            {other.map(([question, field]) => (
              <QuerySlip
                key={question}
                question={question}
                detail={field}
                disabled={busy}
                value={
                  answers[question]
                  ?? field.value_entered_by_user
                  ?? field.suggested_answer
                  ?? ""
                }
                onChange={(value) => setAnswers({ ...answers, [question]: value })}
              />
            ))}
          </div>
        </section>
      ) : null}

      {/* How much of a multi-step form the agent actually got through.
          "Filled 16 fields" reads the same whether that was the whole
          application or the first screen of five. */}
      {data.walk && data.walk.step_count > 1 ? (
        <details className="fold">
          <summary>{data.walk.message}</summary>
          <div className="fold__body">
            <table className="register">
              <thead>
                <tr>
                  <th>Step</th>
                  <th data-numeric>Filled</th>
                  <th data-numeric>Deferred</th>
                  <th>Moved on by</th>
                </tr>
              </thead>
              <tbody>
                {data.walk.steps.map((step) => (
                  <tr key={step.step}>
                    <td>{step.step}</td>
                    <td data-numeric>{step.fields_filled}</td>
                    <td data-numeric>{step.fields_deferred}</td>
                    <td className="muted">{step.advanced_by || "— last step"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {data.walk.final_control ? (
              <p className="muted" style={{ fontSize: 12.5, marginTop: "0.5rem" }}>
                The last control on this form is “{data.walk.final_control}”. The agent
                filled up to it and stopped; releasing presses it.
              </p>
            ) : null}
          </div>
        </details>
      ) : null}

      {data.walk?.needs_user ? (
        <Notice tone="amber" title="The agent stopped part-way through this form">
          {data.walk.stopped_because}
        </Notice>
      ) : null}

      {/* The posting this was written against */}
      {data.job ? (
        <details className="fold">
          <summary>The posting this answers</summary>
          <div className="fold__body prose">
            <div className="row" style={{ gap: "1rem", marginBottom: "0.5rem" }}>
              <span className="record muted">{data.job.location}</span>
              {data.job.salary ? <span className="record muted">{data.job.salary}</span> : null}
              {data.job.fit_score != null ? (
                <span className="record muted">fit {data.job.fit_score.toFixed(2)}</span>
              ) : null}
            </div>
            <p style={{ whiteSpace: "pre-wrap", margin: 0, fontSize: 13 }}>
              {data.job.description}
            </p>
          </div>
        </details>
      ) : null}

      {/* The form as the agent left it. The documents have their own section
          above — a PDF link in a fold is one nobody opens. */}
      {data.screenshot_url ? (
        <details className="fold">
          <summary>The filled form, as the agent left it</summary>
          <div className="fold__body row">
            <a
              className="btn btn--sm"
              href={screenshotUrl(id)}
              target="_blank"
              rel="noreferrer"
            >
              Open the screenshot
            </a>
          </div>
        </details>
      ) : null}

      {/* The release lever */}
      <div className="stack stack--tight">
        <label className="label" htmlFor={`notes-${id}`}>
          Note to yourself (optional)
        </label>
        <input
          id={`notes-${id}`}
          className="input"
          value={notes}
          disabled={busy}
          placeholder="Why you approved or discarded this"
          onChange={(event) => setNotes(event.target.value)}
        />
      </div>

      {outstanding.length > 0 ? (
        <Notice
          tone="amber"
          title={`${outstanding.length} required question${outstanding.length === 1 ? "" : "s"} still unanswered`}
        >
          Answer them above before approving. A form submitted with a blank required
          field is a rejected application.
        </Notice>
      ) : null}

      <div className="row">
        <button
          className="btn"
          disabled={busy || !Object.keys(answers).length}
          onClick={() => act(saveAnswers, () => "Answers recorded")}
        >
          Save answers
        </button>

        <span className="spacer" />

        <button
          className="btn btn--danger btn--sm"
          disabled={busy}
          onClick={() =>
            act(() => api.discard(id, notes), () => "Discarded", "red")
          }
        >
          Discard
        </button>

        {data.reviewed_by_user ? (
          <button
            className="btn btn--release"
            disabled={busy || unverified.length > 0 || defects.length > 0}
            title={
              defects.length
                ? "The analyst found something wrong with this application"
                : undefined
            }
            onClick={() =>
              act(
                // Reaching this button with a judgement outstanding means the
                // user read it and disagreed — the panel above says so and
                // this is the only way past it. Defects keep the button off.
                () => api.submit(id, judgements.length > 0),
                (result) =>
                  result.confirmed
                    ? `Submitted${result.confirmation_ref ? ` — ref ${result.confirmation_ref}` : ""}`
                    : "Submitted, but no confirmation was found — verify with the employer",
                "olive",
              )
            }
          >
            {judgements.length > 0 ? "Release anyway" : "Release to employer"}
          </button>
        ) : (
          <button
            className="btn btn--release"
            disabled={busy || outstanding.length > 0}
            onClick={async () => {
              if (!(await saveAnswers())) return;
              act(() => api.approve(id, notes), () => "Approved — ready to release");
            }}
          >
            Approve
          </button>
        )}
      </div>

      {data.reviewed_by_user ? (
        <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
          Approved {stamp(data.reviewed_at)}. Releasing submits the form in the browser
          window the agent already has open on this application.
        </p>
      ) : null}
    </div>
  );
}

/* ==========================================================================
   The analyst's read of the finished application
   ========================================================================== */

/**
 * What the last reader found, before the release lever.
 *
 * Deliberately above the fields rather than folded away with the attachments.
 * The failure this answers is a user approving an application whose resume
 * they never opened — everything that was wrong with it was knowable, and
 * nothing put it in front of them.
 */
function AnalysisPanel({ analysis, busy, onRegenerate, onFit }) {
  const blockers = analysis.blockers || [];
  const warnings = analysis.warnings || [];
  const defects = blockers.filter((finding) => !finding.overridable);
  const judgements = blockers.filter((finding) => finding.overridable);

  return (
    <section className="stack stack--tight">
      <div className="row" style={{ gap: "1.5rem", alignItems: "baseline" }}>
        <span className="label">Read before sending</span>
        {analysis.fit_score != null ? (
          <span className="record muted" style={{ fontSize: 12 }}>
            answers {analysis.fit_score}% of the posting
          </span>
        ) : null}
        {analysis.ats_score != null ? (
          <span className="record muted" style={{ fontSize: 12 }}>
            {analysis.ats_score}/100 machine readable
          </span>
        ) : null}
      </div>

      {defects.length > 0 ? (
        <Notice
          tone="red"
          title={`${defects.length} thing${defects.length === 1 ? "" : "s"} wrong with this application`}
        >
          {defects.map((finding) => (
            <Finding key={finding.check} finding={finding} />
          ))}
          <div style={{ marginTop: "0.4rem" }}>
            These are not matters of taste — release stays locked until they are
            fixed. Most are fixed by writing the documents again.
          </div>
          {onRegenerate ? (
            <div className="row" style={{ marginTop: "0.5rem", gap: "0.4rem" }}>
              <button className="btn btn--sm" disabled={busy} onClick={onRegenerate}>
                Rewrite and re-attach the documents
              </button>
            </div>
          ) : null}
        </Notice>
      ) : null}

      {judgements.map((finding) => (
        <Notice key={finding.check} tone="amber" title="Worth reconsidering">
          <Finding finding={finding} />
          <div style={{ marginTop: "0.4rem" }}>
            This is a judgement about odds, not a defect. Release anyway if you
            disagree — the decision is recorded.
          </div>
          {onFit ? (
            <div style={{ marginTop: "0.5rem" }}>
              <button className="btn btn--sm" disabled={busy} onClick={onFit}>
                Fit my documents to this posting
              </button>
              <div className="muted" style={{ fontSize: 12, marginTop: "0.3rem" }}>
                Reframes your summary and rewords every passage into this
                employer's language, keeping each change only where it actually
                raises the score. It cannot write in experience your resume does
                not have — where a posting names a technology you have never
                used, that gap is what is left, and a closer posting is the
                better move.
              </div>
            </div>
          ) : null}
        </Notice>
      ))}

      {warnings.length > 0 ? (
        <details className="fold">
          <summary>
            {warnings.length} thing{warnings.length === 1 ? "" : "s"} that would
            land better with work
          </summary>
          <div className="fold__body stack stack--tight">
            {warnings.map((finding) => (
              <Finding key={finding.check} finding={finding} />
            ))}
          </div>
        </details>
      ) : null}

      {analysis.ready && !warnings.length ? (
        <Notice tone="olive" title="Nothing to raise">
          The resume, the answers and the attachments were read against this
          posting and came back clean.
        </Notice>
      ) : null}
    </section>
  );
}

function Finding({ finding }) {
  return (
    <div style={{ marginTop: "0.35rem" }}>
      <span>{finding.detail}</span>{" "}
      {finding.fix ? <span className="muted">{finding.fix}</span> : null}
    </div>
  );
}

/* ==========================================================================
   The documents, where they can actually be read
   ========================================================================== */

/**
 * The tailored resume and letter, in the tray rather than behind a link.
 *
 * A tailored document is not the file the user uploaded: it has been reordered
 * and reworded for this posting. Which parts changed is the thing they most
 * need to see and the thing a PDF link in a fold least encourages them to look
 * at — so the text is here, with what it was written from named beside it.
 */
function Documents({ documents }) {
  if (!documents.length) return null;

  return (
    <section className="stack stack--tight">
      <span className="label">What will be attached</span>

      {documents.map((doc) => (
        <details key={doc.version_id} className="fold">
          <summary>
            {doc.kind.replace("_", " ")} — rewritten for this job
            {doc.written_from ? ` from “${doc.written_from}”` : ""}
          </summary>
          <div className="fold__body stack stack--tight">
            <div className="row" style={{ gap: "1rem", fontSize: 12 }}>
              <span className="record muted">{doc.generator}</span>
              <span className="record muted">{stamp(doc.created_at)}</span>
              <a
                className="btn btn--sm"
                href={pdfUrl(doc.version_id)}
                target="_blank"
                rel="noreferrer"
              >
                Open the PDF
              </a>
            </div>

            {(doc.tailoring_notes || []).map((note) => (
              <p key={note} className="muted" style={{ margin: 0, fontSize: 12.5 }}>
                {note}
              </p>
            ))}

            <pre
              className="prose"
              style={{
                whiteSpace: "pre-wrap",
                fontSize: 12.5,
                margin: 0,
                maxHeight: "22rem",
                overflowY: "auto",
              }}
            >
              {doc.content_text}
            </pre>
          </div>
        </details>
      ))}
    </section>
  );
}
