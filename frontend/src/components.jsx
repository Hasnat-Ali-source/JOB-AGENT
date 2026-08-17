/**
 * Shared pieces of the message-blank vocabulary.
 *
 * Every status in this product is a stamp, every refusal is a query slip, and
 * every empty state names the next action. Keeping those three in one place is
 * what stops the surface drifting back into generic badges and "No data".
 */

import { useEffect, useState } from "react";

/* ==========================================================================
   Stamps
   ========================================================================== */

const STAMP_TONES = {
  // Review + submission
  queued_for_review: ["HELD", "amber"],
  draft: ["DRAFT", "ink"],
  submitted: ["SENT", "olive"],
  email_sent: ["SENT", "olive"],
  rejected: ["VOID", "red"],
  withdrawn: ["VOID", "red"],
  approved: ["CLEARED", "olive"],
  failed: ["FAILED", "red"],
  discarded: ["VOID", "red"],
  sent: ["SENT", "olive"],
  // Platform state
  connected: ["ON LINE", "olive"],
  needs_signin: ["SIGN IN", "amber"],
  session_expired: ["OFF LINE", "red"],
  error: ["FAULT", "red"],
  disabled: ["DISABLED", "ink"],
  // Runs
  completed: ["COMPLETE", "olive"],
  partial: ["PART", "amber"],
  running: ["RUNNING", "ink"],
};

export function Stamp({ status, label, tone, struck = false }) {
  const [mapped, mappedTone] = STAMP_TONES[status] || [];
  const text = label || mapped || (status || "").replace(/_/g, " ").toUpperCase();

  return (
    <span className={`stamp${struck ? " stamp--struck" : ""}`} data-tone={tone || mappedTone || "ink"}>
      {text}
    </span>
  );
}

/* ==========================================================================
   Query slips — the two kinds must never look alike
   ========================================================================== */

export function QuerySlip({ question, detail, value, onChange, disabled }) {
  const refused = detail?.category === "sensitive";
  const options = detail?.options || [];
  const answered = (detail?.value_entered_by_user ?? "") !== "";
  const drafted =
    !answered && (detail?.suggested_answer ?? "") !== "";

  return (
    <div className={`slip ${refused ? "slip--refused" : "slip--unmapped"}`}>
      <div className="row" style={{ gap: "0.5rem" }}>
        <span className="slip__kind">
          {refused ? "The agent will not answer this" : "The agent could not map this"}
        </span>
        {detail?.required ? <Stamp label="Required" tone="red" /> : null}
        {answered ? <Stamp label="Answered" tone="olive" /> : null}
        {drafted ? <Stamp label="Draft — check it" tone="amber" /> : null}
      </div>

      <p className="slip__question">{question}</p>
      <p className="slip__reason">{detail?.reason}</p>

      <div className="slip__answer">
        <label className="label" htmlFor={`answer-${question}`}>
          Your answer{detail?.required ? "" : " (optional)"}
        </label>
        {options.length > 0 ? (
          <select
            id={`answer-${question}`}
            className="select"
            value={value ?? ""}
            disabled={disabled}
            onChange={(event) => onChange(event.target.value)}
          >
            <option value="">Leave blank</option>
            {options.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        ) : detail?.field_type === "checkbox" ? (
          // A consent is a box you tick, not a sentence you compose. Rendered
          // as a text field it gave no way to say yes at all.
          <label className="row" style={{ gap: "0.45rem", alignItems: "flex-start" }}>
            <input
              id={`answer-${question}`}
              type="checkbox"
              checked={value === "Yes"}
              disabled={disabled}
              onChange={(event) => onChange(event.target.checked ? "Yes" : "")}
            />
            <span style={{ fontSize: 13 }}>
              I agree — tick to accept, leave clear to decline
            </span>
          </label>
        ) : detail?.field_type === "textarea" ? (
          <textarea
            id={`answer-${question}`}
            className="textarea"
            value={value ?? ""}
            disabled={disabled}
            onChange={(event) => onChange(event.target.value)}
          />
        ) : (
          <input
            id={`answer-${question}`}
            className="input"
            value={value ?? ""}
            disabled={disabled}
            onChange={(event) => onChange(event.target.value)}
          />
        )}
      </div>
    </div>
  );
}

/* ==========================================================================
   Field rows
   ========================================================================== */

export function FieldRow({ label, value, blank }) {
  return (
    <div className="field">
      <span className="field__label">{label}</span>
      <span className={`field__value ${blank ? "field__value--blank" : "field__value--filled"}`}>
        {blank ? "— left blank —" : shortenPath(value) || "—"}
      </span>
    </div>
  );
}

/**
 * An attached file is shown by its name.
 *
 * The API stores absolute paths, and printing
 * "/private/tmp/…/scratchpad/demo_docs/resume.pdf" into a ruled field buries
 * every other value on the row — especially on a phone, where it wraps to
 * three lines.
 */
export function shortenPath(value) {
  if (typeof value !== "string") return value;
  if (!value.startsWith("/") || !value.includes("/")) return value;
  return value.split("/").filter(Boolean).pop() || value;
}

/* ==========================================================================
   Empty states — every one names the next action
   ========================================================================== */

export function Empty({ mark, title, children, action }) {
  return (
    <div className="empty">
      <span className="empty__mark">{mark}</span>
      <h3>{title}</h3>
      <p>{children}</p>
      {action ? <div className="row">{action}</div> : null}
    </div>
  );
}

/* ==========================================================================
   Loading — a skeleton of the thing being loaded
   ========================================================================== */

export function Loading({ rows = 3, label = "Reading the register" }) {
  return (
    <div className="stack" aria-live="polite" aria-busy="true">
      <span className="label">{label}…</span>
      {Array.from({ length: rows }).map((_, index) => (
        <div key={index} className="blank" style={{ padding: "1rem" }}>
          <div className="stack stack--tight">
            <div className="skeleton" style={{ width: "38%" }} />
            <div className="skeleton" style={{ width: "72%" }} />
            <div className="skeleton" style={{ width: "55%" }} />
          </div>
        </div>
      ))}
    </div>
  );
}

/* ==========================================================================
   Notices
   ========================================================================== */

export function Notice({ tone = "amber", title, children }) {
  return (
    <div className="notice" data-tone={tone}>
      {title ? <strong>{title}</strong> : null}
      <div>{children}</div>
    </div>
  );
}

/* ==========================================================================
   Counters
   ========================================================================== */

export function Counter({ value, label, tone }) {
  return (
    <div className="counter">
      <div className="counter__value" data-tone={tone}>
        {value}
      </div>
      <span className="label counter__label">{label}</span>
    </div>
  );
}

/* ==========================================================================
   Section heading
   ========================================================================== */

export function SectionHead({ title, hint, children }) {
  return (
    <div className="section-head">
      <h2>{title}</h2>
      {hint ? <p>{hint}</p> : null}
      <span className="spacer" />
      {children}
    </div>
  );
}

/* ==========================================================================
   Fold — a section of the form book kept closed until it's wanted
   ========================================================================== */

/**
 * A headed section that opens on demand.
 *
 * Setting up happens once; checking in happens daily. Leaving every setup
 * form permanently unrolled buries the two controls that get used — the run
 * button and the tray — under a page of fields that were filled in weeks ago.
 * Closed, a fold still states what it holds, so nothing is hidden, only
 * folded away.
 */
export function Fold({ title, hint, summary, defaultOpen = false, children }) {
  const [open, setOpen] = useState(defaultOpen);

  return (
    <section className={`fold ${open ? "fold--open" : ""}`}>
      <button
        type="button"
        className="fold__head"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <span className="fold__mark" aria-hidden="true">
          {open ? "−" : "+"}
        </span>
        <span className="fold__title">{title}</span>
        {summary && !open ? <span className="fold__summary">{summary}</span> : null}
        {hint && open ? <span className="fold__summary">{hint}</span> : null}
      </button>

      {open ? <div className="fold__body">{children}</div> : null}
    </section>
  );
}

/* ==========================================================================
   Toasts — transmission receipts
   ========================================================================== */

export function Toasts({ items, onDismiss }) {
  useEffect(() => {
    if (!items.length) return undefined;
    const timers = items.map((toast) =>
      setTimeout(() => onDismiss(toast.id), toast.tone === "red" ? 9000 : 5000),
    );
    return () => timers.forEach(clearTimeout);
  }, [items, onDismiss]);

  if (!items.length) return null;

  return (
    <div className="toasts" role="status" aria-live="polite">
      {items.map((toast) => (
        <div key={toast.id} className="toast" data-tone={toast.tone}>
          {toast.message}
        </div>
      ))}
    </div>
  );
}

/* ==========================================================================
   Async data hook
   ========================================================================== */

export function useAsync(loader, deps = []) {
  const [state, setState] = useState({ data: null, error: null, loading: true });
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let live = true;
    setState((prev) => ({ ...prev, loading: true }));

    loader()
      .then((data) => live && setState({ data, error: null, loading: false }))
      .catch((error) => live && setState({ data: null, error, loading: false }));

    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  return { ...state, reload: () => setNonce((n) => n + 1) };
}

/** Format an ISO timestamp the way the register prints it. */
export function stamp(value) {
  if (!value) return "—";
  const date = new Date(value.includes("T") ? value : value.replace(" ", "T"));
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}
