/**
 * Dispatch — application emails, drafted and held until released.
 *
 * Email is the one path where the agent writes something that leaves in the
 * user's name from their own address, so the flow is deliberately three
 * separate acts: read the draft, approve it, send it. Editing after approval
 * revokes the approval, because an approval applies to the text that was read.
 */

import { useState } from "react";
import { api } from "../api.js";
import {
  Empty,
  Loading,
  Notice,
  SectionHead,
  Stamp,
  stamp,
  useAsync,
} from "../components.jsx";

export default function Dispatch({ toast, onNavigate }) {
  const drafts = useAsync(() => api.drafts());
  const [openId, setOpenId] = useState(null);

  if (drafts.loading) return <Loading label="Reading dispatch" />;
  if (drafts.error) {
    return <Notice tone="red" title="Could not read dispatch">{drafts.error.message}</Notice>;
  }

  const items = drafts.data?.drafts || [];

  return (
    <>
      <SectionHead
        title="Dispatch"
        hint={
          items.length
            ? `${drafts.data.awaiting_review} awaiting you · ${drafts.data.hourly_quota_remaining} of ${drafts.data.hourly_limit} sends left this hour`
            : "Application emails, drafted and held"
        }
      />

      {!items.length ? (
        <Empty
          mark="Nothing to send"
          title="No emails drafted"
          action={
            <button className="btn" onClick={() => onNavigate("wire")}>
              Find a posting
            </button>
          }
        >
          Some postings ask you to email a CV rather than fill a form. Draft one from the
          wire and it waits here until you release it.
        </Empty>
      ) : (
        <div className="stack">
          {items.map((draft) => (
            <DraftBlank
              key={draft.id}
              summary={draft}
              open={openId === draft.id}
              onToggle={() => setOpenId(openId === draft.id ? null : draft.id)}
              onChanged={drafts.reload}
              toast={toast}
            />
          ))}
        </div>
      )}
    </>
  );
}

function DraftBlank({ summary, open, onToggle, onChanged, toast }) {
  const sent = summary.status === "sent";

  return (
    <article className="blank blank--feed">
      <header className="blank__head">
        <div className="blank__to">
          <span className="label">To</span>
          <h3 className="blank__title record" style={{ fontSize: "1rem" }}>
            {summary.to_email}
          </h3>
          <span className="record" style={{ fontSize: 12.5, color: "var(--ink-2)" }}>
            {summary.company} — {summary.job_title}
          </span>
        </div>
        <Stamp status={summary.status} struck />
      </header>

      <div className="blank__body">
        <div className="fields">
          <div className="field">
            <span className="field__label">Subject</span>
            <span className="field__value field__value--filled">{summary.subject}</span>
          </div>
          <div className="field">
            <span className="field__label">Attachments</span>
            <span className="field__value field__value--filled">
              {summary.attachment_count} file{summary.attachment_count === 1 ? "" : "s"}
            </span>
          </div>
          <div className="field">
            <span className="field__label">Address found by</span>
            <span className="field__value field__value--filled">
              {summary.recipient_source || "—"}
            </span>
          </div>
          {sent ? (
            <div className="field">
              <span className="field__label">Sent</span>
              <span className="field__value field__value--filled">
                {stamp(summary.sent_at)} via {summary.send_method}
              </span>
            </div>
          ) : null}
        </div>

        {summary.error_message ? (
          <div style={{ marginTop: "0.75rem" }}>
            <Notice tone="red" title="Last send failed">{summary.error_message}</Notice>
          </div>
        ) : null}

        {open ? <DraftDetail id={summary.id} onChanged={onChanged} toast={toast} /> : null}
      </div>

      <footer className="blank__foot">
        <button className="btn btn--sm" onClick={onToggle} aria-expanded={open}>
          {open ? "Close draft" : "Read the whole email"}
        </button>
        <span className="spacer" />
        {!open && !sent ? (
          <span className="label">Read it before releasing</span>
        ) : null}
      </footer>
    </article>
  );
}

function DraftDetail({ id, onChanged, toast }) {
  const detail = useAsync(() => api.draft(id), [id]);
  const [edit, setEdit] = useState(null);
  const [busy, setBusy] = useState(false);

  if (detail.loading) return <Loading rows={1} label="Reading the draft" />;
  if (detail.error) {
    return <Notice tone="red" title="Could not read this draft">{detail.error.message}</Notice>;
  }

  const data = detail.data;
  const sent = data.status === "sent";
  const approved = data.status === "approved";
  const missing = data.missing_attachments || [];

  const act = async (fn, message, tone = "olive") => {
    setBusy(true);
    try {
      await fn();
      toast(message, tone);
      onChanged();
      detail.reload();
      setEdit(null);
    } catch (error) {
      toast(error.message, "red");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="stack" style={{ marginTop: "1rem" }}>
      {missing.length ? (
        <Notice tone="red" title="An attachment is missing from disk">
          {missing.join(", ")} — regenerate the documents before sending.
        </Notice>
      ) : null}

      {edit ? (
        <>
          <div className="field-group">
            <label className="label" htmlFor={`subject-${id}`}>Subject</label>
            <input
              id={`subject-${id}`}
              className="input"
              value={edit.subject}
              onChange={(event) => setEdit({ ...edit, subject: event.target.value })}
            />
          </div>
          <div className="field-group">
            <label className="label" htmlFor={`body-${id}`}>Body</label>
            <textarea
              id={`body-${id}`}
              className="textarea"
              style={{ minHeight: "18rem" }}
              value={edit.body}
              onChange={(event) => setEdit({ ...edit, body: event.target.value })}
            />
          </div>
          <Notice tone="amber">
            Saving an edit clears any approval — you'll approve the text you actually read.
          </Notice>
          <div className="row">
            <button
              className="btn"
              disabled={busy}
              onClick={() => act(() => api.editDraft(id, edit), "Draft updated")}
            >
              Save changes
            </button>
            <button className="btn btn--quiet" disabled={busy} onClick={() => setEdit(null)}>
              Cancel
            </button>
          </div>
        </>
      ) : (
        <section>
          <span className="label">The message as it will be sent</span>
          <pre
            className="record"
            style={{
              whiteSpace: "pre-wrap",
              background: "var(--stock-2)",
              border: "1px solid var(--rule)",
              borderRadius: "var(--radius)",
              padding: "0.85rem",
              margin: "0.4rem 0 0",
              fontSize: 12.5,
              lineHeight: 1.55,
            }}
          >
            {data.body}
          </pre>
        </section>
      )}

      {data.attachments?.length ? (
        <section>
          <span className="label">Attached</span>
          <ul className="record" style={{ margin: "0.3rem 0 0", paddingLeft: "1.1rem", fontSize: 12.5 }}>
            {data.attachments.map((file) => (
              <li key={file.path} style={{ color: file.exists ? undefined : "var(--red)" }}>
                {file.name} {file.exists ? `(${Math.round(file.size_bytes / 1024)} KB)` : "— missing"}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {!sent && !edit ? (
        <div className="row">
          <button className="btn btn--sm" disabled={busy} onClick={() => setEdit({ subject: data.subject, body: data.body })}>
            Edit
          </button>
          <span className="spacer" />
          <button
            className="btn btn--danger btn--sm"
            disabled={busy}
            onClick={() => act(() => api.discardDraft(id, "Discarded from dispatch"), "Draft discarded", "red")}
          >
            Discard
          </button>
          {approved ? (
            <button
              className="btn btn--release"
              disabled={busy || missing.length > 0}
              onClick={() => act(() => api.sendDraft(id), "Email sent")}
            >
              Send now
            </button>
          ) : (
            <button
              className="btn btn--release"
              disabled={busy}
              onClick={() => act(() => api.approveDraft(id), "Approved — send when ready")}
            >
              Approve
            </button>
          )}
        </div>
      ) : null}

      {approved ? (
        <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>
          Approved. Sending is a separate act — nothing goes out until you press Send.
        </p>
      ) : null}
    </div>
  );
}
