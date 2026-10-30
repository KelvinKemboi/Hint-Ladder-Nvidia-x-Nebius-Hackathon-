"""Session state (docs/PROJECT_OUTLINE.md §7).

Sessions are internal objects. API responses are built field by field in
sparring_partner.api, so anything added here stays server-side unless it is
deliberately exposed there. `diagnosis`, `counterexamples` and `strategy_code`
must never reach the client.
"""

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4

MAX_HINT_LEVEL = 5


class Mode(StrEnum):
    DEBUG = "debug"
    REVIEW = "review"
    EXPLORE = "explore"


class SessionState(StrEnum):
    INTENT = "intent"  # code + intent received, properties proposed
    PROPERTIES_CONFIRMED = "properties_confirmed"
    EXPLORING = "exploring"  # secret bug hunt in the sandbox
    PREDICTING = "predicting"  # predict-then-reveal
    HINTING = "hinting"
    VERIFYING = "verifying"  # checking the user's fix
    POSTMORTEM = "postmortem"


S = SessionState
TRANSITIONS: dict[SessionState, set[SessionState]] = {
    S.INTENT: {S.PROPERTIES_CONFIRMED},
    S.PROPERTIES_CONFIRMED: {S.EXPLORING},
    # Bugs found -> predicting; none found -> postmortem; exploration failed -> retry.
    S.EXPLORING: {S.PREDICTING, S.POSTMORTEM, S.PROPERTIES_CONFIRMED},
    S.PREDICTING: {S.HINTING, S.VERIFYING},
    S.HINTING: {S.VERIFYING},
    # Fix rejected -> back to hints, or a new counterexample to predict; accepted -> postmortem.
    S.VERIFYING: {S.HINTING, S.PREDICTING, S.POSTMORTEM},
    # Follow-up question ("what if the input isn't sorted?") keeps the session going.
    S.POSTMORTEM: {S.PREDICTING},
}


class InvalidTransition(Exception):
    pass


@dataclass
class ParamSpec:
    name: str
    kind: str  # an edge-case catalog kind (see agents.testgen) or "other"
    description: str = ""


@dataclass
class PropertySpec:
    statement: str  # natural language, shown to and edited by the user
    predicate: str  # Python source defining check(args, output) -> bool
    id: str = field(default_factory=lambda: uuid4().hex[:8])
    error: str | None = None  # why the predicate failed validation, if it did


@dataclass
class Diagnosis:
    """The answer the agent knows but never tells. Server-only."""

    root_cause: str
    line: int | None
    region: str
    bug_class: str
    concept: str
    reference_fix: str
    verified: bool = False  # the fix passed every test in the sandbox
    attempts: int = 1


@dataclass
class Session:
    code: str
    intent: str
    function: str
    mode: Mode = Mode.DEBUG
    id: str = field(default_factory=lambda: uuid4().hex)
    language: str = "python"
    state: SessionState = SessionState.INTENT
    created_at: float = field(default_factory=time.time)

    params: list[ParamSpec] = field(default_factory=list)
    # For a class: arguments per call ("__init__" and each public method). Empty for a
    # function. A class is tested through operation sequences (testing.capture).
    methods: dict[str, int] = field(default_factory=dict)
    example_args: tuple[Any, ...] | None = None  # a trivial input, for validating predicates
    properties: list[PropertySpec] = field(default_factory=list)

    # --- server-only ---
    worked_examples: list[tuple[Any, Any]] = field(default_factory=list)  # (args, correct output)
    strategy_code: str | None = None
    counterexamples: list[dict[str, Any]] = field(default_factory=list)  # revealed one at a time
    trace: dict[str, Any] | None = None  # locals per line for the top counterexample
    evidence: dict[str, Any] | None = None  # the trace windowed for level 3
    current_case: int = 0  # which counterexample is being worked on
    findings: list[dict[str, Any]] = field(default_factory=list)  # review mode, with details
    diagnosis: Diagnosis | None = None

    original_code: str = ""  # as first submitted; `code` follows the learner's fixes
    solved_cases: list[dict[str, Any]] = field(default_factory=list)  # for the post-mortem
    messages: list[dict[str, Any]] = field(default_factory=list)  # gate-approved only
    gate_log: list[dict[str, Any]] = field(default_factory=list)
    fix_attempts: list[dict[str, Any]] = field(default_factory=list)
    postmortem: dict[str, Any] | None = None
    _hint_level: int = field(default=0, repr=False)

    @property
    def hint_level(self) -> int:
        return self._hint_level

    @hint_level.setter
    def hint_level(self, level: int) -> None:
        if level < self._hint_level:
            raise ValueError(f"hint level can only increase ({self._hint_level} -> {level})")
        if level > MAX_HINT_LEVEL:
            raise ValueError(f"hint level is at most {MAX_HINT_LEVEL}")
        self._hint_level = level

    @property
    def stateful(self) -> bool:
        return bool(self.methods)

    def reset_ladder(self) -> None:
        """A new failing case starts a new hint ladder: levels only rise within a case."""
        self._hint_level = 0

    def __post_init__(self) -> None:
        self.original_code = self.original_code or self.code

    def climb_hint(self) -> int:
        self.hint_level += 1
        return self.hint_level

    def can_move_to(self, state: SessionState) -> bool:
        return state in TRANSITIONS[self.state]

    def move_to(self, state: SessionState) -> None:
        if not self.can_move_to(state):
            raise InvalidTransition(f"cannot go from {self.state} to {state}")
        self.state = state
