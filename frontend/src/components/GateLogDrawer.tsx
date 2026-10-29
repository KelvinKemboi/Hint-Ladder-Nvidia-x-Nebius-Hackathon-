import { useEffect, useState } from "react";

import { api, ApiError, type GateLogEntry } from "../api";

type Props = { sessionId: string | null; open: boolean; refreshKey: number; onClose: () => void };

/** Demo only: every gate decision, including the blocked text and why. */
export default function GateLogDrawer({ sessionId, open, refreshKey, onClose }: Props) {
  const [entries, setEntries] = useState<GateLogEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [blockedOnly, setBlockedOnly] = useState(false);

  useEffect(() => {
    if (!open || !sessionId) return;
    api
      .gateLog(sessionId)
      .then((log) => {
        setEntries(log);
        setError(null);
      })
      .catch((e: Error) =>
        setError(
          e instanceof ApiError && e.status === 404
            ? "The gate log is off. Start the backend with EXPOSE_GATE_LOG=true to see it."
            : e.message,
        ),
      );
  }, [open, sessionId, refreshKey]);

  const shown = (entries ?? []).filter((e) => !blockedOnly || !e.allowed).reverse();
  const blocked = (entries ?? []).filter((e) => !e.allowed).length;

  return (
    <aside className={`drawer${open ? " open" : ""}`} aria-hidden={!open} inert={!open}>
      <header>
        <h2>Gate log</h2>
        <button className="icon" aria-label="Close gate log" onClick={onClose}>
          ×
        </button>
      </header>
      <p className="muted small">
        Every message checked before it reached you. Blocked ones never left the server. (Demo
        only: blocked text can contain the answer.)
      </p>
      {!sessionId && <p className="muted">Start a session first.</p>}
      {error && <p className="error-text">{error}</p>}
      {entries && (
        <>
          <label className="small">
            <input type="checkbox" checked={blockedOnly} onChange={(e) => setBlockedOnly(e.target.checked)} />{" "}
            Blocked only ({blocked} of {entries.length})
          </label>
          <ol className="gate-entries">
            {shown.map((e, i) => (
              <li key={i} className={e.allowed ? "allowed" : "blocked"}>
                <div className="entry-head">
                  <span className="tag">{e.allowed ? "allowed" : "blocked"}</span>
                  <span className="tag">{e.kind}</span>
                  <span className="tag">level {e.level}</span>
                  {!e.allowed && (
                    <span className="tag">
                      {e.layer}
                      {e.rule ? `: ${e.rule}` : ""}
                    </span>
                  )}
                </div>
                {!e.allowed && <p className="reason">{e.reason}</p>}
                <pre>{e.text}</pre>
              </li>
            ))}
          </ol>
        </>
      )}
    </aside>
  );
}
