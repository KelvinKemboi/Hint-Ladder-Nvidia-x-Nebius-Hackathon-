import pytest
from fakes import (
    BUG_LINE,
    COVERED,
    DIAGNOSIS,
    FIXED_MERGE,
    HALF_FIXED_MERGE,
    INTERVALS_STRATEGY,
    MERGE_CODE,
    NO_OVERLAP,
    FakeLLM,
)

from sparring_partner.agents.diagnosis import Diagnoser, render_trace
from sparring_partner.config import Settings
from sparring_partner.sandbox import LocalBackend, Sandbox
from sparring_partner.session import ParamSpec, PropertySpec, Session
from sparring_partner.testing import Counterexample

pytestmark = pytest.mark.anyio

TOUCHING = Counterexample(
    function="merge_intervals",
    args=["[[0, 0], [0, 0]]"],
    call="merge_intervals([[0, 0], [0, 0]])",
    violated_property=NO_OVERLAP[0],
    violated_properties=[NO_OVERLAP[0]],
    output="[[0, 0], [0, 0]]",
).to_dict()
UNSORTED = Counterexample(
    function="merge_intervals",
    args=["[[1, 1], [0, 0]]"],
    call="merge_intervals([[1, 1], [0, 0]])",
    violated_property=COVERED[0],
    violated_properties=[COVERED[0]],
    output="[[1, 1]]",
).to_dict()


@pytest.fixture
def diagnoser_for():
    sandbox = Sandbox(LocalBackend(), Settings(_env_file=None))

    def make(llm):
        return Diagnoser(llm, sandbox, max_examples=100)

    return make


def explored_session() -> Session:
    session = Session(
        code=MERGE_CODE, intent="merge overlapping intervals", function="merge_intervals"
    )
    session.params = [ParamSpec("intervals", "intervals")]
    session.properties = [PropertySpec(*NO_OVERLAP), PropertySpec(*COVERED)]
    session.strategy_code = INTERVALS_STRATEGY
    session.counterexamples = [TOUCHING, UNSORTED]
    return session


def draft(**changes):
    return DIAGNOSIS | changes


async def test_trace_records_locals_line_by_line(diagnoser_for):
    trace = await diagnoser_for(FakeLLM()).trace(explored_session())

    assert trace["output"] == "[[0, 0], [0, 0]]"
    assert trace["exception"] is None
    at_bug = [s for s in trace["steps"] if s["line"] == BUG_LINE]
    assert at_bug and at_bug[0]["locals"]["start"] == "0"
    assert at_bug[0]["locals"]["merged"] == "[[0, 0]]"


def test_render_trace():
    trace = {
        "steps": [{"line": 3, "locals": {"x": "1"}}, {"line": 4, "locals": {}}],
        "truncated": False,
        "output": "[1]",
        "exception": None,
    }
    assert render_trace(trace) == "line 3: x=1\nline 4: -\n=> returned [1]"
    assert "cut off after 1 steps" in render_trace(trace, max_steps=1)
    assert render_trace(None) == "(not available)"


async def test_verified_on_first_try(diagnoser_for):
    llm = FakeLLM(DiagnosisDraft=[draft()])
    diagnoser = diagnoser_for(llm)
    session = explored_session()
    session.trace = await diagnoser.trace(session)

    diagnosis = await diagnoser.diagnose(session)

    assert diagnosis.verified and diagnosis.attempts == 1
    assert diagnosis.line == BUG_LINE
    assert diagnosis.reference_fix == FIXED_MERGE.strip()
    [(role, _, messages)] = llm.calls
    assert role == "deep"
    prompt = messages[-1]["content"]
    assert f"  {BUG_LINE} |         if start < merged[-1][1]:" in prompt  # numbered code
    assert TOUCHING["call"] in prompt and UNSORTED["call"] in prompt
    assert f"line {BUG_LINE}: " in prompt  # the trace
    assert "share an endpoint overlap" in prompt  # the shared conventions


async def test_fix_that_misses_a_failure_is_sent_back(diagnoser_for):
    llm = FakeLLM(DiagnosisDraft=[draft(reference_fix=HALF_FIXED_MERGE), draft()])

    diagnosis = await diagnoser_for(llm).diagnose(explored_session())

    assert diagnosis.verified and diagnosis.attempts == 2
    feedback = llm.asked("DiagnosisDraft")[1][-1]["content"]
    assert "still fails: merge_intervals(" in feedback
    assert COVERED[0] in feedback


async def test_static_problems_are_sent_back(diagnoser_for):
    bad = draft(line=99, reference_fix="def other(x):\n    return x\n")
    llm = FakeLLM(DiagnosisDraft=[bad, draft(reference_fix=MERGE_CODE), draft()])

    diagnosis = await diagnoser_for(llm).diagnose(explored_session())

    assert diagnosis.verified and diagnosis.attempts == 3
    first, second = (m[-1]["content"] for m in llm.asked("DiagnosisDraft")[1:])
    assert "line must be between 1 and" in first
    assert "must still define merge_intervals()" in first
    assert "identical to the original code" in second


async def test_unverified_after_all_attempts(diagnoser_for):
    llm = FakeLLM(DiagnosisDraft=[draft(reference_fix=HALF_FIXED_MERGE)] * 3)

    diagnosis = await diagnoser_for(llm).diagnose(explored_session())

    assert not diagnosis.verified and diagnosis.attempts == 3
    assert diagnosis.reference_fix == HALF_FIXED_MERGE.strip()
