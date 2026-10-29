import { useEffect, useRef, type ReactNode } from "react";

import type { Message } from "../api";
import { LEVEL_NAMES } from "./HintMeter";
import { RichText } from "./PredictionWidget";
import RevealView from "./RevealView";
import TraceTableView from "./TraceTableView";

const FIX_OUTCOME: Record<string, string> = {
  passed: "Fix verified",
  failed: "Not fixed yet",
  special_cased: "Special-cased",
  invalid: "Can't check",
};

const KIND_LABEL: Record<string, string> = {
  review: "Review question",
  risk: "Next risk",
};

/** The coaching thread: questions, predictions, reveals, hints and fix results. */
export default function Chat({ messages, children }: { messages: Message[]; children?: ReactNode }) {
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length]);

  return (
    <div className="chat">
      {messages.map((m, i) => (
        <Bubble key={i} message={m} />
      ))}
      {children}
      <div ref={end} />
    </div>
  );
}

function Bubble({ message: m }: { message: Message }) {
  const d = m.data;
  if (m.role === "user") {
    return (
      <div className={`bubble user ${m.kind}`}>
        {m.kind === "prediction" ? <code>{m.text}</code> : <RichText text={m.text} />}
      </div>
    );
  }
  const fixOutcome = (m.kind === "fix_result" || m.kind === "question") && d?.outcome;
  return (
    <div className={`bubble coach ${m.kind}${fixOutcome ? ` fix-${d.outcome}` : ""}`}>
      {m.kind === "hint" && (
        <span className="badge">
          Hint {m.level} · {LEVEL_NAMES[m.level]}
        </span>
      )}
      {KIND_LABEL[m.kind] && <span className="badge">{KIND_LABEL[m.kind]}</span>}
      {fixOutcome && (
        <span className="badge">
          {FIX_OUTCOME[d.outcome!] ?? d.outcome}
          {d.inputs_checked ? ` · ${d.inputs_checked.toLocaleString()} inputs` : ""}
        </span>
      )}
      {d?.predicted && d.returned && d.call && (
        <RevealView call={d.call} predicted={d.predicted} returned={d.returned} outcome={d.outcome ?? ""} />
      )}
      {d?.table ? (
        <>
          <p>
            <RichText text={d.prose ?? ""} />
          </p>
          <TraceTableView table={d.table} />
        </>
      ) : (
        <p>
          <RichText text={m.text} />
        </p>
      )}
    </div>
  );
}
