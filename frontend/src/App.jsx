import { useCallback, useEffect, useState } from "react";
import { api } from "./api.js";
import { Toasts, useAsync } from "./components.jsx";
import OutgoingTray from "./views/OutgoingTray.jsx";
import Wire from "./views/Wire.jsx";
import Dispatch from "./views/Dispatch.jsx";
import Stations from "./views/Stations.jsx";
import Desk from "./views/Desk.jsx";
import Register from "./views/Register.jsx";

const TABS = [
  ["tray", "Outgoing tray"],
  ["wire", "The wire"],
  ["dispatch", "Dispatch"],
  ["stations", "Stations"],
  ["desk", "The desk"],
  ["register", "Register"],
];

export default function App() {
  const [tab, setTab] = useState(() => window.location.hash.slice(1) || "tray");
  const [toasts, setToasts] = useState([]);

  const health = useAsync(() => api.health());
  const queue = useAsync(() => api.queue("queued_for_review"));
  const drafts = useAsync(() => api.drafts("draft"));

  useEffect(() => {
    const onHash = () => setTab(window.location.hash.slice(1) || "tray");
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const navigate = useCallback((next) => {
    window.location.hash = next;
    setTab(next);
    window.scrollTo({ top: 0 });
  }, []);

  const toast = useCallback((message, tone = "olive") => {
    const id = Math.random().toString(36).slice(2);
    setToasts((current) => [...current, { id, message, tone }]);
    // A completed action changes the counts in the masthead
    health.reload();
    queue.reload();
    drafts.reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const dismiss = useCallback(
    (id) => setToasts((current) => current.filter((toastItem) => toastItem.id !== id)),
    [],
  );

  const waiting = queue.data?.total ?? 0;
  const needsYou = health.data?.needs_attention ?? 0;
  const unsent = drafts.data?.awaiting_review ?? 0;

  const props = { toast, onNavigate: navigate };

  return (
    <div className="shell">
      <header className="masthead">
        <div className="masthead__row">
          <span className="wordmark">
            Job Agent <span>·</span> Operator's Desk
          </span>

          <div className="masthead__meta">
            <span>
              waiting on you <b>{waiting}</b>
            </span>
            <span>
              stations off line <b style={needsYou ? { color: "var(--red)" } : undefined}>{needsYou}</b>
            </span>
            <span>
              nothing sends itself
            </span>
          </div>
        </div>

        <nav className="tabs" aria-label="Sections">
          {TABS.map(([key, label]) => {
            const count =
              key === "tray" ? waiting : key === "dispatch" ? unsent : key === "stations" ? needsYou : 0;

            return (
              <button
                key={key}
                className="tab"
                aria-current={tab === key ? "page" : undefined}
                onClick={() => navigate(key)}
              >
                {label}
                {count > 0 ? (
                  <span
                    className="tab__count"
                    data-tone={key === "stations" ? "red" : undefined}
                  >
                    {count}
                  </span>
                ) : null}
              </button>
            );
          })}
        </nav>
      </header>

      <main className="main">
        {tab === "tray" ? <OutgoingTray {...props} /> : null}
        {tab === "wire" ? <Wire {...props} /> : null}
        {tab === "dispatch" ? <Dispatch {...props} /> : null}
        {tab === "stations" ? <Stations {...props} /> : null}
        {tab === "desk" ? <Desk {...props} /> : null}
        {tab === "register" ? <Register {...props} /> : null}
      </main>

      <Toasts items={toasts} onDismiss={dismiss} />
    </div>
  );
}
