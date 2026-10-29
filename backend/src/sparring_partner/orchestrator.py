"""Drives a session through its states and publishes progress events."""

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from sparring_partner.agents.code_info import FunctionInfo, pick_function
from sparring_partner.agents.errors import GenerationError, UserCodeError
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
        self._move(session, S.PREDICTING if found else S.POSTMORTEM)

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
