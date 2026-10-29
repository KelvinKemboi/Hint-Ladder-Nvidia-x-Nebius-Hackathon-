"""In-memory session store with a per-session event stream for the WebSocket."""

import asyncio
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from typing import Any

from sparring_partner.session.models import Session


class SessionNotFound(KeyError):
    pass


@dataclass
class Event:
    """A progress event. Carries counts and states only, never test details."""

    seq: int
    type: str # "state", "progress", "error"
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._events: dict[str, list[Event]] = defaultdict(list)
        self._subscribers: dict[str, set[asyncio.Queue[Event]]] = defaultdict(set)

    def add(self, session: Session) -> Session:
        self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> Session:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise SessionNotFound(session_id) from None

    def publish(self, session_id: str, type: str, message: str, **data: Any) -> Event:
        events = self._events[session_id]
        event = Event(seq=len(events) + 1, type=type, message=message, data=data)
        events.append(event)
        for queue in self._subscribers[session_id]:
            queue.put_nowait(event)
        return event

    def history(self, session_id: str) -> list[Event]:
        return list(self._events[session_id])

    @asynccontextmanager
    async def subscribe(self, session_id: str) -> AsyncIterator[asyncio.Queue[Event]]:
        """A queue that starts with the backlog, then receives every new event."""
        self.get(session_id)
        queue: asyncio.Queue[Event] = asyncio.Queue()
        for event in self._events[session_id]:
            queue.put_nowait(event)
        self._subscribers[session_id].add(queue)
        try:
            yield queue
        finally:
            self._subscribers[session_id].discard(queue)
