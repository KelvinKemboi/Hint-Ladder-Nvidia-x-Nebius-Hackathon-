"""Propose the properties the code should satisfy, as statements plus executable predicates."""

import ast
import logging
from typing import Any

from pydantic import BaseModel, Field

from sparring_partner.agents.code_info import FunctionInfo
from sparring_partner.agents.errors import UserCodeError
from sparring_partner.agents.testgen import (
    CONVENTIONS,
    JsonLLM,
    clean_code,
    edge_cases,
    predicate_check_harness,
)
from sparring_partner.llm import Message
from sparring_partner.sandbox import Sandbox
from sparring_partner.session import PropertySpec, Session

logger = logging.getLogger(__name__)

SYSTEM = (
    "You are a careful test engineer writing executable specifications for Python functions. "
    "Reply with JSON only."
)

PREDICATE_RULES = """\
Predicate rules:
- Python source defining exactly `def check(args, output):` that returns True when the property \
holds and False when it doesn't.
- `args` is a TUPLE of the positional arguments ({params}); `output` is the return value.
- Write normal multi-line Python with 4-space indentation. Never chain statements on one line \
with semicolons.
- Derive what must be true from the INTENT, not from the code: the code may be buggy. Never call \
the function under test and never copy its logic.
- Return a real bool. Never raise: handle an empty input AND an empty output (min()/max() of an \
empty sequence raise) and a wrongly typed output (guard with isinstance where needed).
- {conventions}
- Standard library only (import inside check if needed). No printing, randomness or mutating args.
- Short and obviously correct."""


class PropertyDraft(BaseModel):
    statement: str = Field(description="one plain-English sentence a student can check by hand")
    predicate: str = Field(description="Python source defining check(args, output)")


class Proposal(BaseModel):
    properties: list[PropertyDraft] = Field(min_length=3, max_length=6)


class Predicates(BaseModel):
    properties: list[PropertyDraft]


class PropertyValidationError(Exception):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(f"{s}: {e}" for s, e in errors.items()))
        self.errors = errors  # statement -> why its predicate doesn't work


class PropertyProposer:
    def __init__(self, llm: JsonLLM, sandbox: Sandbox):
        self.llm = llm
        self.sandbox = sandbox

    async def draft(self, session: Session, fn: FunctionInfo) -> list[PropertySpec]:
        """3-6 candidate properties from the fast model (not yet validated)."""
        messages = self._messages(
            session,
            fn,
            "Propose 3 to 6 properties every correct implementation must satisfy. Include:\n"
            "- invariants of the output on its own (e.g. 'output intervals never overlap');\n"
            "- at least one property tying the output back to the input, so a function that "
            "loses or invents data fails it (for sorting, e.g. 'the output is a rearrangement "
            "of the input'). Shape checks alone miss most bugs.\n"
            "Each statement is one plain-English sentence a student can check by hand.",
        )
        proposal = await self.llm.complete_json("fast", messages, Proposal)
        return [
            PropertySpec(d.statement.strip(), clean_code(d.predicate)) for d in proposal.properties
        ]

    async def propose(
        self, session: Session, fn: FunctionInfo, drafts: list[PropertySpec]
    ) -> list[PropertySpec]:
        """Validate drafts, repair broken predicates once, and drop any still broken."""
        errors = await self.validate(session, drafts)
        if errors:
            drafts = await self._repair(session, fn, drafts, errors)
            errors = await self.validate(session, drafts)
        for p in drafts:
            if p.id in errors:
                logger.warning("dropping property %r: %s", p.statement, errors[p.id])
        return [p for p in drafts if p.id not in errors]

    async def regenerate(
        self, session: Session, fn: FunctionInfo, statements: list[str]
    ) -> list[PropertySpec]:
        """Predicates for user-written or edited statements. Raises if any stays broken."""
        if not statements:
            return []
        listing = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(statements))
        messages = self._messages(
            session,
            fn,
            "Write a predicate for each of these properties. Keep every statement exactly as "
            f"given, in the same order:\n{listing}",
        )
        specs = await self._ask_predicates(messages, statements)
        errors = await self.validate(session, specs)
        if errors:
            specs = await self._repair(session, fn, specs, errors)
            errors = await self.validate(session, specs)
        if errors:
            by_id = {p.id: p.statement for p in specs}
            raise PropertyValidationError({by_id[i]: e for i, e in errors.items()})
        return specs

    async def validate(self, session: Session, specs: list[PropertySpec]) -> dict[str, str]:
        """Property id -> error, for predicates that don't compile or misbehave on trivial input."""
        errors = {p.id: e for p in specs if (e := lint_predicate(p.predicate))}
        runnable = {p.id: p.predicate for p in specs if p.id not in errors}
        inputs = trivial_inputs(session)
        if runnable and (inputs or session.worked_examples):
            harness = predicate_check_harness(
                session.function, runnable, inputs, session.worked_examples
            )
            result = await self.sandbox.run(session.code, harness)
            if result.timed_out:
                raise UserCodeError("your code timed out on a trivial input")
            if result.error:
                raise UserCodeError(
                    f"your code failed to run: {result.error.type}: {result.error.message}"
                )
            errors |= result.value["errors"]
        for spec in specs:
            spec.error = errors.get(spec.id)
        return errors

    async def _repair(
        self, session: Session, fn: FunctionInfo, specs: list[PropertySpec], errors: dict[str, str]
    ) -> list[PropertySpec]:
        broken = [p for p in specs if p.id in errors]
        listing = "\n".join(
            f"{i + 1}. {p.statement}\n   predicate:\n{p.predicate}\n   error: {errors[p.id]}"
            for i, p in enumerate(broken)
        )
        messages = self._messages(
            session,
            fn,
            "These predicates failed when run on a trivial input. Rewrite them, keeping every "
            f"statement exactly as given, in the same order:\n{listing}",
        )
        fixed = await self._ask_predicates(messages, [p.statement for p in broken])
        replacements = {old.id: new for old, new in zip(broken, fixed, strict=True)}
        for old_id, new in replacements.items():
            new.id = old_id
        return [replacements.get(p.id, p) for p in specs]

    async def _ask_predicates(
        self, messages: list[Message], statements: list[str]
    ) -> list[PropertySpec]:
        reply = await self.llm.complete_json("fast", messages, Predicates)
        drafts = reply.properties
        if len(drafts) != len(statements):
            raise PropertyValidationError(
                {s: "the model did not return a predicate" for s in statements}
            )
        # Keep the user's wording even if the model rephrased it.
        return [
            PropertySpec(s, clean_code(d.predicate))
            for s, d in zip(statements, drafts, strict=True)
        ]

    def _messages(self, session: Session, fn: FunctionInfo, task: str) -> list[Message]:
        params = ", ".join(f"args[{i}] = {p.name}" for i, p in enumerate(fn.params))
        return [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Intent: {session.intent}\nSignature: {fn.signature()}\n"
                    f"Docstring: {fn.docstring or '(none)'}\n\n"
                    f"Code:\n```python\n{session.code}\n```\n\n{task}\n\n"
                    + PREDICATE_RULES.format(params=params, conventions=CONVENTIONS)
                ),
            },
        ]


def lint_predicate(source: str) -> str | None:
    try:
        tree = ast.parse(source)
        compile(source, "<property>", "exec")
    except SyntaxError as exc:
        return f"SyntaxError on line {exc.lineno}: {exc.msg}" + _ONE_LINER_HINT * _one_liner(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "check":
            if len(node.args.args) != 2:
                return "check must take exactly two parameters: (args, output)"
            return None
    return "must define check(args, output)"


_ONE_LINER_HINT = ". The code is on a single line: write it over several lines with indentation"


def _one_liner(source: str) -> bool:
    return "\n" not in source.strip()


def trivial_inputs(session: Session) -> list[tuple[Any, ...]]:
    """The model's example input plus the first couple of edge cases."""
    inputs = [session.example_args] if session.example_args is not None else []
    inputs += [case.args for case in edge_cases(session.params)[:2]]
    return inputs
