import type { StreamEvent } from "../api";

type Props = { events: StreamEvent[]; running: boolean };

/** The bug hunt's progress, from the WebSocket's progress and error events. */
export default function ProgressLog({ events, running }: Props) {
  const steps = events.filter((e) => e.type === "progress" || e.type === "error");
  return (
    <section className="progress-log" aria-live="polite">
      <h2>{running ? "Testing your code…" : "Testing"}</h2>
      <ol>
        {steps.map((e, i) => {
          const current = running && i === steps.length - 1 && e.type !== "error";
          return (
            <li key={e.seq} className={e.type === "error" ? "error" : current ? "current" : "done"}>
              <span className="dot" aria-hidden>
                {e.type === "error" ? "!" : current ? "" : "✓"}
              </span>
              {e.message}
            </li>
          );
        })}
        {running && steps.length === 0 && (
          <li className="current">
            <span className="dot" aria-hidden />
            Starting…
          </li>
        )}
      </ol>
    </section>
  );
}
