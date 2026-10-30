"""Fix verification, and the next untested risk once a fix holds.

A fix passes only if the whole bug hunt finds nothing with a fresh seed (N new
inputs per property, plus the edge cases and every original counterexample),
and it doesn't special-case the failing input: no counterexample values newly
hard-coded in the diff, and no failures on inputs right next to the
counterexamples (every integer shifted a little).

The N inputs are split into batches that run in parallel, each with its own
seed, so progress ("verified on 300/500") can be reported as they finish.
"""

import ast
import asyncio
import difflib
import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from sparring_partner.agents.code_info import defined_names
from sparring_partner.agents.diagnosis import counterexample_args
from sparring_partner.agents.testgen import (
    OPERATIONS,
    JsonLLM,
    _literal_args,
    args_fit,
    edge_cases,
)
from sparring_partner.llm import Message
from sparring_partner.sandbox import Sandbox
from sparring_partner.session import Session
from sparring_partner.testing.counterexamples import Counterexample, dedupe

SHIFTS = (1, 2, 3, 7, -1, -4)
TRIVIAL = {"0", "1", "-1", "2", "True", "False", "None", "''", "[]", "()", "{}", "0.0"}

Progress = Callable[[int, int], None]  # (fresh inputs checked per property, of N)


@dataclass
class FixReport:
    outcome: str  # passed, failed, special_cased, invalid
    reason: str = ""  # for logs and messages; never contains expected outputs
    counterexamples: list[dict[str, Any]] = field(default_factory=list)  # new, ranked
    hardcoded: list[str] = field(default_factory=list)
    inputs_checked: int = 0
    original_failures: list[int] = field(default_factory=list)  # original cases still failing


# Special-casing checks
def _canonical(value: Any) -> str:
    """repr with tuples as lists, so [(0, 0)] and [[0, 0]] compare equal."""
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_canonical(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_canonical(k)}: {_canonical(v)}" for k, v in value.items()) + "}"
    return repr(value)


def _literals(source: str) -> set[str]:
    """Every literal in the code: constants, and whole list/tuple/dict/set displays
    (not the pieces of a display that is itself a literal)."""
    found: set[str] = set()

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.Constant | ast.List | ast.Tuple | ast.Dict | ast.Set):
            try:
                found.add(_canonical(ast.literal_eval(node)))
                return
            except (ValueError, TypeError, SyntaxError):
                pass
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(ast.parse(source))
    return found


def _sub_values(value: Any) -> set[str]:
    values = {_canonical(value)}
    if isinstance(value, list | tuple):
        for item in value:
            values |= _sub_values(item)
    elif isinstance(value, dict):
        for k, v in value.items():
            values |= _sub_values(k) | _sub_values(v)
    return values


def hardcoded_literals(original: str, fix: str, counterexamples: list[dict[str, Any]]) -> list[str]:
    """Counterexample values that appear as literals in the fix but not the original."""
    added = "\n".join(
        line[2:]
        for line in difflib.ndiff(original.splitlines(), fix.splitlines())
        if line.startswith("+ ")
    )
    try:
        new = _literals(fix) - _literals(original)
        ast.parse(added)  # only count literals on lines the fix actually added
        new &= _literals(added)
    except SyntaxError:
        new = _literals(fix) - _literals(original)
    failing = set()
    for ce in counterexamples:
        for arg in counterexample_args(ce):
            failing |= _sub_values(arg)
    return sorted(v for v in new & failing if v not in TRIVIAL)


def shifted(value: Any, k: int) -> Any:
    """Every int in the value moved by k (structure, order and start <= end kept)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value + k
    if isinstance(value, list):
        return [shifted(v, k) for v in value]
    if isinstance(value, tuple):
        return tuple(shifted(v, k) for v in value)
    if isinstance(value, dict):
        return {key: shifted(v, k) for key, v in value.items()}
    return value


def nearby_variants(
    counterexamples: list[dict[str, Any]], *, stateful: bool = False
) -> list[tuple[Any, ...]]:
    """For a class, only the method calls shift: a different constructor argument
    (e.g. a capacity of -2) can make the sequence invalid rather than nearby."""
    variants: dict[str, tuple[Any, ...]] = {}
    for ce in counterexamples:
        args = counterexample_args(ce)
        for k in SHIFTS:
            if stateful:
                first, *calls = args[0]
                variant = ([first, *shifted(calls, k)],)
            else:
                variant = shifted(args, k)
            if variant != args:
                variants.setdefault(repr(variant), variant)
    return list(variants.values())


# Verification
class FixVerifier:
    def __init__(
        self,
        sandbox: Sandbox,
        *,
        max_examples: int = 500,
        call_timeout_s: float = 1.0,
        batches: int = 5,
        budget_s: float | None = None,
    ):
        self.sandbox = sandbox
        self.max_examples = max_examples
        self.call_timeout_s = call_timeout_s
        self.budget_s = budget_s
        self.batches = max(1, min(batches, max_examples))

    async def verify(
        self, session: Session, fix: str, on_progress: Progress | None = None
    ) -> FixReport:
        try:
            names = defined_names(fix)
        except SyntaxError as exc:
            return FixReport("invalid", f"SyntaxError on line {exc.lineno}: {exc.msg}")
        if session.function not in names:
            shown = session.function if session.stateful else f"{session.function}()"
            return FixReport("invalid", f"the fix must still define {shown}")
        if session.strategy_code is None:
            return FixReport("invalid", "there are no tests to check against yet")

        # The first batch also runs the hand-picked inputs; the rest only fresh ones.
        originals = [counterexample_args(ce) for ce in session.counterexamples]
        size = math.ceil(self.max_examples / self.batches)
        sizes = [size] * (self.max_examples // size) + (
            [self.max_examples % size] if self.max_examples % size else []
        )
        configs = [
            {
                "function": session.function,
                "predicates": {p.statement: p.predicate for p in session.properties},
                "examples": [],
                "originals": [],
                "variants": [],
                "max_examples": n,
                "seed": random.randrange(1, 2**31),  # fresh inputs every time
                "call_timeout_s": self.call_timeout_s,
                "budget_s": self.budget_s,
            }
            for n in sizes
        ]
        configs[0] |= {
            "examples": originals + [case.args for case in edge_cases(session.params)],
            "originals": originals,
            "variants": nearby_variants(session.counterexamples, stateful=session.stateful),
        }

        checked = 0
        reports = []
        runs = [self._run(session, fix, c, first=i == 0) for i, c in enumerate(configs)]
        for done in asyncio.as_completed(runs):
            report = await done
            if isinstance(report, FixReport):  # timed out or crashed
                return report
            reports.append(report)
            checked += report["max_examples"]
            if on_progress is not None:
                on_progress(checked, self.max_examples)

        first = next(r for r in reports if r["first"])
        base = FixReport(
            "passed",
            inputs_checked=sum(r["inputs_checked"] for r in reports),
            original_failures=first["original_failures"],
        )
        if found := [ce for r in reports for ce in r["counterexamples"]]:
            order = [p.statement for p in session.properties]
            base.outcome, base.reason = "failed", "a property still fails"
            base.counterexamples = [
                ce.to_dict() for ce in dedupe([Counterexample(**d) for d in found], order)
            ]
        elif any(r["out_of_time"] for r in reports):
            # Not every input was tried, so the fix isn't verified (it is very slow,
            # or loops forever on inputs a smaller time limit cut short).
            base.outcome, base.reason = "failed", "the tests ran out of time on your fix"
        elif first["variant_failures"]:
            base.outcome = "special_cased"
            base.reason = "it fails on inputs right next to the original failing one"
            base.counterexamples = first["variant_failures"][:1]
        elif hard := hardcoded_literals(session.code, fix, session.counterexamples):
            base.outcome, base.reason, base.hardcoded = (
                "special_cased",
                "it hard-codes the failing input",
                hard,
            )
        return base

    async def _run(
        self, session: Session, fix: str, config: dict[str, Any], *, first: bool
    ) -> dict[str, Any] | FixReport:
        harness = (
            "from hypothesis import strategies as st\n"
            "from sparring_partner.testing.harness import verify_fix\n\n"
            f"{session.strategy_code}\n\n\n"
            f"CONFIG = {config!r}\n\n\n"
            "def run(solution, inputs):\n"
            "    return verify_fix(solution, CONFIG, strategy)\n"
        )
        result = await self.sandbox.run(fix, harness)
        if result.timed_out:
            return FixReport("failed", "the tests timed out on your fix")
        if result.error:
            return FixReport("failed", f"your fix doesn't run: {result.error.type}")
        return {
            **result.value,
            "max_examples": config["max_examples"],
            "first": first,
        }


# The next untested risk
class RiskDraft(BaseModel):
    question: str = Field(description="one short question, e.g. 'What if the input isn't sorted?'")
    example_args: str = Field(
        description="an argument tuple that exercises it, as a Python literal"
    )


async def next_risk(
    llm: JsonLLM, sandbox: Sandbox, session: Session, *, call_timeout_s: float = 1.0
) -> tuple[str, dict[str, Any] | None]:
    """A risk the tests may not have covered, and its counterexample if the code fails it."""
    tested = sorted({case.label for case in edge_cases(session.params)})
    shape = (
        "\nThe example input is an argument tuple holding one operation list: "
        + OPERATIONS.format(cls=session.function, example="...").replace("args[0]", "it")
        if session.stateful
        else ""
    )
    messages: list[Message] = [
        {"role": "system", "content": "You are a sharp code reviewer. Reply with JSON only."},
        {
            "role": "user",
            "content": (
                f"Intent: {session.intent}\n\nThe learner's fixed code:\n```python\n"
                f"{session.code}\n```\n\nTheir spec:\n"
                + "\n".join(f"- {p.statement}" for p in session.properties)
                + f"\n\nEdge cases already tested: {', '.join(tested) or '(none)'}\n\n"
                "Name ONE input risk these tests may not cover, as a short question to the "
                "learner (e.g. 'What if the input isn't sorted?'), and an example input that "
                "exercises it. Don't say whether the code handles it." + shape
            ),
        },
    ]
    draft = await llm.complete_json("deep", messages, RiskDraft)
    args = _literal_args(draft.example_args, len(session.params) or 1)
    # Not strict: a risk is often an input the code assumed away, like unsorted input.
    if args is None or not args_fit(args, session.params, session.methods, strict=False):
        return draft.question, None
    config = {
        "function": session.function,
        "predicates": {p.statement: p.predicate for p in session.properties},
        "inputs": [args],
        "call_timeout_s": call_timeout_s,
    }
    harness = (
        "from sparring_partner.testing.harness import check_inputs\n\n"
        f"CONFIG = {config!r}\n\n\n"
        "def run(solution, inputs):\n"
        "    return check_inputs(solution, CONFIG)\n"
    )
    result = await sandbox.run(session.code, harness)
    if not result.ok:
        return draft.question, None
    failure = result.value["failures"].get("0")
    return draft.question, failure
