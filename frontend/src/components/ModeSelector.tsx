import type { Mode } from "../api";

const MODES: { value: Mode; label: string; disabled?: boolean; note?: string }[] = [
  { value: "debug", label: "Debug" },
  { value: "review", label: "AI Code Review" },
  { value: "explore", label: "Explore", disabled: true, note: "coming later" },
];

type Props = { value: Mode; onChange: (mode: Mode) => void; locked: boolean };

export default function ModeSelector({ value, onChange, locked }: Props) {
  return (
    <div className="mode-selector" role="radiogroup" aria-label="Mode">
      {MODES.map((m) => (
        <button
          key={m.value}
          role="radio"
          aria-checked={value === m.value}
          className={value === m.value ? "active" : ""}
          disabled={m.disabled || (locked && value !== m.value)}
          title={m.note}
          onClick={() => onChange(m.value)}
        >
          {m.label}
          {m.note && <small> ({m.note})</small>}
        </button>
      ))}
    </div>
  );
}
