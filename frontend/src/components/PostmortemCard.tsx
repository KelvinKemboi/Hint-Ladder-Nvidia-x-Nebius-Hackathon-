import type { Postmortem } from "../api";
import { LEVEL_NAMES } from "./HintMeter";

/** After a verified fix: the bug class, how to spot it, what to try next. */
export default function PostmortemCard({ pm }: { pm: Postmortem }) {
  return (
    <section className="postmortem">
      <h2>Post-mortem</h2>
      {pm.bug_class && <span className="bug-class">{pm.bug_class}</span>}
      <dl className="stats">
        <div>
          <dt>Hint level reached</dt>
          <dd>
            {pm.hint_level_reached} · {LEVEL_NAMES[pm.hint_level_reached]}
          </dd>
        </div>
        <div>
          <dt>Fix attempts</dt>
          <dd>{pm.fix_attempts}</dd>
        </div>
        <div>
          <dt>Inputs checked</dt>
          <dd>{pm.inputs_checked.toLocaleString()}</dd>
        </div>
      </dl>
      <h3>How to spot it next time</h3>
      <p>{pm.how_to_spot}</p>
      {pm.cases.length > 0 && (
        <>
          <h3>Bugs you fixed</h3>
          <ul>
            {pm.cases.map((c, i) => (
              <li key={i}>
                <code>{c.call}</code> {c.bug_class && <>({c.bug_class})</>}, hints used:{" "}
                {c.hint_level}
              </li>
            ))}
          </ul>
        </>
      )}
      {pm.other_edge_cases.length > 0 && (
        <>
          <h3>Other edge cases to think about</h3>
          <ul>
            {pm.other_edge_cases.map((e, i) => (
              <li key={i}>{e}</li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
