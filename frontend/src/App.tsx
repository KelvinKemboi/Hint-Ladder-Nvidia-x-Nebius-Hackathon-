import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  api,
  ApiError,
  subscribe,
  type Mode,
  type Postmortem,
  type Session,
  type StreamEvent,
} from "./api";
import Chat from "./components/Chat";
import GateLogDrawer from "./components/GateLogDrawer";
import HintMeter from "./components/HintMeter";
import ModeSelector from "./components/ModeSelector";
import PostmortemCard from "./components/PostmortemCard";
import PredictionWidget from "./components/PredictionWidget";
import ProgressLog from "./components/ProgressLog";
import PropertyReview from "./components/PropertyReview";
import VerifyCounter from "./components/VerifyCounter";

// Monaco is large: load it in its own chunk, after the page shell.
const CodeEditor = lazy(() => import("./components/CodeEditor"));

const DEMO_CODE = `def merge_intervals(intervals):
    """Merge overlapping intervals."""
    if not intervals:
        return []
    merged = [list(intervals[0])]
    for start, end in intervals[1:]:
        if start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged
`;
const DEMO_INTENT = "merge overlapping intervals";

type Busy = null | "create" | "confirm" | "predict" | "hint" | "fix";

export default function App() {
  const dark = usePrefersDark();
  const [mode, setMode] = useState<Mode>("debug");
  const [intent, setIntent] = useState(DEMO_INTENT);
  const [code, setCode] = useState(DEMO_CODE);

  const [session, setSession] = useState<Session | null>(null);
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [busy, setBusy] = useState<Busy>(null);
  const [error, setError] = useState<string | null>(null);
  const [propertyErrors, setPropertyErrors] = useState<Record<string, string>>({});
  const [reviewAttempt, setReviewAttempt] = useState(0);
  const [question, setQuestion] = useState<string | null>(null);
  const [predictError, setPredictError] = useState<string | null>(null);
  const [verify, setVerify] = useState<{ checked: number; total: number } | null>(null);
  const [postmortem, setPostmortem] = useState<Postmortem | null>(null);
  const [gateOpen, setGateOpen] = useState(false);

  const id = session?.id ?? null;
  const state = session?.state ?? null;

  const refresh = useCallback(async () => {
    if (!id) return;
    try {
      setSession(await api.get(id));
    } catch (e) {
      setError((e as Error).message);
    }
  }, [id]);

  // The session id lives in the URL hash, so a reload or a shared link opens that session.
  const idRef = useRef(id);
  useEffect(() => {
    const open = () => {
      const saved = location.hash.slice(1);
      if (!saved || saved === idRef.current) return;
      setSession(null);
      clear();
      api
        .get(saved)
        .then((opened) => {
          setSession(opened);
          setCode(opened.code);
          setIntent(opened.intent);
          setMode(opened.mode);
        })
        .catch(() => history.replaceState(null, "", " "));
    };
    open();
    window.addEventListener("hashchange", open);
    return () => window.removeEventListener("hashchange", open);
  }, []);
  useEffect(() => {
    idRef.current = id;
    if (id) history.replaceState(null, "", `#${id}`);
  }, [id]);

  // Progress events over the WebSocket; any state change or new message refreshes the session.
  useEffect(() => {
    if (!id) return;
    return subscribe(id, (event) => {
      setEvents((es) => (es.some((e) => e.seq === event.seq) ? es : [...es, event]));
      if (event.type === "progress" && event.data.phase === "verify") {
        setVerify({ checked: Number(event.data.checked), total: Number(event.data.total) });
      }
      if (event.type !== "progress") void refresh();
    });
  }, [id, refresh]);

  // The prediction question for the current failing case (asking again is a no-op).
  const questions = session?.messages.filter((m) => m.kind === "question").length ?? 0;
  useEffect(() => {
    if (!id || (state !== "predicting" && state !== "hinting")) return;
    api
      .predictionQuestion(id)
      .then((q) => setQuestion(q.question))
      .catch(() => setQuestion(null));
  }, [id, state, questions]);

  useEffect(() => {
    if (!id || postmortem || !(session?.has_postmortem || state === "postmortem")) return;
    api.postmortem(id).then(setPostmortem).catch(() => undefined);
  }, [id, state, session?.has_postmortem, postmortem]);

  const run = async (kind: Busy, action: () => Promise<void>) => {
    setBusy(kind);
    setError(null);
    try {
      await action();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const start = () =>
    run("create", async () => {
      reset();
      setSession(await api.create(code, intent, mode));
    });

  const confirm = (properties: { id: string | null; statement: string }[]) =>
    run("confirm", async () => {
      setPropertyErrors({});
      try {
        setSession(await api.confirmProperties(id!, properties));
      } catch (e) {
        const detail = e instanceof ApiError ? (e.detail as { errors?: Record<string, string> }) : null;
        if (detail?.errors) setPropertyErrors(detail.errors);
        throw e;
      }
    });

  const predict = (answer: string) =>
    run("predict", async () => {
      setPredictError(null);
      try {
        await api.predict(id!, answer);
      } catch (e) {
        if (e instanceof ApiError && e.status === 422) {
          setPredictError(e.message);
          return;
        }
        throw e;
      }
      await refresh();
    });

  const climb = () =>
    run("hint", async () => {
      await api.hint(id!);
      await refresh();
    });

  const submitFix = () =>
    run("fix", async () => {
      setVerify(null);
      try {
        const result = await api.fix(id!, code);
        if (result.outcome === "passed") setPostmortem(null); // a new one is written
      } finally {
        setVerify(null);
      }
      await refresh();
    });

  function reset() {
    history.replaceState(null, "", " ");
    setSession(null);
    clear();
  }

  /** Everything that belongs to one session, apart from the session itself. */
  function clear() {
    setEvents([]);
    setQuestion(null);
    setPostmortem(null);
    setPropertyErrors({});
    setVerify(null);
    setPredictError(null);
    setReviewAttempt((n) => n + 1);
  }

  // The bug hunt failed (an error after it started, and back to properties_confirmed).
  const huntFailed = useMemo(() => {
    if (state !== "properties_confirmed") return false;
    const started = events.findLastIndex((e) => e.type === "state" && e.data.state === "exploring");
    return started >= 0 && events.slice(started).some((e) => e.type === "error");
  }, [state, events]);

  const inLoop = state === "predicting" || state === "hinting";
  // From pressing Submit fix until the result is in (the state event can lag the click).
  const verifying = state === "verifying" || (busy === "fix" && inLoop);
  const lastHint = session?.messages.filter((m) => m.kind === "hint").at(-1);
  const highlightLine =
    inLoop && lastHint?.level === 5 ? Number(/line (\d+)/i.exec(lastHint.text)?.[1]) || null : null;
  // The post-mortem goes right after the message saying the fix passed.
  const messages = session?.messages ?? [];
  const passedAt = messages.findLastIndex(
    (m) => m.kind === "fix_result" && m.data?.outcome === "passed",
  );
  const split = passedAt >= 0 ? passedAt + 1 : messages.length;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <strong>Sparring Partner</strong>
          <span className="muted small">knows the answer, won't tell you</span>
        </div>
        <ModeSelector value={mode} onChange={setMode} locked={session !== null} />
        <div className="spacer" />
        <button className="ghost" onClick={() => setGateOpen((o) => !o)}>
          Gate log
        </button>
        {session && (
          <button className="ghost" disabled={busy !== null} onClick={reset}>
            New session
          </button>
        )}
      </header>

      <main className="workspace">
        <section className="pane left">
          <label className="intent">
            <span>What should this code do?</span>
            <input
              value={intent}
              maxLength={500}
              placeholder="e.g. merge overlapping intervals"
              disabled={session !== null}
              onChange={(e) => setIntent(e.target.value)}
            />
          </label>
          <div className="editor">
            <Suspense fallback={<pre className="editor-loading">{code}</pre>}>
              <CodeEditor
                value={code}
                onChange={setCode}
                dark={dark}
                readOnly={busy === "create" || busy === "fix" || state === "verifying"}
                highlightLine={highlightLine}
              />
            </Suspense>
          </div>
          <footer className="editor-bar">
            {session ? (
              <>
                <span className="muted small">
                  Testing <code>{session.function}()</code>
                  {session.failures_found > 0 && ` · ${session.failures_found} failure(s) found`}
                </span>
                <button disabled={!inLoop || busy !== null} onClick={submitFix}>
                  {verifying ? "Verifying…" : busy === "fix" ? "Wrapping up…" : "Submit fix"}
                </button>
              </>
            ) : (
              <>
                <span className="muted small">
                  {mode === "review" ? "Paste AI-written code to review." : "Paste your Python code."}
                </span>
                <button
                  disabled={busy !== null || intent.trim().length < 3 || !code.trim()}
                  onClick={start}
                >
                  {busy === "create" ? "Proposing properties…" : "Start"}
                </button>
              </>
            )}
          </footer>
        </section>

        <section className="pane right">
          {session && (
            <HintMeter
              level={session.hint_level}
              canClimb={inLoop}
              busy={busy === "hint"}
              onClimb={climb}
            />
          )}
          {error && (
            <div className="banner error" role="alert">
              {error}
              <button className="icon" aria-label="Dismiss" onClick={() => setError(null)}>
                ×
              </button>
            </div>
          )}

          <div className="panel-body">
            {!session && busy !== "create" && <Welcome mode={mode} />}
            {!session && busy === "create" && (
              <ProgressLog
                running
                events={[
                  {
                    seq: 0,
                    type: "progress",
                    message: "Reading your code and proposing properties (this can take a minute)",
                    data: {},
                    at: 0,
                  },
                ]}
              />
            )}

            {session && (state === "intent" || huntFailed) && (
              <PropertyReview
                key={`${session.id}-${reviewAttempt}`}
                properties={session.properties}
                errors={propertyErrors}
                busy={busy === "confirm"}
                onConfirm={confirm}
              />
            )}

            {session && !huntFailed && (state === "properties_confirmed" || state === "exploring") && (
              <ProgressLog events={events} running />
            )}

            {session && !["intent", "properties_confirmed", "exploring"].includes(state!) && (
              <>
                <details className="run-summary">
                  <summary>How your code was tested</summary>
                  <ProgressLog events={events.filter((e) => e.data.phase !== "verify")} running={false} />
                </details>
                <Chat messages={messages.slice(0, split)} />
                {postmortem && <PostmortemCard pm={postmortem} />}
                {split < messages.length && <Chat messages={messages.slice(split)} />}
              </>
            )}
          </div>

          {verifying && (
            <VerifyCounter checked={verify?.checked ?? 0} total={verify?.total ?? 0} />
          )}
          {!verifying && busy === "fix" && (
            <p className="next-step muted small">Fix verified. Writing your post-mortem…</p>
          )}
          {state === "predicting" && !verifying && (
            <PredictionWidget
              key={question ?? ""}
              question={question}
              busy={busy === "predict"}
              error={predictError}
              onSubmit={predict}
            />
          )}
          {state === "hinting" && (
            <p className="next-step muted small">
              Found it? Edit the code on the left and press <strong>Submit fix</strong>. Stuck?
              Press <strong>Need more help</strong>.
            </p>
          )}
        </section>
      </main>

      <GateLogDrawer
        sessionId={id}
        open={gateOpen}
        refreshKey={session?.messages.length ?? 0}
        onClose={() => setGateOpen(false)}
      />
    </div>
  );
}

function Welcome({ mode }: { mode: Mode }) {
  return (
    <section className="welcome">
      <h2>{mode === "review" ? "Review AI-written code" : "Find the bug yourself"}</h2>
      <ol>
        <li>Say what the code should do, paste it on the left, and press Start.</li>
        <li>Check the properties that define “correct”. Your code is then tested against them.</li>
        <li>
          Predict what the failing call should return. If you're stuck, climb the hint ladder one
          level at a time.
        </li>
        <li>Fix it in the editor. The fix is checked on hundreds of fresh inputs.</li>
      </ol>
      <p className="muted small">
        {mode === "review"
          ? "Review mode also looks for invented APIs, swallowed exceptions and wrong input assumptions, and asks about them as questions."
          : "The coach knows where the bug is and how to fix it. It won't say."}
      </p>
    </section>
  );
}

function usePrefersDark(): boolean {
  const query = "(prefers-color-scheme: dark)";
  const [dark, setDark] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const mq = window.matchMedia(query);
    const on = (e: MediaQueryListEvent) => setDark(e.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return dark;
}
