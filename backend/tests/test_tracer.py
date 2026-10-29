import ast

import pytest
from fakes import FIXED_MERGE, MERGE_CODE, FakeLLM

from sparring_partner.agents import tracer
from sparring_partner.agents.diagnosis import Diagnoser
from sparring_partner.config import Settings
from sparring_partner.sandbox import LocalBackend, Sandbox
from sparring_partner.session import Session

TOUCHING = {"args": ["[[0, 0], [0, 0]]"], "call": "merge_intervals([[0, 0], [0, 0]])"}


def steps(*states):
    return {
        "steps": [{"line": line, "function": "f", "locals": loc} for line, loc in states],
        "truncated": False,
        "output": "1",
        "exception": None,
    }


def test_first_divergence_compares_distinct_states():
    ours = steps((1, {"x": "1"}), (2, {"x": "1"}), (3, {"x": "2"}), (4, {"x": "5"}))
    # The fix has an extra no-op line (2) and goes on to a different value.
    fix = steps((1, {"x": "1"}), (2, {"x": "1"}), (3, {"x": "1"}), (4, {"x": "2"}), (5, {"x": "3"}))
    assert tracer.first_divergence(ours, fix) == 3  # x=5 where the fix has x=3
    assert tracer.first_divergence(ours, ours) is None
    assert tracer.first_divergence(ours, None) is None


def test_first_divergence_when_our_run_does_extra_work():
    ours = steps((1, {"x": "1"}), (2, {"x": "2"}))
    fix = steps((1, {"x": "1"}))
    assert tracer.first_divergence(ours, fix) == 1


def test_variables_only_in_one_run_are_ignored():
    ours = steps((1, {"x": "1"}), (2, {"x": "1", "tmp": "9"}))
    fix = steps((1, {"x": "1"}), (2, {"x": "1", "other": "3"}))
    assert tracer.first_divergence(ours, fix) is None


def test_window_and_render():
    trace = steps(*[(n, {"i": str(n), "total": str(n * 10)}) for n in range(1, 11)])
    evidence = tracer.window(trace, anchor=6)

    assert [r["step"] for r in evidence["rows"]] == [3, 4, 5, 6, 7, 8, 9]
    assert evidence["rows"][0]["vars"] == {"i": "3", "total": "30"}  # first row: everything
    assert evidence["truncated_before"]

    hidden = tracer.render(evidence, show_lines=False)
    assert hidden.splitlines()[0] == "| step | values |"
    assert "| 7 | i = 7, total = 70 ◀ goes wrong here |" in hidden
    assert "It returns `1`." in hidden
    shown = tracer.render(evidence, show_lines=True)
    assert shown.splitlines()[0] == "| step | line | values |"
    assert "| 7 | 7 | i = 7, total = 70 ◀ goes wrong here |" in shown


def test_window_falls_back_to_the_diagnosed_line_then_the_end():
    trace = steps((1, {"a": "1"}), (5, {"a": "2"}), (9, {"a": "3"}))
    assert [r["marked"] for r in tracer.window(trace, line=5)["rows"]] == [False, True, False]
    assert tracer.window(trace)["rows"][-1]["marked"]


@pytest.mark.anyio
async def test_real_trace_diverges_where_touching_intervals_stay_apart():
    sandbox = Sandbox(LocalBackend(), Settings(_env_file=None))
    diagnoser = Diagnoser(FakeLLM(), sandbox)
    session = Session(code=MERGE_CODE, intent="merge", function="merge_intervals")
    session.counterexamples = [TOUCHING]

    ours = await diagnoser.trace(session)
    fix = await diagnoser.trace(session, code=FIXED_MERGE)
    anchor = tracer.first_divergence(ours, fix)
    evidence = tracer.window(ours, anchor=anchor)

    marked = next(r for r in evidence["rows"] if r["marked"])
    assert ast.literal_eval(marked["vars"]["merged"]) == [[0, 0], [0, 0]]  # appended, not merged


def test_marked_row_shows_the_variables_that_differ_from_a_correct_run():
    ours = steps((1, {"xs": "[2, 1]"}), (2, {"xs": "[2, 1]"}), (3, {"xs": "[2, 1]", "n": "2"}))
    fix = steps((1, {"xs": "[2, 1]"}), (2, {"xs": "[1, 2]"}), (3, {"xs": "[1, 2]", "n": "1"}))
    index, differ = tracer.divergence(ours, fix)
    assert (index, differ) == (2, {"xs"})  # n only exists in our run at this point

    rows = tracer.window(ours, anchor=index, highlight=differ)["rows"]
    marked = next(r for r in rows if r["marked"])
    assert marked["vars"] == {"xs": "[2, 1]", "n": "2"}  # xs didn't change, but it's wrong
