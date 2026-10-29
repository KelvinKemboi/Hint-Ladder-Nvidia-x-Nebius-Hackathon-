import { useState } from "react";

import type { Property } from "../api";

type Card = { key: number; id: string | null; statement: string; predicate: string | null };

type Props = {
  properties: Property[];
  errors: Record<string, string>; // statement -> why it couldn't be checked
  busy: boolean;
  onConfirm: (properties: { id: string | null; statement: string }[]) => void;
};

let nextKey = 0;

/** The proposed properties as editable cards: edit, add, remove, then confirm. */
export default function PropertyReview({ properties, errors, busy, onConfirm }: Props) {
  const [cards, setCards] = useState<Card[]>(() =>
    properties.map((p) => ({ key: nextKey++, ...p })),
  );

  const update = (key: number, statement: string) =>
    setCards((cs) => cs.map((c) => (c.key === key ? { ...c, statement } : c)));
  const remove = (key: number) => setCards((cs) => cs.filter((c) => c.key !== key));
  const add = () =>
    setCards((cs) => [...cs, { key: nextKey++, id: null, statement: "", predicate: null }]);

  const filled = cards.filter((c) => c.statement.trim().length >= 3);
  const general = errors[""];

  return (
    <section className="property-review">
      <h2>Is this what “correct” means?</h2>
      <p className="muted">
        These are the rules your code will be tested against. Edit them in your own words,
        remove any that are wrong, or add what's missing.
      </p>
      <ul className="cards">
        {cards.map((card, i) => {
          const error = errors[card.statement.trim()];
          const edited = card.id === null || properties.find((p) => p.id === card.id)?.statement !== card.statement;
          return (
            <li key={card.key} className={`card${error ? " invalid" : ""}`}>
              <div className="card-head">
                <span className="tag">Property {i + 1}</span>
                {edited && <span className="tag edited">{card.id ? "edited" : "new"}</span>}
                <button
                  className="icon"
                  aria-label={`Remove property ${i + 1}`}
                  disabled={busy || cards.length <= 1}
                  onClick={() => remove(card.key)}
                >
                  ×
                </button>
              </div>
              <textarea
                rows={2}
                value={card.statement}
                placeholder="e.g. No two intervals in the output overlap."
                disabled={busy}
                onChange={(e) => update(card.key, e.target.value)}
              />
              {error && <p className="error-text">{error}</p>}
              {card.predicate && !edited && (
                <details>
                  <summary>How it's checked</summary>
                  <pre>{card.predicate}</pre>
                </details>
              )}
            </li>
          );
        })}
      </ul>
      {general && <p className="error-text">{general}</p>}
      <div className="row">
        <button className="secondary" disabled={busy || cards.length >= 10} onClick={add}>
          + Add property
        </button>
        <button
          disabled={busy || filled.length === 0}
          onClick={() => onConfirm(filled.map((c) => ({ id: c.id, statement: c.statement.trim() })))}
        >
          {busy ? "Writing checks…" : "Confirm and start testing"}
        </button>
      </div>
    </section>
  );
}
