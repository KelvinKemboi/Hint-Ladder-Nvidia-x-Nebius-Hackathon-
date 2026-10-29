type Props = { checked: number; total: number };

/** Live while a fix is being verified: "verified on N/500 fresh inputs". */
export default function VerifyCounter({ checked, total }: Props) {
  const pct = total ? Math.round((checked / total) * 100) : 0;
  return (
    <div className="verify" aria-live="polite">
      <div className="verify-text">
        {total ? (
          <>
            Verified on <strong>{checked}</strong>/{total} fresh inputs
            <span className="muted small"> (per property)</span>
          </>
        ) : (
          "Generating fresh inputs for your fix…"
        )}
      </div>
      <div className="bar" role="progressbar" aria-valuenow={checked} aria-valuemin={0} aria-valuemax={total}>
        <div style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}
