"""Session endpoints (docs/PROJECT_OUTLINE.md §8).

Responses are built field by field from the Session, so server-only data
(diagnosis, counterexamples, generated strategies) can never leak by default.
"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, WebSocket
from pydantic import BaseModel, Field

from sparring_partner.agents.errors import GenerationError, NotAvailable, UserCodeError
from sparring_partner.agents.predict import PredictionParseError
from sparring_partner.agents.properties import PropertyValidationError
from sparring_partner.config import get_settings
from sparring_partner.llm import StructuredOutputError, get_llm
from sparring_partner.orchestrator import Orchestrator
from sparring_partner.sandbox import SandboxConfigError, get_sandbox
from sparring_partner.session import (
    InvalidTransition,
    Mode,
    Session,
    SessionNotFound,
    SessionState,
    SessionStore,
)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@lru_cache
def get_orchestrator() -> Orchestrator:
    return Orchestrator(SessionStore(), get_llm(), get_sandbox())


OrchestratorDep = Annotated[Orchestrator, Depends(get_orchestrator)]


# Schemas
class CreateSession(BaseModel):
    code: str = Field(min_length=1, max_length=50_000)
    intent: str = Field(min_length=3, max_length=500, examples=["merge overlapping intervals"])
    mode: Mode = Mode.DEBUG
    function: str | None = Field(default=None, description="function to test; inferred if omitted")


class PropertyIn(BaseModel):
    id: str | None = Field(default=None, description="omit for a new property")
    statement: str = Field(min_length=3, max_length=300)


class PropertiesIn(BaseModel):
    properties: list[PropertyIn] = Field(min_length=1, max_length=10)


class PropertyOut(BaseModel):
    id: str
    statement: str
    predicate: str


class ParamOut(BaseModel):
    name: str
    kind: str
    description: str


class MessageOut(BaseModel):
    role: str  # "coach" or "user"
    kind: str  # question, prediction, reveal, nudge, hint
    level: int  # the hint level when it was sent
    text: str


class PredictionQuestionOut(BaseModel):
    question: str
    call: str


class PredictionIn(BaseModel):
    answer: str = Field(min_length=1, max_length=2000, examples=["[[1, 5]]"])


class PredictionOut(BaseModel):
    outcome: str  # revealed, agreed, matches_code, both_differ
    message: str
    actual: str  # what the code did, e.g. "returns `[[1, 4], [4, 5]]`"
    state: SessionState
    hint_level: int


class FixIn(BaseModel):
    code: str = Field(min_length=1, max_length=50_000)


class FixOut(BaseModel):
    outcome: str  # passed, failed, special_cased, invalid
    message: str
    inputs_checked: int
    state: SessionState
    hint_level: int
    next_question: str | None = None  # after a pass: the next risk to think about


class CaseOut(BaseModel):
    call: str
    bug_class: str | None
    hint_level: int


class PostmortemOut(BaseModel):
    bug_class: str | None
    bug_classes: list[str | None]
    how_to_spot: str
    other_edge_cases: list[str]
    hint_level_reached: int
    fix_attempts: int
    inputs_checked: int
    cases: list[CaseOut]


class HintOut(BaseModel):
    level: int
    text: str


class SessionOut(BaseModel):
    id: str
    mode: Mode
    language: str
    state: SessionState
    function: str
    intent: str
    code: str
    params: list[ParamOut]
    properties: list[PropertyOut]
    hint_level: int
    failures_found: int  # a count only; the failures are revealed one at a time
    messages: list[MessageOut]  # gate-approved only
    created_at: float

    @classmethod
    def from_session(cls, s: Session) -> "SessionOut":
        return cls(
            id=s.id,
            mode=s.mode,
            language=s.language,
            state=s.state,
            function=s.function,
            intent=s.intent,
            code=s.code,
            params=[
                ParamOut(name=p.name, kind=p.kind, description=p.description) for p in s.params
            ],
            properties=[
                PropertyOut(id=p.id, statement=p.statement, predicate=p.predicate)
                for p in s.properties
            ],
            hint_level=s.hint_level,
            failures_found=len(s.counterexamples),
            messages=[
                MessageOut(role=m["role"], kind=m["kind"], level=m["level"], text=m["text"])
                for m in s.messages
            ],
            created_at=s.created_at,
        )


# Endpoints
@contextmanager
def _errors() -> Iterator[None]:
    try:
        yield
    except SessionNotFound as exc:
        raise HTTPException(404, "session not found") from exc
    except InvalidTransition as exc:
        raise HTTPException(409, str(exc)) from exc
    except PropertyValidationError as exc:
        raise HTTPException(
            422, {"message": "some properties could not be checked", "errors": exc.errors}
        ) from exc
    except (UserCodeError, PredictionParseError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except NotAvailable as exc:
        raise HTTPException(409, str(exc)) from exc
    except (GenerationError, StructuredOutputError) as exc:
        raise HTTPException(502, f"the model could not complete this step: {exc}") from exc
    except SandboxConfigError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("", status_code=201)
async def create_session(body: CreateSession, orchestrator: OrchestratorDep) -> SessionOut:
    """Start a session: returns the proposed properties for the user to confirm or edit."""
    with _errors():
        session = await orchestrator.create(body.code, body.intent, body.mode, body.function)
    return SessionOut.from_session(session)


@router.get("/{session_id}")
async def get_session(session_id: str, orchestrator: OrchestratorDep) -> SessionOut:
    with _errors():
        return SessionOut.from_session(orchestrator.store.get(session_id))


@router.put("/{session_id}/properties")
async def confirm_properties(
    session_id: str, body: PropertiesIn, orchestrator: OrchestratorDep
) -> SessionOut:
    """Confirm or edit the properties; starts the bug hunt in the background."""
    with _errors():
        edits = [(p.id, p.statement) for p in body.properties]
        session = await orchestrator.confirm_properties(session_id, edits)
    return SessionOut.from_session(session)


@router.get("/{session_id}/prediction")
async def get_prediction_question(
    session_id: str, orchestrator: OrchestratorDep
) -> PredictionQuestionOut:
    """Predict-then-reveal: what should the failing call return?"""
    with _errors():
        return PredictionQuestionOut(**await orchestrator.prediction_question(session_id))


@router.post("/{session_id}/prediction")
async def submit_prediction(
    session_id: str, body: PredictionIn, orchestrator: OrchestratorDep
) -> PredictionOut:
    """Run the code and compare it with the prediction; never reveals the right answer."""
    with _errors():
        return PredictionOut(**await orchestrator.submit_prediction(session_id, body.answer))


@router.post("/{session_id}/hint")
async def next_hint(session_id: str, orchestrator: OrchestratorDep) -> HintOut:
    """Climb one level of the hint ladder (1 symptom ... 5 line)."""
    with _errors():
        return HintOut(**await orchestrator.next_hint(session_id))


@router.post("/{session_id}/fix")
async def submit_fix(session_id: str, body: FixIn, orchestrator: OrchestratorDep) -> FixOut:
    """Verify a fix: all property tests, N fresh inputs, and checks for special-casing."""
    with _errors():
        return FixOut(**await orchestrator.submit_fix(session_id, body.code))


@router.get("/{session_id}/postmortem")
async def get_postmortem(session_id: str, orchestrator: OrchestratorDep) -> PostmortemOut:
    """Bug class, how to spot it next time, other edge cases, hints and attempts used."""
    with _errors():
        return PostmortemOut(**await orchestrator.postmortem(session_id))


@router.get("/{session_id}/gate-log", include_in_schema=False)
async def gate_log(session_id: str, orchestrator: OrchestratorDep) -> list[dict]:
    """Demo only (EXPOSE_GATE_LOG=true): every gate decision, including blocked text."""
    if not get_settings().expose_gate_log:
        raise HTTPException(404, "not found")
    with _errors():
        return orchestrator.store.get(session_id).gate_log


@router.websocket("/{session_id}/stream")
async def stream(websocket: WebSocket, session_id: str, orchestrator: OrchestratorDep) -> None:
    """Progress events: the backlog first, then live, until the client disconnects."""
    await websocket.accept()
    try:
        async with orchestrator.store.subscribe(session_id) as queue:
            disconnected = asyncio.create_task(_wait_for_disconnect(websocket))
            while True:
                next_event = asyncio.create_task(queue.get())
                done, _ = await asyncio.wait(
                    {next_event, disconnected}, return_when=asyncio.FIRST_COMPLETED
                )
                if disconnected in done:
                    next_event.cancel()
                    return
                await websocket.send_json(next_event.result().to_dict())
    except SessionNotFound:
        await websocket.close(code=4404, reason="session not found")


async def _wait_for_disconnect(websocket: WebSocket) -> None:
    while (await websocket.receive())["type"] != "websocket.disconnect":
        pass
