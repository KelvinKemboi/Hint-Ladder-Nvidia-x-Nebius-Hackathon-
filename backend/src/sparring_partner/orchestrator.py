"""Drives a session through its states and publishes progress events."""

import asyncio
import logging
import time
from collections.abc import Coroutine
from typing import Any

from sparring_partner.agents.code_info import FunctionInfo, pick_function
from sparring_partner.agents.diagnosis import Diagnoser, counterexample_args
from sparring_partner.agents.errors import GenerationError, NotAvailable, UserCodeError
from sparring_partner.agents.gate import HintGate
from sparring_partner.agents.hints import HintWriter
from sparring_partner.agents.predict import (
    Prediction,
    current_case,
    describe_actual,
    parse_prediction,
    question,
)
from sparring_partner.agents.properties import PropertyProposer, PropertyValidationError
from sparring_partner.agents.testgen import (
    JsonLLM,
    edge_cases,
    exploration_harness,
    infer_inputs,
    write_strategy,
)
from sparring_partner.config import Settings, get_settings
from sparring_partner.sandbox import Sandbox
from sparring_partner.session import (
    MAX_HINT_LEVEL,
    InvalidTransition,
    Mode,
    PropertySpec,
    Session,
    SessionState,
    SessionStore,
)

logger = logging.getLogger(__name__)

S = SessionState


class Orchestrator:
    def __init__(
        self,
        store: SessionStore,
        llm: JsonLLM,
        sandbox: Sandbox,
        settings: Settings | None = None,
    ):
        self.store = store
        self.llm = llm
        self.sandbox = sandbox
        self.settings = settings or get_settings()
        self.proposer = PropertyProposer(llm, sandbox)
        self.diagnoser = Diagnoser(
            llm,
            sandbox,
            max_examples=self.settings.explore_max_examples,
            call_timeout_s=self.settings.explore_call_timeout_s,
        )
        self.gate = HintGate(llm, sandbox, call_timeout_s=self.settings.explore_call_timeout_s)
        self.hints = HintWriter(llm, self.gate, self.diagnoser)
        self._tasks: set[asyncio.Task[Any]] = set()

    async def create(
        self, code: str, intent: str, mode: Mode = Mode.DEBUG, function: str | None = None
    ) -> Session:
        """New session in the intent state, with validated candidate properties."""
        fn = _pick(code, intent, function)
        session = self.store.add(Session(code=code, intent=intent, function=fn.name, mode=mode))
        self._progress(session, f"Reading {fn.name}() and proposing properties")

        inputs, drafts = await asyncio.gather(
            infer_inputs(self.llm, fn, intent, code),
            self.proposer.draft(session, fn),
        )
        session.params = inputs.params
        session.example_args = inputs.example_args
        session.worked_examples = inputs.worked_examples
        session.properties = await self.proposer.propose(session, fn, drafts)
        if not session.properties:
            raise GenerationError("could not turn any proposed property into a working check")
        self._progress(
            session, f"Proposed {len(session.properties)} properties", count=len(session.properties)
        )
        return session

    async def confirm_properties(
        self, session_id: str, edits: list[tuple[str | None, str]]
    ) -> Session:
        """Apply the user's (id, statement) list, regenerate changed predicates, start the hunt.

        Unchanged properties (same id and statement) keep their predicate; new or
        edited statements get a fresh one. Properties left out are removed.
        """
        session = self.store.get(session_id)
        if session.state not in (S.INTENT, S.PROPERTIES_CONFIRMED):
            raise InvalidTransition(f"properties can't be changed in state {session.state}")
        if not edits:
            raise PropertyValidationError({"": "keep at least one property"})

        existing = {p.id: p for p in session.properties}
        kept: dict[int, PropertySpec] = {}
        changed: dict[int, str] = {}
        for i, (prop_id, statement) in enumerate(edits):
            statement = statement.strip()
            old = existing.get(prop_id) if prop_id else None
            if old and old.statement == statement and old.error is None:
                kept[i] = old
            else:
                changed[i] = statement

        if changed:
            self._progress(session, f"Writing checks for {len(changed)} edited properties")
        fn = _pick(session.code, session.intent, session.function)
        fresh = iter(await self.proposer.regenerate(session, fn, list(changed.values())))
        session.properties = [kept[i] if i in kept else next(fresh) for i in range(len(edits))]

        if session.state is S.INTENT:
            self._move(session, S.PROPERTIES_CONFIRMED)
        self._start(self.explore(session))
        return session

    async def explore(self, session: Session) -> None:
        """The secret bug hunt. Results stay server-side; events carry counts only."""
        self._move(session, S.EXPLORING)
        try:
            fn = _pick(session.code, session.intent, session.function)
            self._progress(session, "Writing an input generator")
            session.strategy_code = await write_strategy(self.llm, self.sandbox, session, fn)

            cases = edge_cases(session.params)
            max_examples = self.settings.explore_max_examples
            self._progress(
                session,
                f"Testing {len(session.properties)} properties on {max_examples} generated "
                f"inputs each, plus {len(cases)} edge cases",
                properties=len(session.properties),
                max_examples=max_examples,
                edge_cases=len(cases),
            )
            harness = exploration_harness(
                session,
                session.strategy_code,
                cases,
                max_examples=max_examples,
                call_timeout_s=self.settings.explore_call_timeout_s,
            )
            result = await self.sandbox.run(session.code, harness)
            if result.timed_out:
                raise GenerationError("the bug hunt timed out")
            if result.error:
                raise GenerationError(f"the bug hunt crashed: {result.error.type}")
            session.counterexamples = result.value["counterexamples"]
        except Exception as exc:
            logger.exception("exploration failed for session %s", session.id)
            self.store.publish(session.id, "error", f"Exploration failed: {exc}")
            self._move(session, S.PROPERTIES_CONFIRMED)
            return

        found = len(session.counterexamples)
        self._progress(
            session,
            f"Found {found} distinct failure(s)" if found else "No failures found",
            failures=found,
        )
        if found:
            await self._diagnose(session)
        self._move(session, S.PREDICTING if found else S.POSTMORTEM)

    async def _diagnose(self, session: Session) -> None:
        """Work out the private diagnosis. Failure here doesn't stop the session."""
        self._progress(session, "Working out the root cause")
        try:
            session.trace = await self.diagnoser.trace(session)
            session.diagnosis = await self.diagnoser.diagnose(session)
        except Exception:
            logger.exception("diagnosis failed for session %s", session.id)
            # No details: the error text could describe the bug.
            self.store.publish(session.id, "error", "Could not work out a diagnosis")
            return
        if session.diagnosis.verified:
            self._progress(session, "Root cause found and fix verified")
        else:
            self._progress(session, "Root cause found, but the fix could not be verified")

    # Predict, then reveal
    async def prediction_question(self, session_id: str) -> dict[str, str]:
        """'What should f(x) return?' for the current failing case."""
        session = self.store.get(session_id)
        _require(session, S.PREDICTING, S.HINTING)
        text = question(session)
        asked = any(
            m["kind"] == "question" and m["case"] == session.current_case for m in session.messages
        )
        if not asked:
            await self._say(session, [text], kind="question")
        return {"question": text, "call": current_case(session)["call"]}

    async def submit_prediction(self, session_id: str, answer: str) -> dict[str, Any]:
        """Run the code, compare with the user's prediction, and reveal (or nudge).

        The correct answer itself is never shown: a wrong prediction is pointed
        back at the user's own spec instead.
        """
        session = self.store.get(session_id)
        _require(session, S.PREDICTING, S.HINTING)
        prediction = parse_prediction(answer)  # PredictionParseError -> 422
        self._record(session, "user", "prediction", answer)

        result = await self._evaluate_prediction(session, prediction)
        actual = result["actual"]
        if result["reference"] is not None:
            correct = result["matches_reference"]
        else:
            correct = prediction.kind == "value" and not result["violates"]

        ce = current_case(session)
        spec_line = _spec_reminder(session, result, prediction)
        safer_nudge = (
            "Check your answer against your spec again: "
            f"what should a correct {session.function}() return?"
        )
        if correct and not result["matches_actual"]:
            outcome = "revealed"
            texts = [
                f"You expected {prediction.describe()}. The code {describe_actual(actual)}. "
                "Your prediction fits the spec and the code's answer doesn't: that gap is the bug."
            ]
        elif correct:
            outcome = "agreed"
            texts = [f"The code agrees with you on `{ce['call']}`."]
        elif result["matches_actual"]:
            outcome = "matches_code"
            texts = [
                f"That's exactly what the code does: it {describe_actual(actual)}. "
                f"But does that fit your spec? {spec_line}",
                f"That's exactly what the code does. {safer_nudge}",
            ]
        else:
            outcome = "both_differ"
            texts = [
                f"The code {describe_actual(actual)}. Your answer doesn't fit your spec either. "
                f"{spec_line}",
                f"The code {describe_actual(actual)}. {safer_nudge}",
            ]

        session.hint_level = max(session.hint_level, 1)  # the symptom is now on the table
        message = await self._say(session, texts, kind="reveal" if correct else "nudge")
        if outcome == "revealed" and session.state is S.PREDICTING:
            self._move(session, S.HINTING)
        return {
            "outcome": outcome,
            "message": message,
            "actual": describe_actual(actual),
            "state": session.state.value,
            "hint_level": session.hint_level,
        }

    async def _evaluate_prediction(
        self, session: Session, prediction: Prediction
    ) -> dict[str, Any]:
        d = session.diagnosis
        config = {
            "function": session.function,
            "args": counterexample_args(current_case(session)),
            "predicted": prediction.as_config(),
            "predicates": {p.statement: p.predicate for p in session.properties},
            "reference_fix": d.reference_fix if d is not None and d.verified else None,
            "call_timeout_s": self.settings.explore_call_timeout_s,
        }
        harness = (
            "from sparring_partner.testing.harness import evaluate_prediction\n\n"
            f"CONFIG = {config!r}\n\n\n"
            "def run(solution, inputs):\n"
            "    return evaluate_prediction(solution, CONFIG)\n"
        )
        result = await self.sandbox.run(session.code, harness)
        if not result.ok:
            raise UserCodeError("couldn't run your code on that input")
        return result.value

    # Hints
    async def next_hint(self, session_id: str) -> dict[str, Any]:
        """Climb one rung of the hint ladder and return the gate-approved hint."""
        session = self.store.get(session_id)
        _require(session, S.PREDICTING, S.HINTING)
        if session.diagnosis is None:
            raise NotAvailable("hints aren't available: the bug couldn't be diagnosed")
        if session.hint_level >= MAX_HINT_LEVEL:
            raise NotAvailable("that was the last hint: the next step is your fix")
        if session.state is S.PREDICTING:
            self._move(session, S.HINTING)
        level = session.climb_hint()
        text = await self.hints.write(session, level)
        self._record(session, "coach", "hint", text, level=level)
        return {"level": level, "text": text}

    async def _say(self, session: Session, texts: list[str], *, kind: str) -> str:
        """Send the first of `texts` the gate allows (fixed templates: no model layer)."""
        for text in texts:
            decision = await self.gate.check(
                session, text, session.hint_level, kind=kind, use_llm=False
            )
            if decision.allowed:
                break
        self._record(session, "coach", kind, text)
        return text

    def _record(
        self, session: Session, role: str, kind: str, text: str, *, level: int | None = None
    ) -> None:
        level = session.hint_level if level is None else level
        session.messages.append(
            {
                "role": role,
                "kind": kind,
                "level": level,
                "text": text,
                "case": session.current_case,
                "at": time.time(),
            }
        )
        self.store.publish(session.id, "message", text, role=role, kind=kind, level=level)

    async def wait_idle(self) -> None:
        """Wait for background work (exploration) to finish."""
        while self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def _move(self, session: Session, state: SessionState) -> None:
        session.move_to(state)
        self.store.publish(session.id, "state", f"Now {state.value}", state=state.value)

    def _progress(self, session: Session, message: str, **data: Any) -> None:
        self.store.publish(session.id, "progress", message, **data)

    def _start(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _pick(code: str, intent: str, function: str | None) -> FunctionInfo:
    try:
        return pick_function(code, intent, function)
    except SyntaxError as exc:
        raise UserCodeError(f"SyntaxError on line {exc.lineno}: {exc.msg}") from exc
    except ValueError as exc:
        raise UserCodeError(str(exc)) from exc


def _require(session: Session, *states: SessionState) -> None:
    if session.state not in states:
        raise InvalidTransition(f"not available in state {session.state}")


def _spec_reminder(session: Session, result: dict[str, Any], prediction: Prediction) -> str:
    if result["violates"]:
        return f'Check it against: "{result["violates"][0].rstrip(".")}".'
    if prediction.kind == "error":
        return "Would a correct implementation really fail on this input?"
    return (
        f'Re-read the intent: "{session.intent}". '
        f"What should a correct {session.function}() return?"
    )
