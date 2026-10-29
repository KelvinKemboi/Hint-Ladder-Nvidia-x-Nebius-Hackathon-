import pytest
from fakes import (
    BUG_LINE,
    COVERED,
    DIAGNOSIS,
    FIXED_MERGE,
    HALF_FIXED_MERGE,
    MERGE_CODE,
    NO_OVERLAP,
    FakeLLM,
)

from sparring_partner.agents.gate import (
    HintGate,
    code_snippets,
    concept_terms,
    fix_sequences,
    leaks_fix,
    mentions_concept,
    mentions_line_number,
    mentions_structure,
    mentions_trace,
    splice_variants,
)
from sparring_partner.config import Settings
from sparring_partner.sandbox import LocalBackend, Sandbox
from sparring_partner.session import Diagnosis, ParamSpec, PropertySpec, Session
from sparring_partner.testing import Counterexample

ALLOW = {"allow": True, "reason": "fine"}
DENY = {"allow": False, "reason": "it names the operator to use"}


# --- Deterministic rules, one by one ------------------------------------------------------


def test_fix_sequences_are_the_new_code_in_the_fix():
    sequences = fix_sequences(MERGE_CODE, FIXED_MERGE)
    assert ("<=",) in sequences  # new operator
    assert ("sorted", "(") in sequences  # new call
    assert ("start", "<=", "merged") in sequences
    assert ("merged", "[", "-") not in sequences  # already in the original code


@pytest.mark.parametrize(
    "text, leak",
    [
        ("Use <= instead.", "<="),
        ("Try sorted(intervals) first.", "sorted ("),
        ("if start <= merged then...", "<="),
        ("Make sure the output is sorted.", None),  # the English word is fine
        ("Check your comparison operator.", None),
    ],
)
def test_leaks_fix(text, leak):
    assert leaks_fix(text, fix_sequences(MERGE_CODE, FIXED_MERGE)) == leak


@pytest.mark.parametrize(
    "text, found",
    [
        ("Look at line 7.", True),
        ("on Line #7", True),
        ("lines 6-7", True),
        ("the 7th line", True),
        ("see L7", True),
        ("with 7 intervals", False),
        ("an online demo", False),
    ],
)
def test_mentions_line_number(text, found):
    assert bool(mentions_line_number(text)) is found


@pytest.mark.parametrize(
    "text, found",
    [
        ("Look at the for loop.", "for loop"),
        ("Check the if statement.", "if statement"),
        ("What does `merged` hold?", "`merged`"),
        ("merged[-1] is wrong", "merged["),
        ("_helper does it", "_helper"),
        ("Think about the end of the list.", None),  # 'end' is a variable, but used as English
        ("The merged result is wrong.", None),
    ],
)
def test_mentions_structure(text, found):
    assert mentions_structure(text, {"_helper"}, {"merged", "start", "end"}) == found


@pytest.mark.parametrize(
    "text, found",
    [
        ("At that point merged = [[0, 0]].", "merged = ["),
        ("merged is [[0, 0]] there", "merged is ["),
        ("| step | values |\n|---|---|", "| step |"),
        ("The merged list is too long.", None),
        ("start == end here", None),  # a comparison, not a value
    ],
)
def test_mentions_trace(text, found):
    assert mentions_trace(text, {"merged", "start", "end"}) == found


def test_concept_vocabulary_excludes_the_users_own_words():
    terms = concept_terms(
        "intervals that share an endpoint overlap",
        "boundary condition",
        "merge overlapping intervals",
    )
    assert mentions_concept("They share a point.", terms) == "share"
    assert mentions_concept("Touching at an endpoint counts.", terms) == "endpoint"
    assert mentions_concept("It's a boundary problem.", terms) == "boundary"
    assert mentions_concept("The intervals overlap.", terms) is None  # the user's own words


def test_code_snippets():
    text = "Try `merged` or `x <= y`, or:\n```python\nif a:\n    b()\n```"
    assert code_snippets(text) == ["x <= y", "if a:\n    b()"]


def test_splice_variants():
    variants = splice_variants(MERGE_CODE, "if start <= merged[-1][1]:", "merge_intervals")
    assert HALF_FIXED_MERGE in variants  # replacing line 7
    assert MERGE_CODE not in variants
    whole = "def merge_intervals(xs):\n    return xs\n"
    assert splice_variants(MERGE_CODE, whole, "merge_intervals") == [whole.strip()]


# --- The gate as a whole ---------------------------------------------------------------------


def session() -> Session:
    s = Session(code=MERGE_CODE, intent="merge overlapping intervals", function="merge_intervals")
    s.params = [ParamSpec("intervals", "intervals")]
    s.properties = [PropertySpec(*NO_OVERLAP), PropertySpec(*COVERED)]
    s.counterexamples = [
        Counterexample(
            function="merge_intervals",
            args=["[[0, 0], [0, 0]]"],
            call="merge_intervals([[0, 0], [0, 0]])",
            violated_property=NO_OVERLAP[0],
            violated_properties=[NO_OVERLAP[0]],
            output="[[0, 0], [0, 0]]",
        ).to_dict()
    ]
    s.diagnosis = Diagnosis(**DIAGNOSIS, verified=True)
    local_vars = {"intervals": "[[0, 0], [0, 0]]", "merged": "[[0, 0]]", "start": "0", "end": "0"}
    s.trace = {
        "steps": [{"line": 6, "function": "merge_intervals", "locals": local_vars}],
        "truncated": False,
        "output": "[[0, 0], [0, 0]]",
        "exception": None,
    }
    return s


@pytest.fixture
def gate_with():
    sandbox = Sandbox(LocalBackend(), Settings(_env_file=None))

    def make(*verdicts):
        llm = FakeLLM(GateVerdict=list(verdicts))
        return HintGate(llm, sandbox), llm

    return make


@pytest.mark.anyio
async def test_levels_unlock_in_order(gate_with):
    gate, _ = gate_with(*[ALLOW] * 10)
    s = session()
    cases = [
        ("Look at line 7.", 5),
        ("The bug is in the for loop.", 2),
        ("At that point merged = [[0, 0]].", 3),
        ("Intervals that share an endpoint overlap.", 4),
    ]
    for text, level in cases:
        assert not (await gate.check(s, text, level - 1, kind="hint")).allowed, text
        assert (await gate.check(s, text, level, kind="hint")).allowed, text


@pytest.mark.anyio
async def test_fix_code_is_blocked_even_at_level_5(gate_with):
    gate, llm = gate_with()
    decision = await gate.check(session(), "Change it to `start <= merged[-1][1]`.", 5, kind="hint")
    assert (decision.allowed, decision.rule) == (False, "fix_tokens")
    assert llm.calls == []  # the cheap layer caught it


@pytest.mark.anyio
async def test_code_that_fixes_the_failure_is_blocked_in_the_sandbox(gate_with):
    gate, llm = gate_with()
    # Same meaning as the fix, different tokens: only running it catches this.
    text = "What if it read:\n```python\nif not start > merged[-1][1]:\n```"
    decision = await gate.check(session(), text, 5, kind="hint")
    assert (decision.allowed, decision.layer, decision.rule) == (False, "sandbox", "code_fixes")


@pytest.mark.anyio
async def test_quoting_the_buggy_line_is_fine(gate_with):
    gate, _ = gate_with(ALLOW)
    text = f"Look at line {BUG_LINE}: `if start < merged[-1][1]:`. What should it do?"
    assert (await gate.check(session(), text, 5, kind="hint")).allowed


@pytest.mark.anyio
async def test_llm_judge_can_block(gate_with):
    gate, llm = gate_with(DENY)
    decision = await gate.check(session(), "Think about which comparison to use.", 4, kind="hint")

    assert (decision.allowed, decision.layer, decision.reason) == (False, "llm", DENY["reason"])
    [(role, _, messages)] = llm.calls
    assert role == "gate"
    prompt = messages[-1]["content"]
    assert "Current hint level: 4" in prompt
    allowed, not_yet = prompt.split("NOT allowed yet:")
    assert "level 4" in allowed and "level 5" not in allowed
    assert "level 5: the line" in not_yet.split("Never allowed")[0]
    assert "Think about which comparison to use." in prompt


@pytest.mark.anyio
async def test_every_decision_is_logged_and_templates_skip_the_model(gate_with):
    gate, llm = gate_with()
    s = session()
    await gate.check(s, "What should `merge_intervals([[0, 0], [0, 0]])` return?", 0,
                     kind="question", use_llm=False)  # fmt: skip
    await gate.check(s, "line 7", 1, kind="hint", use_llm=False)

    assert llm.calls == []
    assert [(e["kind"], e["allowed"], e["rule"]) for e in s.gate_log] == [
        ("question", True, None),
        ("hint", False, "line_number"),
    ]


@pytest.mark.parametrize(
    "text",
    [
        "start is less than the last end there.",
        "It uses a strict check.",
        "a strict inequality",
        "What happens when start is equal to the end?",
        "what if they are equal?",
        "classic off-by-one",
    ],
)
def test_comparison_language_is_concept_level(text):
    assert mentions_concept(text, set())


@pytest.mark.parametrize("text", ["The two intervals are identical.", "It returns two lists."])
def test_ordinary_words_are_not_comparison_language(text):
    assert mentions_concept(text, set()) is None


def test_concept_words_match_inflections_and_skip_generic_words():
    terms = concept_terms(
        "merging needs a sorted order, or the result is wrong",
        "wrong assumption",
        "merge intervals",
    )
    assert mentions_concept("The merged result is off.", terms) is None  # the user's own word
    assert mentions_concept("Something goes wrong here.", terms) is None  # generic
    assert mentions_concept("Think about the ordering.", terms) == "ordering"
    assert mentions_concept("What's the assumption?", terms) == "assumption"


@pytest.mark.anyio
async def test_review_questions_get_the_review_allowance(gate_with):
    gate, llm = gate_with(ALLOW, ALLOW)
    await gate.check(session(), "What if the input isn't sorted?", 0, kind="review")
    await gate.check(session(), "Which part of your spec breaks?", 1, kind="hint")
    review_prompt, hint_prompt = (m[-1]["content"] for m in llm.asked("GateVerdict"))
    assert "This is a code-review question" in review_prompt
    assert "Does this question answer itself" in review_prompt
    assert "hint level" not in review_prompt.lower()  # not judged against the ladder
    assert "This is a code-review question" not in hint_prompt
    assert "Current hint level: 1" in hint_prompt
