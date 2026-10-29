export const LEVEL_NAMES = ["None", "Symptom", "Region", "Evidence", "Concept", "Line"];

type Props = {
  level: number;
  canClimb: boolean;
  busy: boolean;
  onClimb: () => void;
};

/** Levels 0–5; "Need more help" climbs one level. */
export default function HintMeter({ level, canClimb, busy, onClimb }: Props) {
  return (
    <div className="hint-meter" aria-label={`Hint level ${level} of 5`}>
      <div className="hint-meter-head">
        <span className="label">Hint level</span>
        <span className="value">
          {level} · {LEVEL_NAMES[level]}
        </span>
      </div>
      <ol className="hint-steps">
        {LEVEL_NAMES.map((name, n) => (
          <li
            key={name}
            className={n === 0 ? "zero" : n <= level ? "reached" : ""}
            title={`${n}: ${name}`}
          >
            <span>{n === 0 ? "0" : name}</span>
          </li>
        ))}
      </ol>
      <button className="secondary" disabled={!canClimb || busy || level >= 5} onClick={onClimb}>
        {busy ? "Thinking…" : level >= 5 ? "No more hints" : "Need more help"}
      </button>
    </div>
  );
}
