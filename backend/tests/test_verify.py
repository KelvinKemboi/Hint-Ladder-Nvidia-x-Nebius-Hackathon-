import pytest
from fakes import (
    COVERED,
    FIXED_MERGE,
    HALF_FIXED_MERGE,
    INTERVALS_STRATEGY,
    MERGE_CODE,
    NO_OVERLAP,
    FakeLLM,
)
from test_diagnosis import TOUCHING, UNSORTED

from sparring_partner.agents.postmortem import write_postmortem
from sparring_partner.agents.verify import (
    FixVerifier,
    hardcoded_literals,
    nearby_variants,
    next_risk,
    shifted,
)
from sparring_partner.config import Settings
from sparring_partner.sandbox import LocalBackend, Sandbox
from sparring_partner.session import ParamSpec, PropertySpec, Session

pytestmark = pytest.mark.anyio

SORTED_ONLY_STRATEGY = INTERVALS_STRATEGY.replace(
    "st.lists(interval, max_size=6)", "st.lists(interval, max_size=6).map(sorted)"
)
SPECIAL_CASED = HALF_FIXED_MERGE.replace(
    '    """Merge overlapping intervals."""\n',
    '    """Merge overlapping intervals."""\n'
    "    if [list(i) for i in intervals] == [[1, 1], [0, 0]]:\n"
    "        return [[0, 1]]\n",
)
FIXED_WITH_LITERAL = FIXED_MERGE.replace(
    '    """Merge overlapping intervals."""\n',
    '    """Merge overlapping intervals."""\n'
    "    if intervals == [[0, 0], [0, 0]]:\n"
    "        return [[0, 0]]\n",
)


@pytest.fixture
def sandbox():
    return Sandbox(LocalBackend(), Settings(_env_file=None))


def explored(strategy=INTERVALS_STRATEGY, code=MERGE_CODE) -> Session:
    s = Session(code=code, intent="merge overlapping intervals", function="merge_intervals")
    s.params = [ParamSpec("intervals", "intervals")]
    s.properties = [PropertySpec(*NO_OVERLAP), PropertySpec(*COVERED)]
    s.strategy_code = strategy
    s.counterexamples = [TOUCHING, UNSORTED]
    return s


# Special-casing checks
def test_hardcoded_literals_are_counterexample_values_new_in_the_fix():
    cases = [TOUCHING, UNSORTED]
    assert hardcoded_literals(MERGE_CODE, FIXED_WITH_LITERAL, cases) == ["[[0, 0], [0, 0]]"]
    assert hardcoded_literals(MERGE_CODE, SPECIAL_CASED, cases) == ["[[1, 1], [0, 0]]"]
    assert hardcoded_literals(MERGE_CODE, FIXED_MERGE, cases) == []
    # Trivial values like [] or 0 don't count, even if the counterexample contains them.
    trivial = MERGE_CODE.replace("return []", "return [] if intervals == [] else []")
    assert hardcoded_literals(MERGE_CODE, trivial, cases) == []


def test_tuples_and_lists_match():
    tupled = {**TOUCHING, "args": ["[(0, 0), (0, 0)]"]}
    assert hardcoded_literals(MERGE_CODE, FIXED_WITH_LITERAL, [tupled]) == ["[[0, 0], [0, 0]]"]


def test_nearby_variants_shift_every_int_and_keep_shape():
    assert shifted(([[1, 4], (4, 5)], True, "x"), 2) == ([[3, 6], (6, 7)], True, "x")
    variants = nearby_variants([TOUCHING])
    assert ([[1, 1], [1, 1]],) in variants and ([[-4, -4], [-4, -4]],) in variants
    assert len(variants) == len({repr(v) for v in variants})


# Verification in the sandbox
async def test_correct_fix_passes_on_hundreds_of_fresh_inputs(sandbox):
    report = await FixVerifier(sandbox, max_examples=200).verify(explored(), FIXED_MERGE)
    assert report.outcome == "passed", report
    assert report.inputs_checked >= 600  # 200 per search, 3 searches, plus examples
    assert report.original_failures == []


async def test_progress_is_reported_as_each_batch_finishes(sandbox):
    seen = []
    verifier = FixVerifier(sandbox, max_examples=100, batches=4)

    report = await verifier.verify(explored(), FIXED_MERGE, on_progress=lambda *p: seen.append(p))

    assert report.outcome == "passed"
    assert seen == [(25, 100), (50, 100), (75, 100), (100, 100)]


async def test_partial_fix_fails_with_a_new_counterexample(sandbox):
    report = await FixVerifier(sandbox, max_examples=100).verify(explored(), HALF_FIXED_MERGE)

    assert report.outcome == "failed"
    assert report.original_failures == [1]  # touching is fixed; unsorted still fails
    assert {ce["violated_property"] for ce in report.counterexamples} == {COVERED[0]}


async def test_fix_that_only_handles_the_failing_input_is_special_cased(sandbox):
    # Tests that only draw sorted input, and no hand-picked edge cases, can't see the
    # unsorted bug; the nearby variants do.
    session = explored(SORTED_ONLY_STRATEGY)
    session.params = [ParamSpec("intervals", "other")]
    report = await FixVerifier(sandbox, max_examples=100).verify(session, SPECIAL_CASED)

    assert report.outcome == "special_cased"
    assert "right next to" in report.reason
    [variant] = report.counterexamples
    assert variant["call"] != UNSORTED["call"]


async def test_hardcoded_failing_input_is_special_cased(sandbox):
    report = await FixVerifier(sandbox, max_examples=100).verify(explored(), FIXED_WITH_LITERAL)
    assert (report.outcome, report.hardcoded) == ("special_cased", ["[[0, 0], [0, 0]]"])


@pytest.mark.parametrize(
    "fix, reason",
    [
        ("def merge_intervals(:\n", "SyntaxError on line 1"),
        ("def other(x):\n    return x\n", "still define"),
    ],
)
async def test_invalid_fixes(sandbox, fix, reason):
    report = await FixVerifier(sandbox).verify(explored(), fix)
    assert report.outcome == "invalid" and reason in report.reason


# Next risk and post-mortem
async def test_next_risk_is_tested_against_the_code(sandbox):
    risk = {"question": "What if the input isn't sorted?", "example_args": "([[3, 4], [1, 2]],)"}
    llm = FakeLLM(RiskDraft=[risk, risk])

    question, ce = await next_risk(llm, sandbox, explored(code=HALF_FIXED_MERGE))
    assert question == risk["question"]
    assert ce["call"] == "merge_intervals([[3, 4], [1, 2]])"  # the code fails it

    session = explored(code=FIXED_MERGE)
    assert await next_risk(llm, sandbox, session) == (risk["question"], None)
    prompt = llm.asked("RiskDraft")[0][-1]["content"]
    assert "Edge cases already tested" in prompt and "touching ranges" in prompt


async def test_next_risk_may_break_an_assumption_the_intent_made(sandbox):
    # Demo bug 1: the intent says sorted, so the hunt only draws sorted input and the
    # comparison fix passes; the follow-up question then tries unsorted input.
    risk = {"question": "What if the input isn't sorted?", "example_args": "([(5, 6), (1, 2)],)"}
    session = explored(code=HALF_FIXED_MERGE)
    session.params = [ParamSpec("intervals", "sorted_intervals")]

    _, ce = await next_risk(FakeLLM(RiskDraft=[risk]), sandbox, session)

    assert ce is not None and ce["call"] == "merge_intervals([[5, 6], [1, 2]])"


async def test_postmortem_is_structured():
    llm = FakeLLM(
        PostmortemDraft=[
            {
                "bug_class": "boundary condition",
                "how_to_spot": "Check what happens when two values are equal.",
                "other_edge_cases": ["nested intervals", "negative numbers"],
            }
        ]
    )
    session = explored(code=FIXED_MERGE)
    session.solved_cases = [
        {"call": "f(1)", "bug_class": "boundary condition", "root_cause": "x", "hint_level": 3},
        {"call": "f(2)", "bug_class": "wrong input assumption", "root_cause": "y", "hint_level": 1},
    ]
    session.fix_attempts = [
        {"outcome": "failed", "inputs_checked": 10},
        {"outcome": "passed", "inputs_checked": 900},
    ]

    pm = await write_postmortem(llm, session)

    assert pm["bug_class"] == "boundary condition"
    assert pm["bug_classes"] == ["boundary condition", "wrong input assumption"]
    assert pm["hint_level_reached"] == 3 and pm["fix_attempts"] == 2
    assert pm["inputs_checked"] == 900
    assert pm["other_edge_cases"] == ["nested intervals", "negative numbers"]


async def test_postmortem_without_bugs_needs_no_model():
    pm = await write_postmortem(FakeLLM(), explored())
    assert pm["bug_class"] is None and pm["fix_attempts"] == 0
