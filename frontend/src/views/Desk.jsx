/**
 * The Desk — who you are, what you're looking for, and what the agent sends
 * on your behalf. Setup lives here so the tray stays about deciding.
 *
 * Ordered by what a first-time user must do, in sequence: identity, then the
 * resume that speaks for them, then what they're looking for, then the run
 * that goes and looks.
 */

import { useEffect, useState } from "react";
import { api, pdfUrl } from "../api.js";
import {
  Empty,
  Fold,
  Loading,
  Notice,
  SectionHead,
  Stamp,
  stamp,
  useAsync,
} from "../components.jsx";

export default function Desk({ toast, onNavigate }) {
  const profile = useAsync(() => api.profile());
  const masters = useAsync(() => api.masters());
  const profiles = useAsync(() => api.searchProfiles());

  const activeResume = (masters.data?.masters || []).find((m) => m.is_active);

  return (
    <div className="stack" style={{ gap: "1.75rem" }}>
      {/* The control that gets used every day comes first. Everything below
          it is setup, done once and then folded away. */}
      <RunCard toast={toast} onNavigate={onNavigate} />

      <div>
        <Fold
          title="Your details"
          hint="What the agent types into application forms — and nothing beyond it"
          summary={
            profile.data?.full_name
              ? `${profile.data.full_name} · ${profile.data.email}`
              : "Not filled in yet"
          }
          defaultOpen={!profile.loading && !profile.data?.full_name}
        >
          <ProfileCard toast={toast} onSaved={profile.reload} />
        </Fold>

        <Fold
          title="Master documents"
          hint="Your real resume. Every tailored version is checked against it."
          summary={activeResume ? `In use — ${activeResume.name}` : "No resume uploaded"}
          defaultOpen={!masters.loading && !activeResume}
        >
          <DocumentsCard toast={toast} onChanged={masters.reload} />
        </Fold>

        <Fold
          title="What you're looking for"
          hint="Saved criteria the agent searches with"
          summary={
            profiles.data?.length
              ? profiles.data.map((p) => p.name).join(" · ")
              : "No search profile yet"
          }
          defaultOpen={!profiles.loading && !profiles.data?.length}
        >
          <SearchProfilesCard toast={toast} onChanged={profiles.reload} />
        </Fold>
      </div>
    </div>
  );
}

/* ==========================================================================
   Who you are
   ========================================================================== */

const PROFILE_FIELDS = [
  ["full_name", "Full name", "text", true],
  ["email", "Email", "email", true],
  ["phone", "Phone", "tel", false],
  ["location", "Location", "text", false],
  ["linkedin_url", "LinkedIn", "url", false],
  ["github_url", "GitHub", "url", false],
  ["portfolio_url", "Portfolio", "url", false],
  ["work_authorization", "Work authorisation", "text", false],
  ["notice_period", "Notice period", "text", false],
];

function ProfileCard({ toast, onSaved }) {
  const profile = useAsync(() => api.profile());
  const [form, setForm] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (profile.data) {
      setForm(
        Object.fromEntries(PROFILE_FIELDS.map(([key]) => [key, profile.data[key] ?? ""])),
      );
    }
  }, [profile.data]);

  if (profile.loading || !form) return <Loading rows={1} label="Reading your details" />;

  const save = async () => {
    setBusy(true);
    try {
      await api.saveProfile(form);
      toast("Your details are saved");
      profile.reload();
      onSaved?.();
    } catch (error) {
      toast(error.message, "red");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="blank">
        <div className="blank__body">
          <div className="grid-3">
            {PROFILE_FIELDS.map(([key, label, type, required]) => (
              <div className="field-group" key={key}>
                <label className="label" htmlFor={`profile-${key}`}>
                  {label}
                  {required ? " *" : ""}
                </label>
                <input
                  id={`profile-${key}`}
                  className="input"
                  type={type}
                  value={form[key]}
                  onChange={(event) => setForm({ ...form, [key]: event.target.value })}
                />
              </div>
            ))}
          </div>

          <Notice tone="olive" title="What is deliberately absent">
            There is no field here for gender, race, disability or veteran status, and none
            for your salary history. The agent leaves those questions blank on every form
            and hands them to you.
          </Notice>
        </div>

        <footer className="blank__foot">
          <button
            className="btn btn--release"
            disabled={busy || !form.full_name || !form.email}
            onClick={save}
          >
            Save details
          </button>
          {profile.data?.configured ? (
            <span className="label">
              {Object.keys(profile.data.remembered_answers || {}).length} remembered answer(s)
            </span>
          ) : null}
        </footer>
      </div>
    </>
  );
}

/* ==========================================================================
   The resume that speaks for you
   ========================================================================== */

function DocumentsCard({ toast, onChanged }) {
  const masters = useAsync(() => api.masters());
  const [busy, setBusy] = useState(false);
  const [docType, setDocType] = useState("resume");

  const upload = async (file) => {
    if (!file) return;
    setBusy(true);
    try {
      const result = await api.uploadMaster(file, docType);
      toast(`Stored — ${result.sections.length} sections read from ${result.name}`);
      masters.reload();
      onChanged?.();
    } catch (error) {
      toast(error.message, "red");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="blank">
        <div className="blank__body stack">
          <div className="row">
            <select
              className="select"
              style={{ width: "auto" }}
              value={docType}
              onChange={(event) => setDocType(event.target.value)}
              aria-label="Document type"
            >
              <option value="resume">Resume</option>
              <option value="cover_letter">Cover letter</option>
            </select>
            <label className="btn" style={{ cursor: "pointer" }}>
              {busy ? "Reading…" : "Upload a file"}
              <input
                type="file"
                accept=".pdf,.docx,.txt,.md"
                hidden
                disabled={busy}
                onChange={(event) => upload(event.target.files?.[0])}
              />
            </label>
            <span className="label">.docx · .pdf · .txt · .md</span>
          </div>

          {masters.loading ? (
            <Loading rows={1} label="Reading stored documents" />
          ) : masters.data?.masters?.length ? (
            <div className="stack" style={{ gap: "0.5rem" }}>
              {masters.data.masters.map((master) => (
                <div
                  key={master.id}
                  className="row"
                  style={{
                    gap: "0.6rem",
                    alignItems: "baseline",
                    padding: "0.5rem 0.65rem",
                    // The one in use has to be readable at a glance: every
                    // tailored document is built from it, so mistaking which
                    // is active means applying with the wrong resume.
                    border: master.is_active
                      ? "1px solid var(--olive)"
                      : "1px solid var(--rule)",
                  }}
                >
                  <Stamp
                    label={master.is_active ? "In use" : "Stored"}
                    tone={master.is_active ? "olive" : undefined}
                  />
                  <div style={{ flex: 1, minWidth: "12rem" }}>
                    <div className="record">{master.name}</div>
                    <div className="muted" style={{ fontSize: 12 }}>
                      {master.doc_type.replace("_", " ")} ·{" "}
                      {master.sections.join(", ") || "no sections detected"}
                    </div>
                  </div>
                  <AtsBadge master={master} toast={toast} onChanged={masters.reload} />
                  {master.is_active ? null : (
                    <button
                      className="btn btn--sm"
                      disabled={busy}
                      onClick={async () => {
                        setBusy(true);
                        try {
                          await api.activateMaster(master.id);
                          toast(`${master.name} is now the one in use`);
                          masters.reload();
                          onChanged?.();
                        } catch (error) {
                          toast(error.message, "red");
                        } finally {
                          setBusy(false);
                        }
                      }}
                    >
                      Use this one
                    </button>
                  )}
                </div>
              ))}
            </div>
          ) : (
            <Empty mark="No master" title="Nothing uploaded yet">
              Upload the resume you actually send. The agent reorders and rewords it per
              job, and refuses to add anything this file does not say.
            </Empty>
          )}
        </div>
      </div>
    </>
  );
}


/**
 * How well one master resume survives an applicant tracking system.
 *
 * The score is mechanical — parseable contact block, headings a parser routes
 * on, dated roles, bulleted claims — so it means something. The improver only
 * ever changes form: it renames a heading, bullets a list that was already a
 * list, leads a bullet with the verb its own sentence already used. It will
 * not write in a skill to close a gap, and a rewrite that introduces anything
 * the original doesn't say is refused rather than offered.
 */
function AtsBadge({ master, toast, onChanged }) {
  const ats = useAsync(() => api.atsScore(master.id), [master.id]);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState(false);

  if (ats.loading || !ats.data) return null;

  const { score, passed, failures } = ats.data;
  const tone = passed ? "olive" : score >= 60 ? "amber" : "red";

  const improve = async () => {
    setBusy(true);
    try {
      const result = await api.atsImprove(master.id, true);

      toast(
        result.applied
          ? `ATS score ${result.score_before} → ${result.score_after} — ${result.improvements.length} change(s) applied`
          : "Nothing could be improved without changing what the resume says",
        result.applied ? "olive" : "amber",
      );

      ats.reload();
      onChanged?.();
    } catch (error) {
      toast(error.message, "red");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="row" style={{ gap: "0.4rem", alignItems: "center" }}>
      <button
        className="btn btn--quiet btn--sm"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        title="What an applicant tracking system can read"
      >
        <Stamp label={`ATS ${score}`} tone={tone} />
      </button>

      {!passed ? (
        <button className="btn btn--sm" disabled={busy} onClick={improve}>
          {busy ? "Improving…" : "Improve"}
        </button>
      ) : null}

      {open ? (
        <div style={{ flexBasis: "100%", marginTop: "0.4rem" }}>
          {failures.length ? (
            <Notice tone={tone} title={`${failures.length} thing(s) an ATS will struggle with`}>
              <ul style={{ margin: "0.3rem 0 0", paddingLeft: "1.1rem" }}>
                {failures.map((f) => (
                  <li key={f.check} style={{ fontSize: 13 }}>
                    <strong style={{ display: "inline" }}>{f.check}:</strong> {f.detail}
                    {f.fix ? <div className="muted">{f.fix}</div> : null}
                  </li>
                ))}
              </ul>
            </Notice>
          ) : (
            <Notice tone="olive" title="Reads cleanly">
              Contact details, headings, dates, bullets and layout all parse.
            </Notice>
          )}
        </div>
      ) : null}
    </div>
  );
}

/* ==========================================================================
   What you're looking for
   ========================================================================== */

function SearchProfilesCard({ toast, onChanged }) {
  const profiles = useAsync(() => api.searchProfiles());
  const [form, setForm] = useState({
    name: "",
    titles: "",
    location: "",
    remote_pref: "remote",
    salary_min: "",
    exclusions: "",
  });
  const [busy, setBusy] = useState(false);

  const create = async () => {
    setBusy(true);
    try {
      await api.createSearchProfile({
        name: form.name,
        target_titles: form.titles.split(",").map((s) => s.trim()).filter(Boolean),
        region: form.location || null,
        remote_pref: form.remote_pref || null,
        salary_min: form.salary_min ? Number(form.salary_min) : null,
        exclusions: form.exclusions.split(",").map((s) => s.trim()).filter(Boolean),
      });
      toast(`Search profile “${form.name}” saved`);
      setForm({ ...form, name: "", titles: "", exclusions: "" });
      profiles.reload();
      onChanged?.();
    } catch (error) {
      toast(error.message, "red");
    } finally {
      setBusy(false);
    }
  };

  const remove = async (profile) => {
    setBusy(true);
    try {
      await api.deleteSearchProfile(profile.id);
      toast(`“${profile.name}” deleted — past runs are kept`);
      profiles.reload();
      onChanged?.();
    } catch (error) {
      toast(error.message, "red");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="blank">
        <div className="blank__body stack">
          {profiles.data?.length ? (
            <div className="stack" style={{ gap: "0.4rem" }}>
              {profiles.data.map((profile) => (
                <div
                  key={profile.id}
                  className="row"
                  style={{
                    gap: "0.6rem",
                    alignItems: "baseline",
                    padding: "0.45rem 0.65rem",
                    border: "1px solid var(--rule)",
                  }}
                >
                  <div style={{ flex: 1, minWidth: "12rem" }}>
                    <div className="record">{profile.name}</div>
                    <div className="muted" style={{ fontSize: 12 }}>
                      {(profile.target_titles || []).join(", ") || "any title"}
                      {profile.remote_pref ? ` · ${profile.remote_pref}` : ""}
                      {profile.region ? ` · ${profile.region}` : ""}
                    </div>
                  </div>
                  <button
                    className="btn btn--sm"
                    disabled={busy}
                    onClick={() => remove(profile)}
                    title="Delete this search profile"
                  >
                    Delete
                  </button>
                </div>
              ))}
            </div>
          ) : null}

          <div className="grid-3">
            <div className="field-group">
              <label className="label" htmlFor="sp-name">Name this search *</label>
              <input
                id="sp-name"
                className="input"
                value={form.name}
                placeholder="Backend — remote"
                onChange={(event) => setForm({ ...form, name: event.target.value })}
              />
            </div>
            <div className="field-group">
              <label className="label" htmlFor="sp-titles">Job titles *</label>
              <input
                id="sp-titles"
                className="input"
                value={form.titles}
                placeholder="Backend Engineer, Platform Engineer"
                onChange={(event) => setForm({ ...form, titles: event.target.value })}
              />
            </div>
            <div className="field-group">
              <label className="label" htmlFor="sp-remote">Remote preference</label>
              <select
                id="sp-remote"
                className="select"
                value={form.remote_pref}
                onChange={(event) => setForm({ ...form, remote_pref: event.target.value })}
              >
                <option value="">Any</option>
                <option value="remote">Remote</option>
                <option value="hybrid">Hybrid</option>
                <option value="on_site">On site</option>
              </select>
            </div>
            <div className="field-group">
              <label className="label" htmlFor="sp-location">Location</label>
              <input
                id="sp-location"
                className="input"
                value={form.location}
                placeholder="Singapore"
                onChange={(event) => setForm({ ...form, location: event.target.value })}
              />
            </div>
            <div className="field-group">
              <label className="label" htmlFor="sp-salary">Salary floor</label>
              <input
                id="sp-salary"
                className="input"
                type="number"
                value={form.salary_min}
                placeholder="100000"
                onChange={(event) => setForm({ ...form, salary_min: event.target.value })}
              />
            </div>
            <div className="field-group">
              <label className="label" htmlFor="sp-excl">Exclude these words</label>
              <input
                id="sp-excl"
                className="input"
                value={form.exclusions}
                placeholder="unpaid, internship"
                onChange={(event) => setForm({ ...form, exclusions: event.target.value })}
              />
            </div>
          </div>
        </div>

        <footer className="blank__foot">
          <button
            className="btn btn--release"
            disabled={busy || !form.name || !form.titles}
            onClick={create}
          >
            Save search profile
          </button>
        </footer>
      </div>
    </>
  );
}

/* ==========================================================================
   Send the agent out
   ========================================================================== */

function RunCard({ toast, onNavigate }) {
  const profiles = useAsync(() => api.searchProfiles());
  const runs = useAsync(() => api.runs(5));
  // A run across zero platforms searches nothing. Saying so here — before the
  // button is pressed — beats a summary afterwards explaining why nothing came
  // back, which reads like the search itself came up empty.
  const health = useAsync(() => api.health());
  const [profileId, setProfileId] = useState("");
  const [documents, setDocuments] = useState(true);
  const [queue, setQueue] = useState(false);
  const [busy, setBusy] = useState(false);
  // Whether the resume in use has anything to do with the roles being
  // searched. The agent will not invent experience to close that gap, so the
  // gap has to be visible before a run rather than after forty applications.
  const match = useAsync(
    () => (profileId ? api.resumeMatch(Number(profileId)) : Promise.resolve(null)),
    [profileId],
  );

  useEffect(() => {
    if (profiles.data?.length && !profileId) setProfileId(String(profiles.data[0].id));
  }, [profiles.data, profileId]);

  const start = async () => {
    setBusy(true);
    try {
      const result = await api.startRun({
        profileId: Number(profileId),
        generateDocuments: documents,
        queueApplications: queue,
      });
      const nothingRan = !result.platforms_run?.length;
      toast(
        result.summary,
        result.errors?.length || nothingRan ? "amber" : "olive",
      );
      runs.reload();
      health.reload();
    } catch (error) {
      toast(error.message, "red");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section>
      <SectionHead title="Send the agent out" hint="One pass across every connected platform" />

      <div className="blank">
        <div className="blank__body stack">
          {match.data && match.data.checked && !match.data.matches ? (
            <Notice tone="red" title="The resume in use doesn't fit this search">
              {match.data.reason}
            </Notice>
          ) : null}

          {health.data && health.data.total === 0 ? (
            <Notice tone="red" title="No platforms are connected">
              <div className="stack" style={{ gap: "0.6rem", alignItems: "flex-start" }}>
                <span>
                  The agent searches the platforms you have connected, and there are none
                  yet, so a run would come back empty however good your search profile is.
                </span>
                <button className="btn btn--sm" onClick={() => onNavigate("stations")}>
                  Connect a platform
                </button>
              </div>
            </Notice>
          ) : null}

          <div className="row">
            <select
              className="select"
              style={{ width: "auto", minWidth: "14rem" }}
              value={profileId}
              onChange={(event) => setProfileId(event.target.value)}
              aria-label="Search profile"
            >
              {(profiles.data || []).map((profile) => (
                <option key={profile.id} value={profile.id}>
                  {profile.name}
                </option>
              ))}
            </select>

            <label className="row" style={{ gap: "0.35rem" }}>
              <input
                type="checkbox"
                checked={documents}
                onChange={(event) => setDocuments(event.target.checked)}
              />
              <span style={{ fontSize: 13 }}>Tailor documents</span>
            </label>

            <label className="row" style={{ gap: "0.35rem" }}>
              <input
                type="checkbox"
                checked={queue}
                onChange={(event) => setQueue(event.target.checked)}
              />
              <span style={{ fontSize: 13 }}>Fill applications</span>
            </label>

            <span className="spacer" />

            <button className="btn btn--release" disabled={busy || !profileId} onClick={start}>
              {busy ? "Running…" : "Run now"}
            </button>
          </div>

          <Notice tone="olive">
            A run never submits anything. With “fill applications” on it opens forms, fills
            what it knows, and leaves them in the tray for you.
          </Notice>

          {runs.data?.runs?.length ? (
            <div className="fields">
              {runs.data.runs.map((run) => (
                <div className="field" key={run.id}>
                  <span className="field__label">{stamp(run.started_at)}</span>
                  <span className="field__value field__value--filled">
                    {run.summary}
                  </span>
                </div>
              ))}
            </div>
          ) : null}
        </div>

        <footer className="blank__foot">
          <button className="btn btn--sm" onClick={() => onNavigate("wire")}>
            See what came in
          </button>
        </footer>
      </div>
    </section>
  );
}
