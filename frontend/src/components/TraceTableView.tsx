import type { TraceTable } from "../api";

/** Level 3 evidence: variable values step by step, the row where it goes wrong marked. */
export default function TraceTableView({ table }: { table: TraceTable }) {
  return (
    <div className="trace">
      <table>
        <thead>
          <tr>
            <th>step</th>
            <th>values (what changed)</th>
          </tr>
        </thead>
        <tbody>
          {table.truncated_before && (
            <tr className="ellipsis">
              <td>…</td>
              <td>…</td>
            </tr>
          )}
          {table.rows.map((row) => (
            <tr key={row.step} className={row.marked ? "marked" : ""}>
              <td>{row.step}</td>
              <td>
                {Object.keys(row.vars).length === 0 ? (
                  <span className="muted">(no change)</span>
                ) : (
                  Object.entries(row.vars).map(([name, value]) => (
                    <span key={name} className="var">
                      <code>
                        {name} = {value}
                      </code>
                    </span>
                  ))
                )}
                {row.marked && <span className="marker">◀ goes wrong here</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="small">
        {table.exception ? (
          <>
            It raises <code>{table.exception}</code>.
          </>
        ) : (
          <>
            It returns <code>{table.output}</code>.
          </>
        )}
      </p>
    </div>
  );
}
