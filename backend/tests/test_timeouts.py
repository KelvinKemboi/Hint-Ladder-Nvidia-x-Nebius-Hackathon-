"""Code that never finishes: demo bug 2, a binary search whose `lo = mid` can stop
moving. A hang is a symptom to predict, not a crash of the bug hunt."""

import time
from pathlib import Path

from fakes import FakeLLM, prop
from hypothesis import strategies as st
from test_api import wait_for_state

from sparring_partner.testing.capture import find_counterexamples
from sparring_partner.testing.counterexamples import Property

SEARCH_CODE = (Path(__file__).parents[2] / "demo" / "binary_search.py").read_text()
FIXED_SEARCH = SEARCH_CODE.replace("            lo = mid\n", "            lo = mid + 1\n")

FINDS_TARGET = (
    "if the result isn't -1, nums[result] is the target",
    "def check(args, output):\n"
    "    nums, target = args\n"
    "    return output == -1 or (0 <= output < len(nums) and nums[output] == target)\n",
)
MINUS_ONE_ONLY_IF_ABSENT = (
    "the result is -1 only when the target isn't in nums",
    "def check(args, output):\n    return output != -1 or args[1] not in args[0]\n",
)
RETURNS_INT = (
    "the result is an int",
    "def check(args, output):\n    return isinstance(output, int)\n",
)
SEARCH_STRATEGY = (
    "def strategy():\n"
    "    return st.tuples(st.lists(st.integers(-10, 10), max_size=8).map(sorted), "
    "st.integers(-10, 10))\n"
)
INPUT_SPEC = {
    "params": [
        {"name": "nums", "kind": "sorted_list_int", "description": "sorted list"},
        {"name": "target", "kind": "int", "description": "value to find"},
    ],
    "examples": [
        {"args": "([1, 3, 5], 3)", "output": "1"},
        {"args": "([], 4)", "output": "-1"},
    ],
}
DIAGNOSIS = {
    "root_cause": "SECRET: lo = mid never moves past mid when lo == mid",
    "line": 9,
    "region": "the while loop",
    "bug_class": "off-by-one",
    "concept": "SECRET: each step must shrink the search range",
    "reference_fix": FIXED_SEARCH,
}


def properties() -> list[Property]:
    from sparring_partner.testing.harness import load_predicate

    return [
        Property(s, load_predicate(src))
        for s, src in (FINDS_TARGET, MINUS_ONE_ONLY_IF_ABSENT, RETURNS_INT)
    ]


def search_llm() -> FakeLLM:
    return FakeLLM(
        InputSpecDraft=[INPUT_SPEC],
        Proposal=[
            {"properties": [prop(FINDS_TARGET), prop(MINUS_ONE_ONLY_IF_ABSENT), prop(RETURNS_INT)]}
        ],
        StrategyDraft=[{"code": SEARCH_STRATEGY}],
        DiagnosisDraft=[DIAGNOSIS],
    )


# The search
def test_a_hang_is_found_shrunk_and_recorded_as_a_timeout():
    sorted_lists = st.lists(st.integers(-10, 10), max_size=8).map(sorted)
    start = time.monotonic()
    found = find_counterexamples(
        _search_fn(),
        st.tuples(sorted_lists, st.integers(-10, 10)),
        properties(),
        max_examples=200,
        call_timeout_s=1.0,
    )
    elapsed = time.monotonic() - start

    [ce] = found  # every hanging input is the same failure, wherever the loop was stopped
    assert ce.timed_out and ce.exception_type == "TimeoutError"
    assert ce.call == "binary_search([0], 1)"  # the smallest input that never finishes
    assert ce.exception_message == "did not finish within 1.0s"  # the real limit, not the cut
    assert ce.signature() == ("timeout",)
    # After the first full-second hang, later ones are cut short: well under the
    # hundreds of seconds that 1s per hanging input would cost.
    assert elapsed < 20, elapsed


def test_the_hunt_stops_at_its_budget_and_says_so():
    def always_stuck(n):
        while True:
            pass

    report: dict = {}
    start = time.monotonic()
    found = find_counterexamples(
        always_stuck,
        st.tuples(st.integers()),
        [Property("is an int", lambda args, out: isinstance(out, int))] * 3,
        max_examples=200,
        call_timeout_s=1.0,
        examples=[(i,) for i in range(50)],
        budget_s=3,
        report=report,
    )

    assert time.monotonic() - start < 8  # the budget, plus one full-limit re-run
    assert report == {"out_of_time": True}
    assert found and found[0].timed_out


def test_a_slow_call_is_not_mistaken_for_a_hang():
    def slow(n):  # finishes, just after far longer than the other calls
        if n == 7:
            time.sleep(0.3)
        return n

    calls = [(7,), (1,)]
    found = find_counterexamples(
        slow, st.tuples(st.integers(0, 6)), [], call_timeout_s=1.0, examples=calls
    )
    assert found == []


# End to end: the timeout is a symptom to predict
def test_a_timeout_is_revealed_as_never_finishing(make_client):
    client, _ = make_client(search_llm())
    body = client.post(
        "/api/sessions",
        json={"code": SEARCH_CODE, "intent": "binary search in a sorted list; -1 if absent"},
    ).json()
    keep = [{"id": p["id"], "statement": p["statement"]} for p in body["properties"]]
    client.put(f"/api/sessions/{body['id']}/properties", json={"properties": keep})
    wait_for_state(client, body["id"], {"predicting"})

    question = client.get(f"/api/sessions/{body['id']}/prediction").json()
    assert question["call"] == "binary_search([5], 6)"  # edge case: target above all

    reveal = client.post(f"/api/sessions/{body['id']}/prediction", json={"answer": "-1"}).json()
    assert reveal["outcome"] == "revealed"
    assert reveal["returned"] == {"kind": "timeout", "text": "still running after 1.0s"}
    assert reveal["message"].startswith(
        "You expected `-1`. The code never finishes: it was still running when stopped after 1.0s."
    )

    fixed = client.post(f"/api/sessions/{body['id']}/fix", json={"code": FIXED_SEARCH}).json()
    assert fixed["outcome"] == "passed", fixed


def _search_fn():
    namespace: dict = {}
    exec(compile(SEARCH_CODE, "<search>", "exec"), namespace)
    return namespace["binary_search"]
