/**
 * The Wire — postings that came in over the connected platforms.
 *
 * A register, not a card grid: this is a list you scan and triage, and a table
 * lets you compare fit, salary and company down a column. Jobs the hard
 * filters rejected are kept and shown struck through, because "the agent threw
 * away 40 jobs and won't say which" is worse than a longer list.
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

export default function Wire({ toast, onNavigate }) {
  const [showFiltered, setShowFiltered] = useState(false);
  const jobs = useAsync(
    () =>
      api.jobs({
        limit: 200,
        hard_filter_pass: showFiltered ? undefined : true,
        // "Show filtered out" also lifts the resume filter: the user is
        // asking to see everything the agent has, and a postings list that
        // stays empty when you press "show me anyway" reads as broken.
        for_current_resume: showFiltered ? false : undefined,
      }),
    [showFiltered],
  );
  // What came in before the filters had their say. Without this, a run that
  // found forty postings and rejected all forty looks identical to a run that
  // found nothing — and the user goes off reconnecting platforms that are fine.
  const everything = useAsync(() => api.jobs({ limit: 1, for_current_resume: false }), []);
  const [busyId, setBusyId] = useState(null);

  const resync = async () => {
    setBusyId("resync");
    try {
      const result = await api.resyncPipeline();
      toast(result.message, "olive");
      // The run is a background task, so the wire will not have refilled by
      // the time this returns. Reloading shows whatever landed; the desk's
      // run history has the rest.
      jobs.reload();
      everything.reload();
    } catch (error) {
      toast(error.message, "red");
    } finally {
      setBusyId(null);
    }
  };

  if (jobs.loading || everything.loading) return <Loading label="Reading the wire" />;
  if (jobs.error) {
    return <Notice tone="red" title="Could not read the wire">{jobs.error.message}</Notice>;
  }

  const rows = jobs.data?.jobs || [];

  const totalSeen = everything.data?.total ?? 0;

  if (!rows.length && !showFiltered) {
    // Checked before the hard-filter branch, because it is the more accurate
    // diagnosis of the same symptom: when the wire is empty and postings do
    // exist, "they belong to a resume you have moved away from" is the reason,
    // and "your filters rejected them" would send the user to widen a search
    // profile that is not the problem.
    if (jobs.data?.filtered_to_resume && totalSeen > 0) {
      return (
        <>
          <SectionHead
            title="The wire"
            hint={`Nothing yet for ${jobs.data.resume_in_use || "this resume"}`}
          />
          <Empty
            mark="New resume"
            title="No postings have been found for the resume you are using"
            action={
              <div className="row" style={{ gap: "0.4rem" }}>
                <button className="btn" disabled={busyId === "resync"} onClick={resync}>
                  {busyId === "resync" ? "Searching…" : "Search for this resume"}
                </button>
                <button className="btn btn--quiet" onClick={() => setShowFiltered(true)}>
                  Show earlier postings
                </button>
              </div>
            }
          >
            {totalSeen} posting{totalSeen === 1 ? " was" : "s were"} found for an earlier
            resume. They are kept in the register but are not shown here, because an
            application built from this resume could not answer them honestly. Searching
            again reads your resume for the roles it supports and refills the wire.
          </Empty>
        </>
      );
    }

    // Everything that came in was rejected by the hard filters. That is a
    // result, not an absence, and the fix is the search profile — not the
    // platform connections.
    if (totalSeen > 0) {
      return (
        <>
          <SectionHead title="The wire" hint={`${totalSeen} postings, all filtered out`} />
          <Empty
            mark="All filtered"
            title={`${totalSeen} ${totalSeen === 1 ? "posting" : "postings"} came in, and your filters rejected ${totalSeen === 1 ? "it" : "them all"}`}
            action={
              <div className="row" style={{ gap: "0.4rem" }}>
                <button className="btn" onClick={() => setShowFiltered(true)}>
                  Show them anyway
                </button>
                <button className="btn" onClick={() => onNavigate("desk")}>
                  Loosen the search profile
                </button>
              </div>
            }
          >
            The agent searched and found postings — none matched your title, location or
            remote requirements. Widen the profile, or look at what it rejected.
          </Empty>
        </>
      );
    }

    return (
      <>
        <SectionHead title="The wire" hint="Postings the agent has found" />
        <Empty
          mark="No traffic"
          title="Nothing has come in yet"
          action={
            <button className="btn" onClick={() => onNavigate("stations")}>
              Connect a platform
            </button>
          }
        >
          The agent can only search platforms you have connected. Connect one under
          Stations, then run a search from the desk — postings land here, scored against
          your search profile.
        </Empty>
      </>
    );
  }

  const prepare = async (job, mode) => {
    setBusyId(job.id);
    try {
      if (mode === "application") {
        const result = await api.prepareApplication(job.id);
        toast(result.message, "olive");
        jobs.reload();
        onNavigate("tray");
      } else if (mode === "documents") {
        const result = await api.generateDocuments(job.id, "package");
        toast(
          result.review_required
            ? "Documents written — one contains unsupported claims, check it"
            : `Documents written for ${job.company}`,
          result.review_required ? "red" : "olive",
        );
      } else {
        const draft = await api.createDraft(job.id);
        toast(`Email drafted to ${draft.to_email} — read it before sending`, "olive");
        onNavigate("dispatch");
      }
    } catch (error) {
      toast(error.message, "red");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <>
      <SectionHead title="The wire" hint={`${jobs.data.total} postings`}>
        <label className="row" style={{ gap: "0.4rem", fontSize: 12 }}>
          <input
            type="checkbox"
            checked={showFiltered}
            onChange={(event) => setShowFiltered(event.target.checked)}
          />
          <span className="label" style={{ display: "inline" }}>
            Show filtered out
          </span>
        </label>
      </SectionHead>

      <div className="table-wrap">
        <table className="register">
          <thead>
            <tr>
              <th>Company</th>
              <th>Role</th>
              <th>Location</th>
              <th>Salary</th>
              <th data-numeric>Fit</th>
              <th>Stage</th>
              <th>Seen</th>
              <th>Prepare</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((job) => (
              <tr key={job.id} style={job.hard_filter_pass ? undefined : { opacity: 0.55 }}>
                <td>
                  <strong>{job.company}</strong>
                  {!job.hard_filter_pass ? (
                    <div>
                      <Stamp label="Filtered out" tone="red" />
                    </div>
                  ) : null}
                </td>
                <td>{job.title}</td>
                <td className="muted">{job.location}</td>
                <td className="muted">{job.salary || "—"}</td>
                <td data-numeric>
                  {job.fit_score != null ? job.fit_score.toFixed(2) : "—"}
                </td>
                <td>
                  <StageCell job={job} onNavigate={onNavigate} />
                </td>
                <td className="muted">{stamp(job.first_seen_at)}</td>
                <td>
                  {/* Once an application exists for a posting, offering to
                      prepare it again is an invitation to do duplicate work on
                      something already waiting to be sent. */}
                  {job.application_id ? (
                    <button
                      className="btn btn--sm"
                      onClick={() => onNavigate(job.stage === "submitted" ? "register" : "tray")}
                      title={
                        job.stage === "submitted"
                          ? "See this in the register"
                          : "Answer its questions in the outgoing tray"
                      }
                    >
                      {job.stage === "submitted" ? "See record" : "Open in tray"}
                    </button>
                  ) : (
                    <div className="row" style={{ gap: "0.3rem", flexWrap: "nowrap" }}>
                      <button
                        className="btn btn--sm btn--release"
                        disabled={busyId === job.id}
                        onClick={() => prepare(job, "application")}
                        title="Tailor documents, open the form and fill it, ready for review"
                      >
                        Prepare
                      </button>
                      <button
                        className="btn btn--sm"
                        disabled={busyId === job.id}
                        onClick={() => prepare(job, "documents")}
                        title="Tailor a resume and cover letter for this posting"
                      >
                        Documents
                      </button>
                      <button
                        className="btn btn--sm"
                        disabled={busyId === job.id}
                        onClick={() => prepare(job, "email")}
                        title="Draft an application email, if the posting asks for one"
                      >
                        Email
                      </button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {showFiltered ? (
        <p className="muted" style={{ fontSize: 12.5, marginTop: "0.75rem" }}>
          Struck rows failed a hard filter — salary floor, location, an excluded keyword
          or a stale posting date. They are kept so you can see what was rejected.
        </p>
      ) : null}
    </>
  );
}

/**
 * How far a posting has got, and where to go to move it on.
 *
 * A job with a tailored resume and a filled application waiting in the tray
 * used to look identical to one the agent had never touched. The work was
 * done; there was simply nowhere on this screen that said so, and the user
 * had to guess which section to look in next.
 */
const STAGES = {
  found: { label: "Found", tone: undefined, next: null },
  draft: { label: "Being prepared", tone: "amber", next: "tray" },
  documents_ready: { label: "Documents ready", tone: "olive", next: null },
  queued_for_review: { label: "Waiting on you", tone: "amber", next: "tray" },
  approved: { label: "Approved", tone: "olive", next: "tray" },
  submitted: { label: "Submitted", tone: "olive", next: "register" },
  email_sent: { label: "Emailed", tone: "olive", next: "register" },
  failed: { label: "Failed", tone: "red", next: "tray" },
  // A posting the user threw away. It reads as "Found" only if this map has
  // no entry for it — which is what happened, so a discarded job looked
  // untouched and its row offered to open an application the tray no longer
  // held. Preparing it again is the useful action, and the Prepare column
  // offers exactly that because the backend clears application_id here.
  discarded: { label: "Discarded", tone: "red", next: null },
};

function StageCell({ job, onNavigate }) {
  const stage = STAGES[job.stage] || STAGES.found;

  if (!stage.next) {
    return <Stamp label={stage.label} tone={stage.tone} />;
  }

  return (
    <button
      className="btn btn--quiet btn--sm"
      onClick={() => onNavigate(stage.next)}
      title={`Open this in the ${stage.next === "tray" ? "outgoing tray" : "register"}`}
    >
      <Stamp label={stage.label} tone={stage.tone} />
    </button>
  );
}
