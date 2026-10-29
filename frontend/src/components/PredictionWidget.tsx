import { useState } from "react";

type Props = {
  question: string | null;
  busy: boolean;
  error: string | null;
  onSubmit: (answer: string) => void;
};

/** "What should f(x) return?" and a box for the answer. */
export default function PredictionWidget({ question, busy, error, onSubmit }: Props) {
  const [answer, setAnswer] = useState("");
  const submit = () => {
    if (answer.trim()) onSubmit(answer.trim());
  };
  return (
    <form
      className="prediction"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <label htmlFor="prediction">
        <RichText text={question ?? "Loading the question…"} />
      </label>
      <div className="row">
        <input
          id="prediction"
          value={answer}
          placeholder="e.g. [[1, 5]]   or   raises ValueError"
          autoComplete="off"
          spellCheck={false}
          disabled={busy}
          onChange={(e) => setAnswer(e.target.value)}
        />
        <button disabled={busy || !answer.trim()}>{busy ? "Running…" : "Predict"}</button>
      </div>
      <p className="muted small">
        Work it out from your spec, not from the code. Any Python value works.
      </p>
      {error && <p className="error-text">{error}</p>}
    </form>
  );
}

/** Plain text with `code` spans and line breaks. */
export function RichText({ text }: { text: string }) {
  return (
    <span className="rich">
      {text.split(/(`[^`]+`)/).map((part, i) =>
        part.startsWith("`") && part.endsWith("`") && part.length > 1 ? (
          <code key={i}>{part.slice(1, -1)}</code>
        ) : (
          <span key={i}>{part}</span>
        ),
      )}
    </span>
  );
}
