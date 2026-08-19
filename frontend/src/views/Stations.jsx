/**
 * Stations — the platforms this desk is wired to.
 *
 * Two things this view must not soften:
 *
 * - A platform that needs the user (expired session, CAPTCHA) says so at the
 *   top of its own card, with the agent's own guidance verbatim.
 * - A connector's terms-of-service note is shown *before* the user can turn
 *   automation up, not after.
 */

import { useState } from "react";
import { api } from "../api.js";
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

export default function Stations({ toast }) {
  const health = useAsync(() => api.health());
  const connectors = useAsync(() => api.connectors());
  const interruptions = useAsync(() => api.interruptions());
  const [busy, setBusy] = useState(null);
  // Platforms whose sign-in window we have opened. Signing in happens in that
  // window, where the app cannot see it, so the connection is only recorded
  // when the user comes back and says they are done.
  const [awaitingSignin, setAwaitingSignin] = useState([]);

  if (health.loading || connectors.loading) return <Loading label="Checking the lines" />;
  if (health.error) {
    return <Notice tone="red" title="Could not read platform health">{health.error.message}</Notice>;
  }

  const platforms = health.data?.platforms || [];
  const catalogue = connectors.data?.connectors || [];
  const connected = new Set(platforms.map((p) => p.platform));
  const openInterruptions = interruptions.data?.interruptions || [];

  const reloadAll = () => {
    health.reload();
    connectors.reload();
    interruptions.reload();
  };

  const act = async (key, fn, message, tone = "olive") => {
    setBusy(key);
    try {
      const result = await fn();
      toast(
        typeof message === "function" ? message(result) : message,
        typeof tone === "function" ? tone(result) : tone,
      );
      reloadAll();
    } catch (error) {
      toast(error.message, "red");
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
      <SectionHead
        title="Stations"
        hint={`${health.data.healthy} of ${health.data.total} on line`}
      >
        <button
          className="btn btn--sm"
          disabled={busy === "check"}
          onClick={() => act("check", () => api.checkHealth(), "Lines checked")}
        >
          Check all lines
        </button>
      </SectionHead>

      {openInterruptions.length > 0 ? (
        <div className="stack" style={{ marginBottom: "1.25rem" }}>
          {openInterruptions.map((item) => (
            <Notice
              key={item.id}
              tone="red"
              title={`${item.platform} — ${item.kind.replace(/_/g, " ")}`}
            >
              <div>{item.guidance}</div>
              <div className="row" style={{ marginTop: "0.6rem" }}>
                <button
                  className="btn btn--sm"
                  disabled={busy === `resolve-${item.id}`}
                  onClick={() =>
                    act(
                      `resolve-${item.id}`,
                      () => api.resolveInterruption(item.id, true),
                      "Cleared — the line checks out",
                    )
                  }
                >
                  I've handled it
                </button>
                <span className="label">Detected {stamp(item.detected_at)}</span>
              </div>
            </Notice>
          ))}
        </div>
      ) : null}

      <AddStationCard toast={toast} onAdded={reloadAll} />

      {!platforms.length ? (
        <Empty mark="No stations" title="No platforms connected yet">
          Add one above, or connect a built-in platform below. A browser window opens and
          you sign in yourself — the agent never handles your credentials.
        </Empty>
      ) : (
        <div className="grid-2" style={{ marginBottom: "1.5rem" }}>
          {platforms.map((platform) => (
            <article key={platform.platform} className="blank">
              <header className="blank__head">
                <div className="blank__to">
                  <span className="label">Station</span>
                  <h3 className="blank__title record" style={{ fontSize: "1rem" }}>
                    {platform.platform}
                  </h3>
                </div>
                <Stamp status={platform.status} />
              </header>

              <SearchUrlRow
                platform={platform}
                toast={toast}
                onSaved={reloadAll}
              />

              <div className="blank__body">
                <div className="fields">
                  <div className="field">
                    <span className="field__label">Last verified</span>
                    <span className="field__value field__value--filled">
                      {stamp(platform.last_verified_at)}
                    </span>
                  </div>
                  <div className="field">
                    <span className="field__label">Waiting jobs</span>
                    <span className="field__value field__value--filled">
                      {platform.pending_jobs}
                    </span>
                  </div>
                </div>

                <DailyLimits
                  platform={platform}
                  toast={toast}
                  onSaved={reloadAll}
                />

                {platform.reason ? (
                  <div style={{ marginTop: "0.75rem" }}>
                    <Notice tone="red" title="Needs you">{platform.reason}</Notice>
                  </div>
                ) : null}
              </div>

              <footer className="blank__foot">
                {platform.needs_reconnect ? (
                  <>
                    <button
                      className="btn btn--sm"
                      disabled={busy === platform.platform}
                      onClick={() =>
                        act(
                          platform.platform,
                          () => api.reconnect(platform.platform),
                          "Browser opened — sign in, then press Resume",
                          "amber",
                        )
                      }
                    >
                      Reconnect
                    </button>
                    <button
                      className="btn btn--sm"
                      disabled={busy === `${platform.platform}-resume`}
                      onClick={() =>
                        act(
                          `${platform.platform}-resume`,
                          () => api.resume(platform.platform),
                          (r) => `${r.platform} back on line — ${r.pending_jobs} job(s) waiting`,
                        )
                      }
                    >
                      Resume
                    </button>
                  </>
                ) : (
                  <button
                    className="btn btn--sm"
                    disabled={busy === platform.platform}
                    onClick={() =>
                      act(
                        platform.platform,
                        () => api.checkHealth(platform.platform),
                        "Line checked",
                      )
                    }
                  >
                    Check line
                  </button>
                )}

                {platform.is_custom ? (
                  <button
                    className="btn btn--sm"
                    disabled={busy === `remove-${platform.platform}`}
                    onClick={() =>
                      act(
                        `remove-${platform.platform}`,
                        () => api.disconnect(platform.platform),
                        `${platform.platform} removed`,
                        "amber",
                      )
                    }
                  >
                    Remove
                  </button>
                ) : null}
              </footer>
            </article>
          ))}
        </div>
      )}

      <Fold
        title="Built-in platforms"
        hint="What each one can do, and what it refuses to"
        summary={`${catalogue.length} available · ${platforms.length} connected`}
      >
      <div className="table-wrap">
        <table className="register">
          <thead>
            <tr>
              <th>Platform</th>
              <th>Search</th>
              <th>Read</th>
              <th>Fill</th>
              <th>Submit</th>
              <th>Verified</th>
              <th>Notes</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {catalogue.map((connector) => (
              <tr key={connector.platform}>
                <td>
                  <strong>{connector.platform}</strong>
                  {connected.has(connector.platform) ? (
                    <div><Stamp label="Connected" tone="olive" /></div>
                  ) : null}
                </td>
                <Cell on={connector.capabilities.search} />
                <Cell on={connector.capabilities.read_details} />
                <Cell on={connector.capabilities.fill_standard_fields} />
                <Cell on={connector.capabilities.submit_automatically} />
                <td className="muted">{connector.verified_against || "—"}</td>
                <td style={{ maxWidth: "26rem", whiteSpace: "normal" }}>
                  {connector.tos_risk_note ? (
                    <span style={{ color: "var(--red)" }}>{connector.tos_risk_note}</span>
                  ) : (
                    <span className="muted">Applies directly to the employer.</span>
                  )}
                </td>
                <td>
                  {connected.has(connector.platform) ? null : awaitingSignin.includes(
                      connector.platform,
                    ) ? (
                    <div className="stack" style={{ gap: "0.35rem", alignItems: "flex-start" }}>
                      <button
                        className="btn btn--sm btn--release"
                        disabled={busy === `confirm-${connector.platform}`}
                        onClick={() =>
                          act(
                            `confirm-${connector.platform}`,
                            async () => {
                              const result = await api.checkStatus(connector.platform);
                              if (result.status === "connected") {
                                setAwaitingSignin((current) =>
                                  current.filter((p) => p !== connector.platform),
                                );
                              }
                              return result;
                            },
                            (result) =>
                              result.status === "connected"
                                ? `${connector.platform} is connected — it will be searched on the next run`
                                : "Still signed out in that window — finish signing in, then try again",
                            (result) =>
                              result.status === "connected" ? "olive" : "amber",
                          )
                        }
                      >
                        I have signed in
                      </button>
                      <span className="muted" style={{ fontSize: 12 }}>
                        Sign in in the window that opened, then press this.
                      </span>
                    </div>
                  ) : (
                    <button
                      className="btn btn--sm"
                      disabled={busy === `connect-${connector.platform}`}
                      onClick={() =>
                        act(
                          `connect-${connector.platform}`,
                          async () => {
                            const result = await api.connect(connector.platform);
                            setAwaitingSignin((current) =>
                              current.includes(connector.platform)
                                ? current
                                : [...current, connector.platform],
                            );
                            return result;
                          },
                          "Browser opened — sign in there, then press “I have signed in”",
                          "amber",
                        )
                      }
                    >
                      Connect
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="muted prose" style={{ fontSize: 12.5, marginTop: "0.75rem" }}>
        “Verified: fixture” means the connector was tested against a replica of the
        platform's markup, not a live account. The first three applications on any
        platform go through you regardless.
      </p>
      </Fold>
    </>
  );
}

/**
 * How much this station may do per day, and how much of that is left.
 *
 * These caps are pacing, not safety: they keep the agent from hammering a
 * platform. Running out of search budget stops a run dead, so the remaining
 * count is shown next to the limit and both are editable — otherwise hitting
 * the cap is indistinguishable from a station that has broken.
 */
function DailyLimits({ platform, toast, onSaved }) {
  const [search, setSearch] = useState(String(platform.daily_search_limit ?? ""));
  const [apply, setApply] = useState(String(platform.daily_apply_limit ?? ""));
  const [busy, setBusy] = useState(false);

  const changed =
    Number(search) !== platform.daily_search_limit ||
    Number(apply) !== platform.daily_apply_limit;

  const save = async () => {
    setBusy(true);
    try {
      await api.updatePlatform(platform.platform, {
        daily_search_limit: Number(search),
        daily_apply_limit: Number(apply),
      });
      toast(`${platform.platform} limits updated`, "olive");
      onSaved();
    } catch (error) {
      toast(error.message, "red");
    } finally {
      setBusy(false);
    }
  };

  const spent = platform.searches_left_today === 0 && platform.daily_search_limit > 0;

  return (
    <div style={{ marginTop: "0.75rem" }}>
      <span className="label">Per day · postings / applications</span>

      <div className="row" style={{ gap: "0.35rem", marginTop: "0.25rem" }}>
        <input
          className="input"
          type="number"
          min="0"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          style={{ width: "5.5rem", fontSize: 12 }}
          aria-label="Postings per day"
        />
        <input
          className="input"
          type="number"
          min="0"
          value={apply}
          onChange={(event) => setApply(event.target.value)}
          style={{ width: "5.5rem", fontSize: 12 }}
          aria-label="Applications per day"
        />
        <span className="muted" style={{ fontSize: 12 }}>
          {platform.searches_left_today} postings left today
        </span>
        <span className="spacer" />
        <button className="btn btn--sm" disabled={busy || !changed} onClick={save}>
          Save
        </button>
      </div>

      {spent ? (
        <div style={{ marginTop: "0.5rem" }}>
          <Notice tone="amber" title="No searching left today">
            A run will prepare the jobs already found rather than looking for more.
            Raise the postings-per-day number above to keep searching.
          </Notice>
        </div>
      ) : null}
    </div>
  );
}

/**
 * The URL a station searches, shown on its card and editable in place.
 *
 * Greenhouse, Lever and Ashby host one board per company rather than a single
 * searchable site, so a station on those platforms is inert until it is told
 * whose jobs to read. Making that the visible, fixable thing on the card is
 * the difference between "connected, no jobs" and "connected to nothing yet".
 */
function SearchUrlRow({ platform, toast, onSaved }) {
  const [value, setValue] = useState(platform.search_url || "");
  const [busy, setBusy] = useState(false);

  const needsOne = platform.needs_search_url;

  // Show the field when the station needs a URL, already has one, or is the
  // user's own. Hiding it once a URL was set left no way to correct a wrong
  // one — and pointing a station at the wrong page is the easiest mistake
  // here, because an account dashboard and a job board look alike in a URL bar.
  // Pausing applies to every station, so the row itself always renders.
  const showUrl = needsOne || platform.is_custom || platform.search_url;

  if (!showUrl) {
    return (
      <div style={{ padding: "0 1rem", marginTop: "0.5rem" }}>
        <PauseSwitch platform={platform} toast={toast} onSaved={onSaved} />
      </div>
    );
  }

  const save = async () => {
    setBusy(true);
    try {
      await api.updatePlatform(platform.platform, { search_url: value.trim() });
      toast(
        value.trim()
          ? `${platform.platform} will search ${value.trim()}`
          : `${platform.platform}'s board URL cleared`,
        "olive",
      );
      onSaved();
    } catch (error) {
      toast(error.message, "red");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ padding: "0 1rem", marginTop: "0.5rem" }}>
      <span className="label">
        {platform.is_custom ? "Your station · board URL" : "Board URL"}
      </span>

      <div className="row" style={{ gap: "0.35rem", marginTop: "0.25rem" }}>
        <input
          className="input"
          value={value}
          onChange={(event) => setValue(event.target.value)}
          placeholder="https://job-boards.greenhouse.io/COMPANY"
          style={{ fontSize: 12 }}
        />
        <button
          className="btn btn--sm"
          disabled={busy || value.trim() === (platform.search_url || "")}
          onClick={save}
        >
          Save
        </button>
      </div>

      <SignInSwitch platform={platform} toast={toast} onSaved={onSaved} />
      <PauseSwitch platform={platform} toast={toast} onSaved={onSaved} />

      {needsOne ? (
        <div style={{ marginTop: "0.5rem" }}>
          <Notice tone="amber" title="Nothing to search yet">
            This platform hosts a separate board for each company, so it needs the URL of
            one. Until then a run skips it.
          </Notice>
        </div>
      ) : null}
    </div>
  );
}

/**
 * Take one station out of service without disconnecting it.
 *
 * A signed-in station is expensive to rebuild, so "don't use this one for
 * now" must not mean "throw the session away". Pausing lets a run be aimed at
 * a specific board without disconnecting the rest.
 */
function PauseSwitch({ platform, toast, onSaved }) {
  const [busy, setBusy] = useState(false);

  const change = async (event) => {
    const paused = event.target.checked;
    setBusy(true);
    try {
      await api.updatePlatform(platform.platform, { paused });
      toast(
        paused
          ? `${platform.platform} stopped — runs will skip it until you start it again`
          : `${platform.platform} back in service`,
        paused ? "amber" : "olive",
      );
      onSaved();
    } catch (error) {
      toast(error.message, "red");
    } finally {
      setBusy(false);
    }
  };

  return (
    <label className="row" style={{ gap: "0.4rem", marginTop: "0.4rem", fontSize: 12.5 }}>
      <input
        type="checkbox"
        checked={Boolean(platform.paused)}
        disabled={busy}
        onChange={change}
      />
      <span>
        Stop using this station for now
        {platform.paused ? (
          <span className="muted"> — runs are skipping it, the sign-in is kept</span>
        ) : null}
      </span>
    </label>
  );
}


/**
 * Whether this station has to be signed into, changeable after the fact.
 *
 * Ticking "needs sign-in" when a station was added is a guess, and the wrong
 * guess was unrecoverable: a public board marked private can never prove it
 * has a session, so it sits in `needs_signin` for ever and Resume answers
 * "still isn't usable — reconnect it first", which is advice that cannot
 * work. Most consumer job boards are searchable logged out; this is how you
 * say so.
 */
function SignInSwitch({ platform, toast, onSaved }) {
  const [busy, setBusy] = useState(false);

  if (!platform.is_custom) return null;

  const change = async (event) => {
    const requires = event.target.checked;
    setBusy(true);
    try {
      await api.updatePlatform(platform.platform, { requires_signin: requires });
      toast(
        requires
          ? `${platform.platform} will ask you to sign in`
          : `${platform.platform} marked public — it can be searched now`,
        "olive",
      );
      onSaved();
    } catch (error) {
      toast(error.message, "red");
    } finally {
      setBusy(false);
    }
  };

  return (
    <label
      className="row"
      style={{ gap: "0.4rem", marginTop: "0.5rem", fontSize: 12.5 }}
    >
      <input
        type="checkbox"
        checked={Boolean(platform.requires_signin)}
        disabled={busy}
        onChange={change}
      />
      <span>
        This station needs me to sign in
        {platform.requires_signin ? (
          <span className="muted">
            {" "}
            — untick if its listings are public, and it comes back on line
          </span>
        ) : null}
      </span>
    </label>
  );
}

/**
 * Add a station the agent doesn't ship with.
 *
 * The point is reach: fifteen built-in platforms is a fixed list, and the job
 * you want is often on a board or a company careers page that isn't on it.
 * Anything the generic connector can read becomes a station here.
 */
function AddStationCard({ toast, onAdded }) {
  const blank = { name: "", url: "", requires_signin: false };
  const [form, setForm] = useState(blank);
  const [busy, setBusy] = useState(false);
  // Where the agent decided the jobs actually are. Shown after adding,
  // because a station pointing at the wrong page is only discoverable
  // otherwise by running a search and getting nothing back.
  const [landed, setLanded] = useState(null);

  const set = (key) => (event) =>
    setForm((current) => ({
      ...current,
      [key]:
        event.target.type === "checkbox"
          ? event.target.checked
          : event.target.value,
    }));

  const submit = async (event) => {
    event.preventDefault();
    setBusy(true);
    try {
      const result = await api.addStation(form);
      toast(result.message, "olive");
      setLanded(result.listings_page || null);
      setForm(blank);
      onAdded();
    } catch (error) {
      toast(error.message, "red");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Fold
        title="Add a station"
        hint="Any job board or careers page, by its URL"
        summary="A company board, by URL"
      >
      <form className="blank" onSubmit={submit} style={{ marginBottom: "0.5rem" }}>
        <div className="blank__body stack">
          <div className="fields">
            <label className="field">
              <span className="field__label">Name *</span>
              <input
                className="input"
                required
                value={form.name}
                onChange={set("name")}
                placeholder="Acme careers"
              />
            </label>
            <label className="field">
              <span className="field__label">Company website or board URL *</span>
              <input
                className="input"
                required
                value={form.url}
                onChange={set("url")}
                placeholder="acme.com"
              />
            </label>
          </div>

          <label className="row" style={{ gap: "0.4rem" }}>
            <input
              type="checkbox"
              checked={form.requires_signin}
              onChange={set("requires_signin")}
            />
            <span style={{ fontSize: 13 }}>
              This site needs me to sign in first
            </span>
          </label>

          <Notice tone="olive">
            The company's own address is enough — <code>acme.com</code>. The agent
            follows the site's careers link to the listings, and on to the Greenhouse
            or Lever board behind it if there is one, and tells you where it landed.
            Paste an exact board URL instead if you have one. Put{" "}
            <code>{"{query}"}</code> and <code>{"{location}"}</code> where the site's
            own search terms go and each run fills them in from your search profile.
            Public boards are searched straight away — tick the box above only if the
            listings are behind a login, and you'll be asked to sign in yourself.
          </Notice>

          {landed ? (
            <Notice
              tone={landed.as_given ? "olive" : "amber"}
              title={
                landed.as_given
                  ? "Station points at the URL you gave"
                  : "Found the listings page"
              }
            >
              <div>
                Searching <code>{landed.url}</code>
                {landed.hosted_board ? ` — a ${landed.hosted_board} board` : ""}.
              </div>
              {landed.how ? <div className="muted">{landed.how}.</div> : null}
              {!landed.as_given ? (
                <div className="muted" style={{ marginTop: "0.35rem" }}>
                  If that is the wrong page, remove the station and add it again with
                  the exact listings URL.
                </div>
              ) : null}
            </Notice>
          ) : null}

          <div className="row">
            <span className="spacer" />
            <button className="btn" type="submit" disabled={busy}>
              {busy ? "Adding…" : "Add station"}
            </button>
          </div>
        </div>
      </form>
      </Fold>
    </>
  );
}

function Cell({ on }) {
  return (
    <td style={{ color: on ? "var(--olive)" : "var(--ink-3)" }}>{on ? "yes" : "no"}</td>
  );
}
