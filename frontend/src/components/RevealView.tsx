import type { ShownValue } from "../api";
import { diff, type Piece } from "../diff";

type Props = { call: string; predicted: ShownValue; returned: ShownValue; outcome: string };

/** Side by side: what you predicted vs what the code returned, differences highlighted. */
export default function RevealView({ call, predicted, returned, outcome }: Props) {
  const [left, right] =
    predicted.kind === returned.kind
      ? diff(predicted.text, returned.text)
      : [[{ text: predicted.text, changed: true }], [{ text: returned.text, changed: true }]];
  return (
    <figure className={`reveal ${outcome}`}>
      <figcaption>
        <code>{call}</code>
      </figcaption>
      <div className="sides">
        <Side title="You predicted" value={predicted} pieces={left} />
        <Side title="The code returned" value={returned} pieces={right} />
      </div>
    </figure>
  );
}

function Side({ title, value, pieces }: { title: string; value: ShownValue; pieces: Piece[] }) {
  return (
    <div className="side">
      <span className="side-title">{title}</span>
      <pre className={value.kind === "error" ? "is-error" : ""}>
        {value.kind === "error" && <span className="err-tag">raises </span>}
        {pieces.map((p, i) =>
          p.changed ? (
            <mark key={i}>{p.text}</mark>
          ) : (
            <span key={i}>{p.text}</span>
          ),
        )}
      </pre>
    </div>
  );
}
