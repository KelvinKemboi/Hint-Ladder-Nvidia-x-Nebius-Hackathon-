"""The private diagnosis: root cause, line, concept and a verified reference fix.

The deep model works it out from the code, intent, properties, counterexamples
and a trace of the failing call. The reference fix is only accepted once it
passes the whole bug hunt in the sandbox, including every recorded
counterexample. None of this is ever sent to the client.
"""

import ast
import logging
from typing import Any

from pydantic import BaseModel, Field

from sparring_partner.agents.code_info import find_functions
from sparring_partner.agents.testgen import (
    CONVENTIONS,
    EdgeCase,
    JsonLLM,
    clean_code,
    edge_cases,
    exploration_harness,
)
from sparring_partner.llm import Message
from sparring_partner.sandbox import Sandbox
from sparring_partner.session import Diagnosis, Session

logger = logging.getLogger(__name__)

MAX_TRACE_STEPS = 60  # shown to the model

SYSTEM = (
    "You are an expert debugger. You find the root cause of a bug and write the smallest "
    "correct fix. Reply with JSON only."
)


class DiagnosisDraft(BaseModel):
    root_cause: str = Field(description="what goes wrong and why, 1-3 sentences")
    line: int = Field(description="1-based line number of the bug in the ORIGINAL code")
    region: str = Field(description="the function or block containing the bug")
    bug_class: str = Field(
        description="short label, e.g. 'boundary condition', 'off-by-one', "
        "'wrong input assumption', 'mutation during iteration', 'missing edge case'"
    )
    concept: str = Field(description="the general rule being broken, one sentence, no code")
    reference_fix: str = Field(description="the COMPLETE corrected code (the whole file)")


class Diagnoser:
    def __init__(
        self,
        llm: JsonLLM,
        sandbox: Sandbox,
        *,
        max_examples: int = 200,
        call_timeout_s: float = 1.0,
    ):
        self.llm = llm
        self.sandbox = sandbox
        self.max_examples = max_examples
        self.call_timeout_s = call_timeout_s

    async def trace(self, session: Session, code: str | None = None) -> dict[str, Any] | None:
        """Line-by-line locals for the top counterexample, recorded in the sandbox.

        `code` traces other code (e.g. the reference fix) on the same input.
        """
        if not session.counterexamples:
            return None
        config = {
            "function": session.function,
            "args": counterexample_args(session.counterexamples[0]),
            "call_timeout_s": self.call_timeout_s,
            "max_steps": 200,
        }
        harness = (
            "from sparring_partner.testing.harness import trace_call\n\n"
            f"CONFIG = {config!r}\n\n\n"
            "def run(solution, inputs):\n"
            "    return trace_call(solution, CONFIG)\n"
        )
        result = await self.sandbox.run(code or session.code, harness)
        if not result.ok:
            logger.warning("tracing failed: %s", result.error or "timed out")
            return None
        return result.value

    async def diagnose(self, session: Session, attempts: int = 3) -> Diagnosis:
        """Ask the deep model, verify its fix in the sandbox, and retry with the failures.

        Returns the last attempt with `verified=False` if no fix passed.
        """
        messages: list[Message] = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": self._prompt(session)},
        ]
        diagnosis: Diagnosis | None = None
        for attempt in range(1, attempts + 1):
            draft = await self.llm.complete_json("deep", messages, DiagnosisDraft)
            fix = clean_code(draft.reference_fix)
            diagnosis = Diagnosis(
                root_cause=draft.root_cause,
                line=draft.line,
                region=draft.region,
                bug_class=draft.bug_class,
                concept=draft.concept,
                reference_fix=fix,
                attempts=attempt,
            )
            problems = self._check_static(session, draft, fix) or await self._check_fix(
                session, fix
            )
            if not problems:
                diagnosis.verified = True
                return diagnosis
            logger.info("diagnosis attempt %d rejected: %s", attempt, problems)
            messages += [
                {"role": "assistant", "content": draft.model_dump_json()},
                {
                    "role": "user",
                    "content": "That diagnosis was rejected:\n- "
                    + "\n- ".join(problems)
                    + "\nFix the problems and reply with the full corrected JSON.",
                },
            ]
        assert diagnosis is not None
        return diagnosis

    def _check_static(self, session: Session, draft: DiagnosisDraft, fix: str) -> list[str]:
        problems = []
        lines = session.code.count("\n") + 1
        if not 1 <= draft.line <= lines:
            problems.append(f"line must be between 1 and {lines} (the original code's lines)")
        try:
            functions = {fn.name for fn in find_functions(fix)}
        except SyntaxError as exc:
            return [*problems, f"reference_fix has a SyntaxError on line {exc.lineno}: {exc.msg}"]
        if session.function not in functions:
            problems.append(f"reference_fix must still define {session.function}()")
        if _normalized(fix) == _normalized(session.code):
            problems.append("reference_fix is identical to the original code")
        return problems

    async def _check_fix(self, session: Session, fix: str) -> list[str]:
        """Run the full bug hunt against the fix; every counterexample must now pass."""
        if session.strategy_code is None:
            return []
        cases = edge_cases(session.params) + [
            EdgeCase("counterexample", counterexample_args(ce)) for ce in session.counterexamples
        ]
        harness = exploration_harness(
            session,
            session.strategy_code,
            cases,
            max_examples=self.max_examples,
            call_timeout_s=self.call_timeout_s,
            seed=0,
        )
        result = await self.sandbox.run(fix, harness)
        if result.timed_out:
            return ["the reference fix timed out on the tests"]
        if result.error:
            return [f"the reference fix failed to run: {result.error.type}: {result.error.message}"]
        return [
            f"still fails: {ce['call']} -> "
            + (ce["output"] if ce["output"] is not None else f"raises {ce['exception_type']}")
            + f" (breaks: {'; '.join(ce['violated_properties'])})"
            for ce in result.value["counterexamples"][:3]
        ]

    def _prompt(self, session: Session) -> str:
        numbered = "\n".join(
            f"{i:>3} | {line}" for i, line in enumerate(session.code.splitlines(), start=1)
        )
        properties = "\n".join(f"- {p.statement}" for p in session.properties)
        first, *others = session.counterexamples
        other_failures = "\n".join(f"- {_describe(ce)}" for ce in others) or "(none)"
        return (
            f"Intent: {session.intent}\n\n"
            f"Code (line numbers added):\n{numbered}\n\n"
            f"Properties a correct {session.function}() must satisfy:\n{properties}\n\n"
            f"Failing case to diagnose:\n{_describe(first)}\n\n"
            f"Other failures (the fix must handle these too):\n{other_failures}\n\n"
            f"Trace of the failing case (locals before each line runs):\n"
            f"{render_trace(session.trace)}\n\n"
            "Diagnose the root cause of the failing case. reference_fix is the COMPLETE "
            "corrected code: change as little as possible, keep the function name and "
            "signature, and make every property hold on every input, fixing all failures "
            f"above. {CONVENTIONS}"
        )


def counterexample_args(ce: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(ast.literal_eval(arg) for arg in ce["args"])


def render_trace(trace: dict[str, Any] | None, max_steps: int = MAX_TRACE_STEPS) -> str:
    if not trace or not trace["steps"]:
        return "(not available)"
    steps = trace["steps"]
    shown = steps[:max_steps]
    lines = [
        f"line {s['line']}: " + (", ".join(f"{k}={v}" for k, v in s["locals"].items()) or "-")
        for s in shown
    ]
    if len(steps) > len(shown) or trace["truncated"]:
        lines.append(f"... (trace cut off after {len(shown)} steps)")
    result = trace["exception"] and f"raised {trace['exception']}"
    lines.append(f"=> {result or 'returned ' + str(trace['output'])}")
    return "\n".join(lines)


def _describe(ce: dict[str, Any]) -> str:
    if ce["exception_type"]:
        line = f" on line {ce['exception_line']}" if ce["exception_line"] else ""
        outcome = f"raises {ce['exception_type']}: {ce['exception_message']}{line}"
    else:
        outcome = f"returns {ce['output']}"
    return f"{ce['call']} {outcome}; breaks: {'; '.join(ce['violated_properties'])}"


def _normalized(code: str) -> str:
    try:
        return ast.dump(ast.parse(code))
    except SyntaxError:
        return code.strip()
