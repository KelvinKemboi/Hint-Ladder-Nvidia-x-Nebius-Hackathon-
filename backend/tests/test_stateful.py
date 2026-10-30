"""Classes, tested as state machines: demo bug 3, an LRU cache whose get() doesn't
count as a use."""

import ast
from pathlib import Path

import pytest
from fakes import FakeLLM, prop
from hypothesis import strategies as st
from test_api import wait_for_state
from test_coaching import ALLOW

from sparring_partner.agents.code_info import ClassInfo, pick_function
from sparring_partner.agents.testgen import args_fit, fallback_strategy
from sparring_partner.session import ParamSpec
from sparring_partner.testing.capture import (
    describe_operations,
    find_counterexamples,
    object_name,
    stateful_target,
)
from sparring_partner.testing.counterexamples import Property
from sparring_partner.testing.harness import check_method_strategies, load_predicate

LRU_CODE = (Path(__file__).parents[2] / "demo" / "lru_cache.py").read_text()
FIXED_LRU = LRU_CODE.replace(
    "        return self.cache[key]\n",
    "        self.cache.move_to_end(key)\n        return self.cache[key]\n",
)
BUG_LINE = 15  # `return self.cache[key]`: no move_to_end before it

# What a correct LRU cache returns, replayed on a simple model of one.
GET_MATCHES_MODEL = (
    "get returns the value most recently put for a key, or -1 if it was never put "
    "or has been evicted as the least recently used",
    "def check(args, output):\n"
    "    from collections import OrderedDict\n"
    "    ops = args[0]\n"
    "    capacity = ops[0][1]\n"
    "    model = OrderedDict()\n"
    "    for (name, *call), got in zip(ops[1:], output[1:]):\n"
    "        if name == 'get':\n"
    "            key = call[0]\n"
    "            if key in model:\n"
    "                model.move_to_end(key)\n"
    "                if got != model[key]:\n"
    "                    return False\n"
    "            elif got != -1:\n"
    "                return False\n"
    "        else:\n"
    "            key, value = call\n"
    "            if key in model:\n"
    "                model.move_to_end(key)\n"
    "            model[key] = value\n"
    "            if len(model) > capacity:\n"
    "                model.popitem(last=False)\n"
    "    return True\n",
)
PUT_RETURNS_NONE = (
    "put returns None",
    "def check(args, output):\n"
    "    return all(r is None for (name, *_), r in zip(args[0], output) if name == 'put')\n",
)
GET_NEVER_INVENTS = (
    "get only returns -1 or a value that was put",
    "def check(args, output):\n"
    "    values = {op[2] for op in args[0] if op[0] == 'put'}\n"
    "    return all(r == -1 or r in values\n"
    "               for (name, *_), r in zip(args[0], output) if name == 'get')\n",
)
LRU_STRATEGY = (
    "def strategy():\n"
    "    return {\n"
    "        '__init__': st.tuples(st.integers(1, 3)),\n"
    "        'get': st.tuples(st.integers(1, 5)),\n"
    "        'put': st.tuples(st.integers(1, 5), st.integers(1, 9)),\n"
    "    }\n"
)
STRATEGIES = {
    "__init__": st.tuples(st.integers(1, 3)),
    "get": st.tuples(st.integers(1, 5)),
    "put": st.tuples(st.integers(1, 5), st.integers(1, 9)),
}
EXAMPLES = {
    "examples": [
        {"args": "([['LRUCache', 1], ['put', 1, 1], ['get', 1]],)", "output": "[None, None, 1]"},
        {
            "args": "([['LRUCache', 1], ['put', 1, 1], ['put', 2, 2], ['get', 1]],)",
            "output": "[None, None, None, -1]",
        },
        {"args": "[['LRUCache', 2], ['get', 7]]", "output": "[None, -1]"},  # no tuple: still fine
    ]
}
DIAGNOSIS = {
    "root_cause": "SECRET: get reads the key without marking it as recently used",
    "line": BUG_LINE,
    "region": "the get method",
    "bug_class": "missing state update",
    "concept": "SECRET: reading an entry counts as using it",
    "reference_fix": FIXED_LRU,
}
INTENT = "An LRU cache: get and put, evicting the least recently used key when full"


def lru_llm(**extra) -> FakeLLM:
    replies = {
        "OperationExamples": [EXAMPLES],
        "Proposal": [
            {
                "properties": [
                    prop(GET_MATCHES_MODEL),
                    prop(PUT_RETURNS_NONE),
                    prop(GET_NEVER_INVENTS),
                ]
            }
        ],
        "StrategyDraft": [{"code": LRU_STRATEGY}],
        "DiagnosisDraft": [DIAGNOSIS],
    }
    for name, queue in extra.items():
        replies[name] = replies.get(name, []) + queue
    return FakeLLM(**replies)


def model_property() -> Property:
    return Property(GET_MATCHES_MODEL[0], load_predicate(GET_MATCHES_MODEL[1]))


# Finding the class
def test_a_class_is_picked_with_its_public_methods():
    target = pick_function(LRU_CODE, "an LRU cache")

    assert isinstance(target, ClassInfo) and target.stateful
    assert target.name == "LRUCache"
    assert target.arities() == {"__init__": 1, "get": 1, "put": 2}
    assert target.signature() == (
        "class LRUCache:\n    __init__(capacity)\n    get(key)\n    put(key, value)"
    )


def test_a_function_matching_the_intent_beats_a_class():
    code = LRU_CODE + "\n\ndef merge_intervals(intervals):\n    return intervals\n"
    assert pick_function(code, "merge overlapping intervals").name == "merge_intervals"
    assert pick_function(code, "least recently used cache").name == "LRUCache"


def test_operations_are_shown_as_calls_on_one_object():
    ops = [["LRUCache", 2], ["put", 1, 1], ["get", 1]]
    assert (
        describe_operations("LRUCache", ops) == "cache = LRUCache(2); cache.put(1, 1); cache.get(1)"
    )
    assert object_name("BankAccount") == "account" and object_name("Stack") == "stack"
    assert stateful_target(_load_lru())(ops) == [None, None, 1]


# The state machine search
def test_the_search_finds_and_shrinks_the_recency_bug():
    [ce] = find_counterexamples(
        stateful_target(_load_lru()), STRATEGIES, [model_property()], max_examples=200
    )

    ops = ast.literal_eval(ce.args[0])
    # The shortest way to see it: fill a cache of 2, read the older key, add a third:
    # the key just read is the one evicted, so reading it again gives -1.
    assert ops[0] == ["LRUCache", 2]
    assert [op[0] for op in ops[1:]] == ["put", "put", "get", "put", "get"]
    (_, a, _), (_, b, _), (_, read), (_, c, _), (_, again) = ops[1:]
    assert read == again == a and len({a, b, c}) == 3
    assert ast.literal_eval(ce.output)[-1] == -1
    assert ce.call.startswith("cache = LRUCache(2); cache.put(")


def test_the_fixed_class_passes():
    fixed = _load_lru(FIXED_LRU)
    assert find_counterexamples(stateful_target(fixed), STRATEGIES, [model_property()]) == []


def test_a_crashing_method_is_found_too():
    code = LRU_CODE.replace("            return -1\n", "            raise KeyError(key)\n")
    [ce] = find_counterexamples(stateful_target(_load_lru(code)), STRATEGIES, [], max_examples=100)
    assert ce.exception_type == "KeyError"
    assert ce.call == "cache = LRUCache(1); cache.get(1)"
    assert ce.exception_line == 14


def test_a_class_strategy_needs_every_method_with_the_right_arity():
    methods = {"__init__": 1, "get": 1, "put": 2}
    assert check_method_strategies(lambda: STRATEGIES, methods) == []
    missing = {k: v for k, v in STRATEGIES.items() if k != "put"}
    assert check_method_strategies(lambda: missing, methods) == [
        "strategy() must have a key for 'put'"
    ]
    wrong = STRATEGIES | {"put": st.tuples(st.integers())}
    assert "'put': must draw a tuple of 2" in check_method_strategies(lambda: wrong, methods)[0]
    assert check_method_strategies(lambda: STRATEGIES["get"], methods)[0].startswith(
        "strategy() must return a dict"
    )


def test_the_fallback_strategy_draws_small_ints_for_every_call():
    namespace = {"st": st}
    exec(fallback_strategy([], {"__init__": 1, "get": 1, "put": 2}), namespace)
    assert check_method_strategies(namespace["strategy"], {"__init__": 1, "get": 1, "put": 2}) == []


def test_suggested_operations_must_name_real_methods():
    params = [ParamSpec("ops", "operations")]
    methods = {"__init__": 1, "get": 1, "put": 2}
    assert args_fit(([["LRUCache", 2], ["put", 1, 1], ["get", 1]],), params, methods)
    assert not args_fit(([["LRUCache", 2], ["delete", 1]],), params, methods)
    assert not args_fit(([["LRUCache", 2], ["put", 1]],), params, methods)
    assert not args_fit(([["LRUCache"]],), params, methods)


# End to end through the API
def lru_session(make_client, **llm_extra):
    llm = lru_llm(**llm_extra)
    client, orchestrator = make_client(llm)
    response = client.post("/api/sessions", json={"code": LRU_CODE, "intent": INTENT})
    assert response.status_code == 201, response.text
    body = response.json()
    keep = [{"id": p["id"], "statement": p["statement"]} for p in body["properties"]]
    client.put(f"/api/sessions/{body['id']}/properties", json={"properties": keep})
    wait_for_state(client, body["id"], {"predicting"})
    return client, orchestrator, body, llm


def test_a_class_session_asks_about_the_last_call(make_client):
    client, orchestrator, body, llm = lru_session(make_client)
    sid = body["id"]

    assert body["function"] == "LRUCache"
    assert len(orchestrator.store.get(sid).worked_examples) == 3
    question = client.get(f"/api/sessions/{sid}/prediction").json()["question"]
    setup, last = question.removeprefix("After `").split("`, what should `")
    assert setup.startswith("cache = LRUCache(2); cache.put(")
    assert last.startswith("cache.get(") and last.endswith(")` return?")

    reveal = client.post(f"/api/sessions/{sid}/prediction", json={"answer": "1"}).json()
    assert reveal["outcome"] == "revealed"
    assert reveal["message"].startswith("You expected `1`. The code returns `-1`.")
    assert reveal["returned"] == {"kind": "value", "text": "-1"}  # the last call, not the list
    assert reveal["predicted"] == {"kind": "value", "text": "1"}

    # Predicting what the buggy cache returns breaks the spec
    nudge = client.post(f"/api/sessions/{sid}/prediction", json={"answer": "-1"}).json()
    assert nudge["outcome"] == "matches_code"
    assert "get returns the value most recently put" in nudge["message"]


def test_the_trace_shows_the_object_state_and_the_fix_verifies(make_client):
    client, _, body, llm = lru_session(
        make_client,
        HintDraft=[{"text": "Look at the marked row: which key is oldest there?"}] * 2,
        GateVerdict=[ALLOW] * 2,
        PostmortemDraft=[
            {"bug_class": "stale state", "how_to_spot": "s", "other_edge_cases": ["capacity 1"]}
        ],
        RiskDraft=[{"question": "What about a capacity of 1?", "example_args": "x"}],
    )
    llm.replies["GateVerdict"].append(ALLOW)  # for the risk question after the fix
    sid = body["id"]
    client.post(f"/api/sessions/{sid}/prediction", json={"answer": "1"})  # level 1
    client.post(f"/api/sessions/{sid}/hint")
    level3 = client.post(f"/api/sessions/{sid}/hint").json()
    assert level3["level"] == 3

    rows = level3["table"]["rows"]
    shown = {k for row in rows for k in row["vars"]}
    assert {"self.cache", "self.capacity"} <= shown  # the object's state, not its address
    assert not any("object at 0x" in v for row in rows for v in row["vars"].values())
    assert any(row["marked"] for row in rows)

    result = client.post(f"/api/sessions/{sid}/fix", json={"code": FIXED_LRU}).json()
    assert result.get("outcome") == "passed", result


def test_a_fix_that_drops_the_class_is_invalid(make_client):
    client, _, body, _ = lru_session(make_client)
    result = client.post(
        f"/api/sessions/{body['id']}/fix", json={"code": "def get(key):\n    return -1\n"}
    ).json()
    assert result["outcome"] == "invalid"
    assert "must still define LRUCache" in result["message"] or result["message"] == (
        "That fix can't be checked."
    )


def _load_lru(code: str = LRU_CODE) -> type:
    namespace: dict = {}
    exec(compile(code, "<lru>", "exec"), namespace)
    return namespace["LRUCache"]


@pytest.fixture(autouse=True)
def _lru_line_is_the_bug():
    assert LRU_CODE.splitlines()[BUG_LINE - 1].strip() == "return self.cache[key]"
