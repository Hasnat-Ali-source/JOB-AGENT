/**
 * The Register — the transmission log, and the exports that take it away.
 *
 * Ledger-ruled because that is what it is: a running record of what the agent
 * did, in order, with the result of each line. `paused` and `failure` rows are
 * inked red, since those are the lines that mean something stopped.
 */

import { useState } from "react";
import { api, exportUrl } from "../api.js";
import {
  Counter,
  Empty,
  Loading,
  Notice,
  SectionHead,
  stamp,
  useAsync,
} from "../components.jsx";

const RESULT_TONE = {
  success: "var(--ink-2)",
  partial: "var(--amber)",
  paused: "var(--red)",
  failure: "var(--red)",
};

export default function Register() {
  const [filters, setFilters] = useState({ platform: "", action: "", result: "", days: "" });
  // The register is mostly routine work. This is the view that answers "is
  // anything actually wrong?" without reading several hundred lines.
  const [onlyAttention, setOnlyAttention] = useState(false);
  const audit = useAsync(
    () => api.audit({ ...filters, needs_attention: onlyAttention || undefined, limit: 200 }),
    [filters.platform, filters.action, filters.result, filters.days, onlyAttention],
  );
  const actions = useAsync(() => api.auditActions());
  const summary = useAsync(() => api.auditSummary(7));
  const exports = useAsync(() => api.exports());

  const set = (key) => (event) => setFilters({ ...filters, [key]: event.target.value });

  return (
    <>
      <SectionHead title="The register" hint="Every action the agent has taken, in order" />

      {summary.data ? (
        <div className="grid-3" style={{ marginBottom: "1.25rem" }}>
          <Counter value={summary.data.total_entries} label="Entries, last 7 days" />
          <Counter
            value={summary.data.needs_attention}
            label="Needing you"
            tone={summary.data.needs_attention ? "red" : undefined}
          />
          <Counter
            value={Object.keys(summary.data.by_platform).length}
            label="Platforms active"
          />
        </div>
      ) : null}

      <div className="row" style={{ marginBottom: "1rem" }}>
        <select className="select" style={{ width: "auto" }} value={filters.platform} onChange={set("platform")} aria-label="Platform">
          <option value="">All platforms</option>
          {(actions.data?.platforms || []).map((platform) => (
            <option key={platform} value={platform}>{platform}</option>
          ))}
        </select>

        <select className="select" style={{ width: "auto" }} value={filters.action} onChange={set("action")} aria-label="Action">
          <option value="">All actions</option>
          {(actions.data?.actions || []).map((entry) => (
            <option key={entry.action} value={entry.action}>
              {entry.action.replace(/_/g, " ")} ({entry.count})
            </option>
          ))}
        </select>

        <select className="select" style={{ width: "auto" }} value={filters.result} onChange={set("result")} aria-label="Result">
          <option value="">Any result</option>
          {(actions.data?.results || []).map((result) => (
            <option key={result} value={result}>{result}</option>
          ))}
        </select>

        <select className="select" style={{ width: "auto" }} value={filters.days} onChange={set("days")} aria-label="Period">
          <option value="">All time</option>
          <option value="1">Last 24 hours</option>
          <option value="7">Last 7 days</option>
          <option value="30">Last 30 days</option>
        </select>

        <label className="row" style={{ gap: "0.4rem", fontSize: 12 }}>
          <input
            type="checkbox"
            checked={onlyAttention}
            onChange={(event) => setOnlyAttention(event.target.checked)}
          />
          <span className="label" style={{ display: "inline" }}>
            Only what needs me
          </span>
        </label>

        <span className="spacer" />

        {(exports.data?.exports || []).map((item) => (
          <a key={item.name} className="btn btn--sm" href={exportUrl(item.name)} download>
            {item.name} CSV
          </a>
        ))}
      </div>

      {audit.loading ? (
        <Loading label="Reading the register" />
      ) : audit.error ? (
        <Notice tone="red" title="Could not read the register">{audit.error.message}</Notice>
      ) : !audit.data.entries.length ? (
        <Empty mark="No entries" title="Nothing recorded for this filter">
          The register fills as the agent works. Try widening the filter, or run a search.
        </Empty>
      ) : (
        <>
          <div className="table-wrap">
            <table className="register">
              <thead>
                <tr>
                  {/* The fixed columns used to claim 42rem of a 46rem table,
                      leaving the detail — the only column with something to
                      read in it — four. */}
                  <th style={{ width: "9rem" }}>Time</th>
                  <th style={{ width: "7rem" }}>Platform</th>
                  <th style={{ width: "9rem" }}>Action</th>
                  <th style={{ width: "4rem" }}>By</th>
                  <th style={{ width: "5rem" }}>Result</th>
                  <th style={{ minWidth: "22rem" }}>Detail</th>
                </tr>
              </thead>
              <tbody>
                {groupRuns(audit.data.entries).map((group) => (
                  <RegisterRows key={group.key} group={group} />
                ))}
              </tbody>
            </table>
          </div>

          <p className="muted" style={{ fontSize: 12.5, marginTop: "0.75rem" }}>
            Showing {audit.data.returned} of {audit.data.total} entries, repeated actions
            folded together. Click a folded line to see each one.
          </p>
        </>
      )}
    </>
  );
}

/**
 * Fold a run of identical actions into one line.
 *
 * Finding forty-three jobs writes forty-three lines that say the same thing.
 * Read one at a time they are indistinguishable from each other and from the
 * handful of lines that matter — a platform pausing, a submission failing.
 * Folding them keeps every entry while making the exceptions visible.
 *
 * Only *consecutive* entries fold, so the order of events is never rearranged.
 * A failure never folds — those are the lines the register exists for. Note
 * that "paused" is not a failure here: a question handed to the user is
 * recorded as paused, and eleven of them per application is the single
 * noisiest thing in the log.
 */
function groupRuns(entries) {
  const groups = [];

  for (const entry of entries) {
    const previous = groups[groups.length - 1];
    const foldable = entry.result !== "failure";

    const sameKind =
      previous &&
      previous.foldable &&
      foldable &&
      previous.entries[0].action === entry.action &&
      previous.entries[0].result === entry.result &&
      previous.entries[0].platform === entry.platform;

    if (sameKind) {
      previous.entries.push(entry);
    } else {
      groups.push({ key: entry.id, foldable, entries: [entry] });
    }
  }

  return groups;
}

function RegisterRows({ group }) {
  const [open, setOpen] = useState(false);
  const first = group.entries[0];
  const count = group.entries.length;

  if (count === 1) return <EntryRow entry={first} />;

  return (
    <>
      <tr>
        <td className="muted">
          {group.entries[count - 1].timestamp} — {first.timestamp}
        </td>
        <td>{first.platform || "—"}</td>
        <td>{first.action.replace(/_/g, " ")}</td>
        <td className="muted">{first.actor}</td>
        <td style={{ color: RESULT_TONE[first.result] || "var(--ink-2)" }}>
          {first.result}
        </td>
        <td style={{ whiteSpace: "normal" }}>
          <button
            className="btn btn--quiet btn--sm"
            aria-expanded={open}
            onClick={() => setOpen(!open)}
          >
            {open ? "−" : "+"} {count} × this action
          </button>
          {!open ? (
            <span className="muted" style={{ marginLeft: "0.5rem" }}>
              {first.detail}
            </span>
          ) : null}
        </td>
      </tr>

      {open
        ? group.entries.map((entry) => <EntryRow key={entry.id} entry={entry} indented />)
        : null}
    </>
  );
}

function EntryRow({ entry, indented = false }) {
  return (
    <tr>
      <td className="muted" style={indented ? { paddingLeft: "1.5rem" } : undefined}>
        {entry.timestamp}
      </td>
      <td>{entry.platform || "—"}</td>
      <td>{entry.action.replace(/_/g, " ")}</td>
      <td className="muted">{entry.actor}</td>
      <td style={{ color: RESULT_TONE[entry.result] || "var(--ink-2)" }}>
        {entry.result}
      </td>
      <td style={{ whiteSpace: "normal" }}>
        {entry.detail}
        {entry.error_message ? (
          <div style={{ color: "var(--red)" }}>{entry.error_message}</div>
        ) : null}
      </td>
    </tr>
  );
}
