"""Level 3 evidence: the trace of the failing call, windowed to where it goes wrong.

The full trace (testing.harness.trace_call) records the locals before every
line. Here it is cut down to a few steps around the first divergence (the first
point where the user's code holds different values from the verified reference
fix on the same input), or around the diagnosed line when there is no verified
fix, and rendered as a compact table.
"""

from typing import Any

WINDOW_BEFORE = 4
WINDOW_AFTER = 2
MARKER = "◀ goes wrong here"


def divergence(
    trace: dict[str, Any], fix_trace: dict[str, Any] | None
) -> tuple[int, set[str]] | None:
    """The first step where the user's code and the fix hold different values, and
    which variables differ there.

    Both traces are reduced to their distinct states (consecutive steps with the
    same locals collapse into one), so extra lines in the fix that don't change
    anything, like sorting an already sorted list, don't count as divergence.
    Only variables present in both runs are compared.
    """
    if not fix_trace:
        return None
    ours, theirs = _states(trace["steps"]), _states(fix_trace["steps"])
    for (index, a), (_, b) in zip(ours, theirs, strict=False):
        if differ := {k for k in a.keys() & b.keys() if a[k] != b[k]}:
            return index, differ
    if len(ours) > len(theirs):  # we did something the correct run never does
        index, state = ours[len(theirs)]
        before = ours[len(theirs) - 1][1] if theirs else {}
        return index, {k for k, v in state.items() if before.get(k) != v}
    return None


def first_divergence(trace: dict[str, Any], fix_trace: dict[str, Any] | None) -> int | None:
    found = divergence(trace, fix_trace)
    return None if found is None else found[0]


def _states(steps: list[dict[str, Any]]) -> list[tuple[int, dict[str, str]]]:
    states: list[tuple[int, dict[str, str]]] = []
    for index, step in enumerate(steps):
        if not states or states[-1][1] != step["locals"]:
            states.append((index, step["locals"]))
    return states


def window(
    trace: dict[str, Any],
    *,
    anchor: int | None = None,
    line: int | None = None,
    highlight: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """A few steps around `anchor`, else the first visit to `line`, else the end.

    The marked row also shows the `highlight` variables (those that differ from a
    correct run), even if they didn't change at that step.
    """
    steps = trace["steps"]
    if anchor is None and line is not None:
        anchor = next((i for i, s in enumerate(steps) if s["line"] == line), None)
    if anchor is None:
        anchor = max(len(steps) - 1, 0)
    start = max(anchor - WINDOW_BEFORE, 0)
    rows = []
    for index in range(start, min(anchor + WINDOW_AFTER + 1, len(steps))):
        step = steps[index]
        previous = steps[index - 1]["locals"] if index > 0 else {}
        changed = {
            k: v
            for k, v in step["locals"].items()
            if previous.get(k) != v or (index == anchor and k in highlight)
        }
        rows.append(
            {
                "step": index + 1,
                "line": step["line"],
                # The first row shows everything, so the reader knows where it starts.
                "vars": step["locals"] if index == start else changed,
                "marked": index == anchor,
            }
        )
    return {
        "rows": rows,
        "output": trace["output"],
        "exception": trace["exception"],
        "truncated_before": start > 0,
    }


def render(evidence: dict[str, Any], *, show_lines: bool) -> str:
    """Markdown table: step | (line) | values. Line numbers only when `show_lines`."""
    header = "| step | line | values |" if show_lines else "| step | values |"
    divider = "|---|---|---|" if show_lines else "|---|---|"
    lines = [header, divider]
    if evidence["truncated_before"]:
        lines.append("| … | … | … |" if show_lines else "| … | … |")
    for row in evidence["rows"]:
        values = ", ".join(f"{k} = {v}" for k, v in row["vars"].items()) or "(no change)"
        if row["marked"]:
            values += f" {MARKER}"
        cells = (
            [str(row["step"]), str(row["line"]), values]
            if show_lines
            else [
                str(row["step"]),
                values,
            ]
        )
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    if evidence["exception"]:
        lines.append(f"\nIt raises `{evidence['exception']}`.")
    else:
        lines.append(f"\nIt returns `{evidence['output']}`.")
    return "\n".join(lines)
