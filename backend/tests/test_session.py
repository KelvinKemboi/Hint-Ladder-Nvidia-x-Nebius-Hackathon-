import pytest

from sparring_partner.session import (
    MAX_HINT_LEVEL,
    InvalidTransition,
    Session,
    SessionNotFound,
    SessionState,
    SessionStore,
)

S = SessionState


def new_session() -> Session:
    return Session(code="def f(x):\n    return x\n", intent="identity", function="f")


def test_happy_path_through_every_state():
    session = new_session()
    path = [
        S.PROPERTIES_CONFIRMED,
        S.EXPLORING,
        S.PREDICTING,
        S.HINTING,
        S.VERIFYING,
        S.POSTMORTEM,
        S.PREDICTING,  # follow-up question after the post-mortem
    ]
    for state in path:
        session.move_to(state)
    assert session.state is S.PREDICTING


@pytest.mark.parametrize(
    "start, target",
    [
        (S.INTENT, S.EXPLORING),
        (S.INTENT, S.HINTING),
        (S.EXPLORING, S.VERIFYING),
        (S.HINTING, S.PREDICTING),
        (S.POSTMORTEM, S.INTENT),
    ],
)
def test_invalid_transitions_are_rejected(start, target):
    session = new_session()
    session.state = start
    with pytest.raises(InvalidTransition):
        session.move_to(target)
    assert session.state is start


def test_hint_level_only_increases_up_to_five():
    session = new_session()
    assert session.hint_level == 0
    for level in range(1, MAX_HINT_LEVEL + 1):
        assert session.climb_hint() == level
    with pytest.raises(ValueError, match="at most"):
        session.climb_hint()
    with pytest.raises(ValueError, match="only increase"):
        session.hint_level = 2
    session.hint_level = MAX_HINT_LEVEL  # staying put is fine
    assert session.hint_level == MAX_HINT_LEVEL


def test_store_get_and_missing():
    store = SessionStore()
    session = store.add(new_session())
    assert store.get(session.id) is session
    with pytest.raises(SessionNotFound):
        store.get("nope")


def test_events_are_numbered_and_kept():
    store = SessionStore()
    session = store.add(new_session())
    store.publish(session.id, "progress", "one", count=1)
    store.publish(session.id, "state", "two")

    history = store.history(session.id)
    assert [(e.seq, e.message) for e in history] == [(1, "one"), (2, "two")]
    assert history[0].to_dict()["data"] == {"count": 1}


@pytest.mark.anyio
async def test_subscribers_get_backlog_then_live_events():
    store = SessionStore()
    session = store.add(new_session())
    store.publish(session.id, "progress", "before")

    async with store.subscribe(session.id) as queue:
        store.publish(session.id, "progress", "after")
        assert (await queue.get()).message == "before"
        assert (await queue.get()).message == "after"

    store.publish(session.id, "progress", "unsubscribed")
    assert queue.empty()


@pytest.mark.anyio
async def test_subscribe_to_unknown_session():
    with pytest.raises(SessionNotFound):
        async with SessionStore().subscribe("nope"):
            pass
