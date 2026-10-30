"""The hint ladder: the deep model drafts one hint per level from the private diagnosis.

Every draft passes the hint gate. A blocked draft goes back to the writer with
the gate's reason (up to MAX_RETRIES times); after that a templated fallback is
used, which must pass the gate's deterministic layers.
"""

import logging
from typing import Any

from pydantic import BaseModel, Field

from sparring_partner.agents import tracer
from sparring_partner.agents.diagnosis import Diagnoser
from sparring_partner.agents.gate import LEVEL_ALLOWS, NEVER, HintGate
from sparring_partner.agents.predict import case_outcome, current_case, describe_actual
from sparring_partner.agents.testgen import JsonLLM
from sparring_partner.llm import Message
from sparring_partner.session import Session

logger = logging.getLogger(__name__)

MAX_RETRIES = 2

SYSTEM = (
    "You are a Socratic debugging coach. You know exactly where the bug is and how to fix "
    "it, but you never tell: you give the learner just enough to find it themselves. "
    "Reply with JSON only."
)

DO_NOT_REVEAL = f"""\
Rules for every hint:
- Never reveal {NEVER}. Never write code or say what to change anything to.
- Never go beyond this level; the learner can ask for the next one.
- At most 3 short sentences, friendly, ideally ending with a question.
- Don't repeat earlier hints."""

LEVEL_TASKS = {
    1: (
        "Level 1, the symptom: state the smallest failing input, what the code returns, and "
        "which property that breaks. Do not say where in the code the problem is, or why, "
        "and don't mention the code's structure (loops, conditions) or variable names."
    ),
    2: (
        "Level 2, the region: say which function or block contains the bug ({region}). Do "
        "not give line numbers, the cause, the rule being broken, or variable values."
    ),
    3: (
        "Level 3, the evidence: a table of the variable values while the failing call runs "
        "is shown right after your text (don't repeat it). In 1-2 sentences, tell the learner "
        "to look at the row marked ◀ and ask what they expected the values to be there. Do "
        "not describe what the code checks or compares, explain the cause, name the rule, "
        "point at a boundary case (e.g. equal values), or give line numbers."
    ),
    4: (
        "Level 4, the concept: explain the rule being broken ({concept}) in general terms, "
        "and what kind of thing in the code to re-check. No code, no line numbers, no "
        "corrected expression or operator."
    ),
    5: (
        "Level 5, the line: name the exact line (line {line}: `{line_text}`) and ask what it "
        "should do for the failing input. Do not give the corrected line or any code."
    ),
}


class HintDraft(BaseModel):
    text: str = Field(description="the hint, in plain prose")


class HintWriter:
    def __init__(self, llm: JsonLLM, gate: HintGate, diagnoser: Diagnoser):
        self.llm = llm
        self.gate = gate
        self.diagnoser = diagnoser

    async def write(self, session: Session, level: int) -> str:
        """A gate-approved hint for `level` (the session must already be at that level)."""
        evidence = await self.evidence(session) if level == 3 else None
        messages: list[Message] = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": self._prompt(session, level, evidence)},
        ]
        for attempt in range(MAX_RETRIES + 1):
            try:
                draft = await self.llm.complete_json("deep", messages, HintDraft)
            except Exception:
                logger.exception("hint writer failed at level %d", level)
                break
            text = _compose(draft.text, evidence, level)
            decision = await self.gate.check(session, text, level, kind="hint")
            if decision.allowed:
                return text
            logger.info("hint attempt %d blocked: %s", attempt + 1, decision.reason)
            messages += [
                {"role": "assistant", "content": draft.model_dump_json()},
                {
                    "role": "user",
                    "content": f"Blocked by the reviewer: {decision.reason}. Rewrite the hint "
                    f"so it stays within level {level} and gives nothing more away.",
                },
            ]
        return await self._fallback(session, level, evidence)

    async def evidence(self, session: Session) -> dict[str, Any] | None:
        """The windowed trace for the current failing case (computed once, then cached)."""
        if session.evidence is None and session.trace:
            d = session.diagnosis
            fix_trace = None
            if d is not None and d.verified:
                fix_trace = await self.diagnoser.trace(session, code=d.reference_fix)
            found = tracer.divergence(session.trace, fix_trace)
            anchor, differ = found if found else (None, set())
            session.evidence = tracer.window(
                session.trace, anchor=anchor, line=d.line if d else None, highlight=differ
            )
        return session.evidence

    async def _fallback(self, session: Session, level: int, evidence: dict[str, Any] | None) -> str:
        for text in (
            fallback_hint(session, level, evidence),
            generic_hint(session, level, evidence),
        ):
            decision = await self.gate.check(
                session, text, level, kind="hint_fallback", use_llm=False
            )
            if decision.allowed:
                return text
        return generic_hint(session, level, evidence)  # nothing safer exists

    def _prompt(self, session: Session, level: int, evidence: dict[str, Any] | None) -> str:
        d = session.diagnosis
        assert d is not None
        ce = current_case(session)
        numbered = "\n".join(
            f"{i:>3} | {line}" for i, line in enumerate(session.code.splitlines(), start=1)
        )
        earlier = [m["text"] for m in session.messages if m["kind"] == "hint"]
        task = LEVEL_TASKS[level].format(
            region=d.region, concept=d.concept, line=d.line, line_text=_line_text(session, d.line)
        )
        allowed = "\n".join(f"- level {n}: {LEVEL_ALLOWS[n]}" for n in range(level + 1))
        trace = (
            "\n\nThe trace table shown to the learner:\n"
            + tracer.render(evidence, show_lines=False)
            if evidence
            else ""
        )
        return (
            f"Intent: {session.intent}\nThe learner's spec:\n"
            + "\n".join(f"- {p.statement}" for p in session.properties)
            + f"\n\nCode:\n{numbered}\n\n"
            f"Failing case: `{ce['call']}` {_actual(session, ce)}; breaks: "
            f"{'; '.join(ce['violated_properties'])}\n\n"
            f"What you know (SECRET):\n- root cause: {d.root_cause}\n- region: {d.region}\n"
            f"- concept: {d.concept}\n- bug line: {d.line}\n- fix:\n{d.reference_fix}\n\n"
            f"What the learner may know so far:\n{allowed}\n\n"
            f"Earlier hints:\n" + ("\n".join(f"- {t}" for t in earlier) or "(none)") + "\n\n"
            f"Task: {task}{trace}\n\n{DO_NOT_REVEAL}"
        )


def fallback_hint(session: Session, level: int, evidence: dict[str, Any] | None) -> str:
    """The templated hint for each level, built from the diagnosis."""
    d = session.diagnosis
    assert d is not None
    ce = current_case(session)
    broken = ce["violated_properties"][0]
    if level == 1:
        return (
            f"Try `{ce['call']}`. It {_actual(session, ce)}, but your spec says: "
            f'"{broken.rstrip(".")}".'
        )
    if level == 2:
        return f"The problem is inside {d.region}."
    if level == 3:
        return (
            f"Here's what the variables hold while `{ce['call']}` runs. "
            f"Look at the row marked ◀.\n\n" + _table(evidence, level)
        )
    if level == 4:
        return f"The rule at stake: {d.concept}"
    line_text = _line_text(session, d.line)
    return f"Look at line {d.line}: `{line_text}`. What should it do for `{ce['call']}`?"


def generic_hint(session: Session, level: int, evidence: dict[str, Any] | None) -> str:
    """Last resort: says as little as each level allows."""
    ce = current_case(session)
    return {
        1: f"Try `{ce['call']}` and compare the result with your spec.",
        2: f"The problem is inside {_target(session)}.",
        3: "Here's what the variables hold as it runs:\n\n" + _table(evidence, level),
        4: "Re-read your spec and ask which rule the failing input breaks.",
        5: f"Look at line {session.diagnosis.line if session.diagnosis else '?'}.",
    }[level]


def _compose(text: str, evidence: dict[str, Any] | None, level: int) -> str:
    return (
        f"{text.strip()}\n\n{_table(evidence, level)}" if level == 3 and evidence else text.strip()
    )


def _table(evidence: dict[str, Any] | None, level: int) -> str:
    if not evidence:
        return "(no trace available)"
    return tracer.render(evidence, show_lines=level >= 5)


def _actual(session: Session, ce: dict[str, Any]) -> str:
    """'returns `-1`'; for a class, what its last call returns."""
    return describe_actual(case_outcome(session, ce))


def _target(session: Session) -> str:
    return f"the {session.function} class" if session.stateful else f"{session.function}()"


def _line_text(session: Session, line: int | None) -> str:
    lines = session.code.splitlines()
    return lines[line - 1].strip() if line and 1 <= line <= len(lines) else ""
