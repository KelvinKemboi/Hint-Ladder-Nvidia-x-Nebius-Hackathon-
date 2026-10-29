"""AI Code Review mode: checks for the usual failures of AI-written code.

Static checks (AST): invented APIs (resolved against the modules installed in
the sandbox), undefined names, and swallowed exceptions. Test-based checks:
which hand-picked edge cases fail reveals assumptions about the input. And an
LLM review against the intent and spec, whose suggested inputs are run: failing
ones join the counterexamples, so they come back as predict-then-reveal
questions. Every finding is phrased as a question; the orchestrator sends it
through the hint gate.
"""

import ast
import builtins
import logging
from dataclasses import asdict, dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from sparring_partner.agents.testgen import JsonLLM, _literal_args, edge_cases
from sparring_partner.llm import Message
from sparring_partner.sandbox import Sandbox
from sparring_partner.session import Session

logger = logging.getLogger(__name__)

BROAD = {"Exception", "BaseException"}
# Edge-case label -> the assumption a failure there reveals.
ASSUMPTIONS = {
    "empty": "an empty input",
    "single element": "a single element",
    "single interval": "a single interval",
    "single character": "a single character",
    "single key": "a single key",
    "duplicates": "duplicates",
    "unsorted": "input that isn't sorted",
    "negatives": "negative numbers",
    "negative": "negative numbers",
    "huge values": "very large numbers",
    "huge": "very large numbers",
    "touching ranges": "ranges that only touch at an endpoint",
    "nested": "one range inside another",
    "zero width": "zero-width ranges",
    "whitespace": "whitespace",
    "unicode": "non-ASCII text",
}


@dataclass
class Finding:
    kind: str  # invented_api, undefined_name, swallowed_exception, assumption, or an LLM kind
    question: str  # what the learner sees (after the gate)
    detail: str  # server-only: what is actually wrong
    llm_written: bool = False
    counterexample: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Static checks
def swallowed_exceptions(code: str) -> list[Finding]:
    """Bare `except:`, `except Exception: pass`, and broad catches that return a default."""
    findings = []
    for node in ast.walk(ast.parse(code)):
        if not isinstance(node, ast.ExceptHandler) or not _is_broad(node.type):
            continue
        raises = any(isinstance(n, ast.Raise) for n in ast.walk(node))
        default = next(
            (n.value for n in ast.walk(node) if isinstance(n, ast.Return) and _is_default(n.value)),
            False,
        )
        if raises:
            continue
        if all(_is_noop(stmt) for stmt in node.body):
            question = "If something goes wrong inside that `try`, does anyone ever find out?"
            detail = f"line {node.lineno}: a broad except silently ignores every error"
        elif default is not False:
            shown = "None" if default is None else ast.unparse(default)
            question = (
                f"When an error happens, your code quietly returns `{shown}`. "
                "Could a caller tell that apart from a real answer?"
            )
            detail = f"line {node.lineno}: a broad except hides errors behind a default ({shown})"
        elif node.type is None:
            question = (
                "Your code catches every possible exception there, even ones nobody expected. "
                "Which errors did you actually mean to handle?"
            )
            detail = f"line {node.lineno}: bare except"
        else:
            continue
        findings.append(Finding("swallowed_exception", question, detail))
    return findings


def _is_broad(type_node: ast.expr | None) -> bool:
    if type_node is None:
        return True
    if isinstance(type_node, ast.Name):
        return type_node.id in BROAD
    if isinstance(type_node, ast.Tuple):
        return any(_is_broad(t) for t in type_node.elts)
    return False


def _is_noop(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Pass | ast.Continue) or (
        isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
    )


def _is_default(value: ast.expr | None) -> bool:
    if value is None or isinstance(value, ast.Constant):
        return True
    return isinstance(value, ast.List | ast.Tuple | ast.Dict | ast.Set) and not getattr(
        value, "elts", getattr(value, "keys", [])
    )


def undefined_names(code: str) -> list[Finding]:
    """Names read but never defined, imported or built in (scope-insensitive)."""
    tree = ast.parse(code)
    defined = set(dir(builtins)) | {"__name__", "__file__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            defined.add(node.id)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            defined.add(node.name)
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            defined |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
        elif isinstance(node, ast.Global | ast.Nonlocal):
            defined |= set(node.names)
    seen: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in defined:
            seen.setdefault(node.id, node.lineno)
    return [
        Finding(
            "undefined_name",
            f"Where is `{name}` defined? What happens when your code reaches it?",
            f"line {line}: {name} is never defined or imported",
        )
        for name, line in seen.items()
    ]


def api_references(code: str) -> list[dict[str, Any]]:
    """Imported modules and the attributes the code uses on them: module + attribute chain."""
    tree = ast.parse(code)
    aliases: dict[str, str] = {}
    refs: dict[str, dict[str, Any]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:  # import numpy as np -> np means numpy
                    aliases[a.asname] = a.name
                else:  # import os.path -> os means os
                    root = a.name.split(".")[0]
                    aliases[root] = root
                refs.setdefault(a.name, {"module": a.name, "attrs": [], "line": node.lineno})
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for a in node.names:
                key = f"{node.module}.{a.name}"
                refs.setdefault(
                    key, {"module": node.module, "attrs": [a.name], "line": node.lineno}
                )
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            chain, base = [], node
            while isinstance(base, ast.Attribute):
                chain.append(base.attr)
                base = base.value
            if isinstance(base, ast.Name) and base.id in aliases:
                attrs = list(reversed(chain))
                key = ".".join([aliases[base.id], *attrs])
                refs.setdefault(
                    key, {"module": aliases[base.id], "attrs": attrs, "line": node.lineno}
                )
    # Keep only the longest chain of each family (os.path.joinn covers os.path).
    keys = sorted(refs)
    return [refs[k] for k in keys if not any(o.startswith(k + ".") for o in keys)]


# Review 
class Concern(BaseModel):
    kind: Literal[
        "intent_mismatch", "missing_edge_case", "wrong_assumption", "invented_api",
        "swallowed_exception",
    ]  # fmt: skip
    detail: str = Field(description="what is wrong, for the system only (never shown)")
    question: str = Field(
        description="a Socratic question for the learner: no answer, no code, no line numbers"
    )
    example_args: str = Field(
        default="", description="an argument tuple that exercises it, as a Python literal, or ''"
    )


class ReviewDraft(BaseModel):
    concerns: list[Concern] = Field(max_length=5)


class Reviewer:
    def __init__(self, llm: JsonLLM, sandbox: Sandbox, *, call_timeout_s: float = 1.0):
        self.llm = llm
        self.sandbox = sandbox
        self.call_timeout_s = call_timeout_s

    async def review(self, session: Session) -> list[Finding]:
        """All findings; failing inputs the LLM suggests are added to the counterexamples."""
        findings = swallowed_exceptions(session.code) + undefined_names(session.code)
        findings += await self._invented_apis(session)
        findings += await self._assumptions(session)
        findings += await self._conformance(session, findings)
        session.findings = [f.to_dict() for f in findings]
        return findings

    async def _invented_apis(self, session: Session) -> list[Finding]:
        refs = api_references(session.code)
        if not refs:
            return []
        result = await self._run("resolve_apis", "", {"references": refs})
        return [
            Finding(
                "invented_api",
                f"Does `{'.'.join([m['module'], *m['attrs']])}` actually exist? "
                "What happens when your code reaches it?",
                f"line {m['line']}: {m['error']}",
            )
            for m in (result or {}).get("missing", [])
        ]

    async def _assumptions(self, session: Session) -> list[Finding]:
        cases = edge_cases(session.params)
        failures = await self._failures(session, [c.args for c in cases])
        findings, phrases = [], set()
        for index, ce in failures.items():
            label = cases[index].label.split(": ")[-1]
            phrase = ASSUMPTIONS.get(label)
            if phrase and phrase not in phrases:
                phrases.add(phrase)
                findings.append(
                    Finding(
                        "assumption",
                        f"Does your code handle {phrase}, like `{ce['call']}`?",
                        f"fails on {label}: {ce['call']} -> {ce['output'] or ce['exception_type']}",
                        counterexample=ce,
                    )
                )
        return findings

    async def _conformance(self, session: Session, so_far: list[Finding]) -> list[Finding]:
        numbered = "\n".join(
            f"{i:>3} | {line}" for i, line in enumerate(session.code.splitlines(), start=1)
        )
        known = "\n".join(f"- {f.detail}" for f in so_far) or "(none)"
        messages: list[Message] = [
            {
                "role": "system",
                "content": "You review code written by an AI assistant for a learner who "
                "must find the problems themselves. Reply with JSON only.",
            },
            {
                "role": "user",
                "content": (
                    f"Intent: {session.intent}\nThe learner's spec:\n"
                    + "\n".join(f"- {p.statement}" for p in session.properties)
                    + f"\n\nCode:\n{numbered}\n\nAlready found:\n{known}\n\n"
                    "List up to 5 NEW concerns where the code doesn't do what the intent and "
                    "spec ask: missing edge cases, wrong assumptions about the input, invented "
                    "APIs, swallowed errors. For each, a question that leads the learner to it "
                    "without giving the answer, and an input that exercises it."
                ),
            },
        ]
        try:
            draft = await self.llm.complete_json("deep", messages, ReviewDraft)
        except Exception:
            logger.exception("conformance review failed")
            return []

        inputs = {i: _literal_args(c.example_args, len(session.params) or 1)
                  for i, c in enumerate(draft.concerns)}  # fmt: skip
        runnable = {i: args for i, args in inputs.items() if args is not None}
        failures = await self._failures(session, list(runnable.values()))
        failing = {list(runnable)[j]: ce for j, ce in failures.items()}

        known_calls = {ce["call"] for ce in session.counterexamples}
        findings = []
        for i, concern in enumerate(draft.concerns):
            ce = failing.get(i)
            if ce is not None and ce["call"] not in known_calls:
                session.counterexamples.append(ce)  # comes back as a prediction question
                known_calls.add(ce["call"])
            findings.append(
                Finding(
                    concern.kind,
                    concern.question,
                    concern.detail,
                    llm_written=True,
                    counterexample=ce,
                )  # fmt: skip
            )
        return findings

    async def _failures(self, session: Session, inputs: list[Any]) -> dict[int, dict[str, Any]]:
        if not inputs:
            return {}
        config = {
            "function": session.function,
            "predicates": {p.statement: p.predicate for p in session.properties},
            "inputs": inputs,
        }
        result = await self._run("check_inputs", session.code, config)
        return {int(k): v for k, v in (result or {}).get("failures", {}).items()}

    async def _run(self, entry: str, code: str, config: dict[str, Any]) -> dict[str, Any] | None:
        config = {**config, "call_timeout_s": self.call_timeout_s}
        harness = (
            f"from sparring_partner.testing.harness import {entry}\n\n"
            f"CONFIG = {config!r}\n\n\n"
            "def run(solution, inputs):\n"
            f"    return {entry}(solution, CONFIG)\n"
        )
        result = await self.sandbox.run(code, harness)
        if not result.ok:
            logger.warning("review check %s failed: %s", entry, result.error or "timed out")
            return None
        return result.value
