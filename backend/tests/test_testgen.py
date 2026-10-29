import ast
from types import SimpleNamespace

import pytest
from fakes import COVERED, INTERVALS_STRATEGY, MERGE_CODE, NO_OVERLAP, FakeLLM

from sparring_partner.agents.code_info import find_functions, pick_function
from sparring_partner.agents.errors import GenerationError
from sparring_partner.agents.testgen import (
    clean_code,
    edge_cases,
    exploration_harness,
    fallback_strategy,
    infer_inputs,
    lint_strategy,
    write_strategy,
)
from sparring_partner.config import Settings
from sparring_partner.sandbox import LocalBackend, Sandbox
from sparring_partner.session import ParamSpec, PropertySpec, Session
from sparring_partner.testing.harness import check_predicates

SETTINGS = Settings(_env_file=None)
TWO_FUNCTIONS = '''\
def _helper(x):
    return x


def binary_search(xs: list[int], target: int) -> int:
    """Index of target in sorted xs, or -1."""
    return -1


def unrelated(y):
    return y
'''


@pytest.fixture
def sandbox():
    return Sandbox(LocalBackend(), SETTINGS)


def merge_session(**overrides) -> Session:
    session = Session(
        code=MERGE_CODE, intent="merge overlapping intervals", function="merge_intervals"
    )
    session.params = [ParamSpec("intervals", "intervals")]
    session.properties = [PropertySpec(*NO_OVERLAP), PropertySpec(*COVERED)]
    for key, value in overrides.items():
        setattr(session, key, value)
    return session


# Reading the code
def test_pick_function_by_intent_name_or_explicit_name():
    assert pick_function(TWO_FUNCTIONS, "binary search for a target").name == "binary_search"
    assert pick_function(TWO_FUNCTIONS, "whatever", name="unrelated").name == "unrelated"
    # No word overlap: the last public function wins.
    assert pick_function(TWO_FUNCTIONS, "do the thing").name == "unrelated"


def test_pick_function_errors():
    with pytest.raises(ValueError, match="no top-level function named"):
        pick_function(TWO_FUNCTIONS, "x", name="missing")
    with pytest.raises(ValueError, match="no top-level function"):
        pick_function("class LRU:\n    pass\n", "cache")
    with pytest.raises(SyntaxError):
        pick_function("def f(:\n", "x")


def test_signature_and_docstring():
    [_, fn, _] = find_functions(TWO_FUNCTIONS)
    assert fn.signature() == "binary_search(xs: list[int], target: int) -> int"
    assert fn.docstring == "Index of target in sorted xs, or -1."


# Input kinds
@pytest.mark.anyio
async def test_infer_inputs_from_the_model():
    llm = FakeLLM(
        InputSpecDraft=[
            {
                "params": [
                    {"name": "xs", "kind": "sorted_list_int", "description": "sorted ints"},
                    {"name": "target", "kind": "int", "description": "value to find"},
                ],
                "examples": [
                    {"args": "([1, 2, 3], 2)", "output": "1"},
                    {"args": "([], 4)", "output": "-1"},
                    {"args": "not python", "output": "0"},  # skipped
                ],
            }
        ]
    )
    fn = pick_function(TWO_FUNCTIONS, "binary search")
    info = await infer_inputs(llm, fn, "binary search", TWO_FUNCTIONS)

    assert [(p.name, p.kind) for p in info.params] == [
        ("xs", "sorted_list_int"),
        ("target", "int"),
    ]
    assert info.example_args == ([1, 2, 3], 2)
    assert info.worked_examples == [(([1, 2, 3], 2), 1), (([], 4), -1)]
    prompt = llm.asked("InputSpecDraft")[0][-1]["content"]
    assert "binary_search(xs: list[int], target: int) -> int" in prompt
    assert "Index of target in sorted xs" in prompt
    assert "CORRECT implementation" in prompt


@pytest.mark.anyio
async def test_infer_inputs_wraps_a_single_bare_argument():
    spec = {"params": [{"name": "intervals", "kind": "intervals", "description": ""}],
            "examples": [{"args": "[[1, 2]]", "output": "[[1, 2]]"}]}  # fmt: skip
    fn = pick_function(MERGE_CODE, "merge")
    info = await infer_inputs(FakeLLM(InputSpecDraft=[spec]), fn, "merge", MERGE_CODE)
    assert info.worked_examples == [(([[1, 2]],), [[1, 2]])]


@pytest.mark.anyio
async def test_infer_inputs_falls_back_to_annotations():
    wrong_names = {"params": [{"name": "nums", "kind": "list_int", "description": ""}],
                   "examples": [{"args": "([],)", "output": "-1"}]}  # fmt: skip
    fn = pick_function(TWO_FUNCTIONS, "binary search")
    info = await infer_inputs(
        FakeLLM(InputSpecDraft=[wrong_names]), fn, "binary search in a sorted list", TWO_FUNCTIONS
    )

    assert [p.kind for p in info.params] == ["sorted_list_int", "int"]
    assert info.example_args == ([1, 3, 5, 7], 3)  # typical values
    assert info.worked_examples == []


# Edge cases
def test_interval_edge_cases_include_the_classic_traps():
    cases = {c.label: c.args for c in edge_cases([ParamSpec("intervals", "intervals")])}
    assert cases["touching ranges"] == ([[1, 4], [4, 5]],)
    assert cases["unsorted"] == ([[5, 6], [1, 2]],)
    assert cases["empty"] == ([],)
    assert "huge values" in cases and "negatives" in cases and "duplicates" in cases


def test_search_edge_cases_cover_every_boundary():
    cases = edge_cases([ParamSpec("xs", "sorted_list_int"), ParamSpec("target", "int")])
    labels = [c.label for c in cases]
    args = [c.args for c in cases]

    assert "xs: empty" in labels and "target: huge" in labels
    assert ([1, 3, 5, 7], 7) in args  # target is the last element
    assert ([5], 6) in args  # absent, above all
    assert len(args) == len({repr(a) for a in args})  # no duplicates


def test_unknown_kinds_have_no_edge_cases_or_fallback():
    params = [ParamSpec("tree", "other")]
    assert edge_cases(params) == []
    assert fallback_strategy(params) is None


# Strategy linting and generation
@pytest.mark.parametrize(
    "code, error",
    [
        ("def strategy(:\n    pass\n", "SyntaxError on line 1"),
        ("def make():\n    return st.integers()\n", "must define"),
        ("def strategy(n):\n    return st.integers()\n", "no arguments"),
        ("import os\ndef strategy():\n    return st.integers()\n", "'os' is not allowed"),
        ("def strategy():\n    return eval('st.integers()')\n", "eval() is not allowed"),
    ],
)
def test_lint_strategy_rejects(code, error):
    [message] = lint_strategy(code)
    assert error in message


def test_lint_accepts_good_and_fallback_strategies():
    assert lint_strategy(INTERVALS_STRATEGY) == []
    fallback = fallback_strategy([ParamSpec("xs", "sorted_list_int"), ParamSpec("t", "int")])
    assert lint_strategy(fallback) == []


@pytest.mark.anyio
async def test_write_strategy_retries_with_the_error(sandbox):
    llm = FakeLLM(StrategyDraft=[{"code": "def strategy(:\n"}, {"code": INTERVALS_STRATEGY}])
    session = merge_session()
    fn = pick_function(MERGE_CODE, session.intent)

    code = await write_strategy(llm, sandbox, session, fn)

    assert code == INTERVALS_STRATEGY.strip()
    retry = llm.asked("StrategyDraft")[1][-1]["content"]
    assert "SyntaxError on line 1" in retry


@pytest.mark.anyio
async def test_write_strategy_checks_draws_in_sandbox_then_falls_back(sandbox):
    wrong_arity = {"code": "def strategy():\n    return st.tuples(st.just([]), st.just(1))\n"}
    not_literal = {"code": "def strategy():\n    return st.tuples(st.builds(object))\n"}
    llm = FakeLLM(StrategyDraft=[wrong_arity, not_literal, wrong_arity])
    session = merge_session()
    fn = pick_function(MERGE_CODE, session.intent)

    code = await write_strategy(llm, sandbox, session, fn)

    assert code == fallback_strategy(session.params)
    retries = [m[-1]["content"] for m in llm.asked("StrategyDraft")[1:]]
    assert "tuple of 1 argument(s)" in retries[0]
    assert "plain Python literals" in retries[1]


@pytest.mark.anyio
async def test_write_strategy_gives_up_without_fallback(sandbox):
    llm = FakeLLM(StrategyDraft=[{"code": "nope("}] * 3)
    session = merge_session(params=[ParamSpec("intervals", "other")])
    fn = pick_function(MERGE_CODE, session.intent)
    with pytest.raises(GenerationError, match="input strategy"):
        await write_strategy(llm, sandbox, session, fn)


# Harnesses
@pytest.mark.anyio
async def test_exploration_harness_reports_structured_results(sandbox):
    session = merge_session()
    cases = edge_cases(session.params)
    harness = exploration_harness(session, INTERVALS_STRATEGY, cases, max_examples=100, seed=1)
    ast.parse(harness)

    result = await sandbox.run(MERGE_CODE, harness)

    assert result.ok, result.error
    report = result.value
    assert report["edge_cases"] == len(cases)
    assert report["property_errors"] == {}
    violated = {ce["violated_property"] for ce in report["counterexamples"]}
    assert violated == {NO_OVERLAP[0], COVERED[0]}  # touching ranges, and unsorted input
    for ce in report["counterexamples"]:
        assert ce["call"].startswith("merge_intervals(")
        assert ce["output"] is not None


def test_check_predicates_flags_raising_and_non_bool_predicates():
    def merge(intervals):
        return intervals

    solution = SimpleNamespace(merge=merge)
    config = {
        "function": "merge",
        "predicates": {
            "ok": "def check(args, output):\n    return output == args[0]\n",
            "raises": "def check(args, output):\n    return output[5] == 1\n",
            "not bool": "def check(args, output):\n    return len(output)\n",
            "no check": "x = 1\n",
        },
        "inputs": [([[1, 2]],)],
    }
    errors = check_predicates(solution, config)["errors"]

    assert "ok" not in errors
    assert errors["raises"].startswith("IndexError")
    assert errors["not bool"].startswith("returned int")
    assert "must define check" in errors["no check"]


def test_check_predicates_flags_a_predicate_that_rejects_correct_output():
    def merge(intervals):
        return [[0, 0]]  # wrong, but predicates are judged on the worked example here

    config = {
        "function": "merge",
        "predicates": {
            "covers": "def check(args, output):\n    return bool(output)\n",  # wrong on []
            "fine": "def check(args, output):\n    return isinstance(output, list)\n",
        },
        "inputs": [],
        "examples": [(([[1, 2]],), [[1, 2]]), (([],), [])],
    }
    errors = check_predicates(SimpleNamespace(merge=merge), config)["errors"]

    assert errors == {"covers": "says the correct output [] for args ([],) breaks the property"}


def test_clean_code_strips_fences_and_unescapes_double_escaped_newlines():
    assert clean_code("```python\ndef f():\n    return 1\n```") == "def f():\n    return 1"
    assert clean_code("def f():\\n    return 1") == "def f():\n    return 1"
    # Real newlines present: an escaped newline inside a string literal is left alone.
    assert clean_code('def f():\n    return "a\\nb"') == 'def f():\n    return "a\\nb"'


@pytest.mark.anyio
async def test_write_strategy_rejects_draws_of_the_wrong_kind(sandbox):
    bare_ints = {"code": "def strategy():\n    return st.tuples(st.integers())\n"}
    backwards = {
        "code": "def strategy():\n    return st.tuples(st.just([[3, 1]]))\n"  # start > end
    }
    llm = FakeLLM(StrategyDraft=[bare_ints, backwards, {"code": INTERVALS_STRATEGY}])
    session = merge_session()
    fn = pick_function(MERGE_CODE, session.intent)

    assert await write_strategy(llm, sandbox, session, fn) == INTERVALS_STRATEGY.strip()
    retries = [m[-1]["content"] for m in llm.asked("StrategyDraft")[1:]]
    assert "argument 1 must be intervals, got 0" in retries[0]
    assert "argument 1 must be intervals, got [[3, 1]]" in retries[1]
