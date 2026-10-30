import time

import pytest
from fakes import (
    COVERED,
    DIAGNOSIS,
    INPUT_SPEC,
    INTERVALS_STRATEGY,
    MERGE_CODE,
    NO_OVERLAP,
    SORTED,
    FakeLLM,
    prop,
)

from sparring_partner.api.sessions import SessionOut
from sparring_partner.llm import ModelConfigError
from sparring_partner.main import app

# Fields that must never reach the client.
SERVER_ONLY = {
    "diagnosis",
    "counterexamples",
    "strategy_code",
    "trace",
    "evidence",
    "current_case",
    "worked_examples",
    "gate_log",
    "reference_fix",
}
FEWER = (
    "output has no more intervals than the input",
    "def check(args, output):\n    return len(output) <= len(argz[0])\n",  # NameError: argz
)
FEWER_FIXED = (FEWER[0], "def check(args, output):\n    return len(output) <= len(args[0])\n")
SORTED_BY_START = (
    "output is sorted by start",
    "def check(args, output):\n    return [a[0] for a in output] == sorted(a[0] for a in output)\n",
)


def merge_llm(**extra) -> FakeLLM:
    replies = {
        "InputSpecDraft": [INPUT_SPEC],
        "Proposal": [{"properties": [prop(NO_OVERLAP), prop(COVERED), prop(SORTED), prop(FEWER)]}],
        "Predicates": [{"properties": [prop(FEWER_FIXED)]}],  # the repair
        "StrategyDraft": [{"code": "def strategy(:\n"}, {"code": INTERVALS_STRATEGY}],
        "DiagnosisDraft": [DIAGNOSIS],
    }
    for name, queue in extra.items():
        replies[name] = replies.get(name, []) + queue
    return FakeLLM(**replies)


def create(client) -> dict:
    response = client.post(
        "/api/sessions", json={"code": MERGE_CODE, "intent": "merge overlapping intervals"}
    )
    assert response.status_code == 201, response.text
    return response.json()


def wait_for_state(client, session_id, states, timeout=60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/sessions/{session_id}").json()
        if body["state"] in states:
            return body
        time.sleep(0.1)
    raise AssertionError(f"session never reached {states}; last state {body['state']}")


# Property proposer
def test_create_proposes_validated_properties(make_client):
    llm = merge_llm()
    client, _ = make_client(llm)

    body = create(client)

    assert body["state"] == "intent"
    assert body["function"] == "merge_intervals"
    assert body["params"] == [
        {"name": "intervals", "kind": "intervals", "description": "[start, end] pairs"}
    ]
    statements = [p["statement"] for p in body["properties"]]
    assert statements == [NO_OVERLAP[0], COVERED[0], SORTED[0], FEWER[0]]
    # The broken predicate was sent back with its error and repaired.
    assert body["properties"][3]["predicate"] == FEWER_FIXED[1].strip()
    repair_prompt = llm.asked("Predicates")[0][-1]["content"]
    assert "NameError" in repair_prompt and "argz" in repair_prompt
    proposal_prompt = llm.asked("Proposal")[0][-1]["content"]
    assert "merge overlapping intervals" in proposal_prompt
    assert all(role == "fast" for role, _, _ in llm.calls)


def test_properties_still_broken_after_repair_are_dropped(make_client):
    llm = merge_llm()
    llm.replies["Predicates"] = [{"properties": [prop(FEWER)]}]  # "repair" is still broken
    client, _ = make_client(llm)

    statements = [p["statement"] for p in create(client)["properties"]]

    assert FEWER[0] not in statements and len(statements) == 3


def test_failed_repair_keeps_the_valid_properties(make_client):
    llm = merge_llm()
    llm.replies["Predicates"] = []  # the repair call fails outright
    client, _ = make_client(llm)

    statements = [p["statement"] for p in create(client)["properties"]]

    assert statements == [NO_OVERLAP[0], COVERED[0], SORTED[0]]


def test_code_that_does_not_parse_is_rejected(make_client):
    client, _ = make_client(FakeLLM())
    response = client.post("/api/sessions", json={"code": "def f(:\n", "intent": "anything"})
    assert response.status_code == 422
    assert "SyntaxError on line 1" in response.json()["detail"]


# Confirming properties and the bug hunt
def test_confirm_edits_regenerates_and_explores(make_client):
    llm = merge_llm(Predicates=[{"properties": [prop(SORTED_BY_START)]}])
    client, orchestrator = make_client(llm)
    body = create(client)
    no_overlap, covered, _sorted, _fewer = body["properties"]

    response = client.put(
        f"/api/sessions/{body['id']}/properties",
        json={
            "properties": [
                {"id": no_overlap["id"], "statement": no_overlap["statement"]},  # unchanged
                {"id": covered["id"], "statement": covered["statement"]},  # unchanged
                {"id": _sorted["id"], "statement": SORTED_BY_START[0]},  # edited
                # FEWER left out: removed
            ]
        },
    )

    assert response.status_code == 200, response.text
    confirmed = response.json()
    assert [p["statement"] for p in confirmed["properties"]] == [
        NO_OVERLAP[0],
        COVERED[0],
        SORTED_BY_START[0],
    ]
    assert confirmed["properties"][0]["id"] == no_overlap["id"]
    assert confirmed["properties"][2]["predicate"] == SORTED_BY_START[1].strip()
    # Only the edited statement went back to the model.
    regenerate_prompt = llm.asked("Predicates")[-1][-1]["content"]
    assert SORTED_BY_START[0] in regenerate_prompt and NO_OVERLAP[0] not in regenerate_prompt

    final = wait_for_state(client, body["id"], {"predicting", "postmortem", "properties_confirmed"})
    assert final["state"] == "predicting"
    assert final["failures_found"] == 2  # touching ranges, and unsorted input

    session = orchestrator.store.get(body["id"])
    violated = {ce["violated_property"] for ce in session.counterexamples}
    assert violated == {NO_OVERLAP[0], COVERED[0]}
    events = [e.message for e in orchestrator.store.history(body["id"])]
    assert "Writing an input generator" in events
    assert any(m.startswith("Testing 3 properties on 60 generated inputs") for m in events)
    assert "Found 2 distinct failure(s)" in events
    assert "Root cause found and fix verified" in events
    assert session.diagnosis.verified and session.diagnosis.line == 7
    # The deep model did the diagnosis; everything else used the fast model.
    assert {role for role, name, _ in llm.calls if name == "DiagnosisDraft"} == {"deep"}


def test_failed_diagnosis_does_not_block_the_session(make_client):
    llm = merge_llm()
    llm.replies["DiagnosisDraft"] = []  # the model call fails
    client, orchestrator = make_client(llm)
    body = create(client)
    keep = [{"id": p["id"], "statement": p["statement"]} for p in body["properties"]]
    client.put(f"/api/sessions/{body['id']}/properties", json={"properties": keep})

    final = wait_for_state(client, body["id"], {"predicting", "properties_confirmed"})

    assert final["state"] == "predicting"
    assert orchestrator.store.get(body["id"]).diagnosis is None
    errors = [e.message for e in orchestrator.store.history(body["id"]) if e.type == "error"]
    assert errors == ["Could not work out a diagnosis"]


def test_edit_whose_predicate_cannot_be_fixed_is_a_422(make_client):
    broken = {"properties": [{"statement": "x", "predicate": "def check(args):\n    return 1\n"}]}
    llm = merge_llm(Predicates=[broken, broken])
    client, _ = make_client(llm)
    body = create(client)

    response = client.put(
        f"/api/sessions/{body['id']}/properties",
        json={"properties": [{"statement": "output is magic"}]},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "exactly two parameters" in detail["errors"]["output is magic"]
    assert client.get(f"/api/sessions/{body['id']}").json()["state"] == "intent"


def test_properties_cannot_change_once_the_hunt_started(make_client):
    client, _ = make_client(merge_llm())
    body = create(client)
    keep = [{"id": p["id"], "statement": p["statement"]} for p in body["properties"]]
    client.put(f"/api/sessions/{body['id']}/properties", json={"properties": keep})
    wait_for_state(client, body["id"], {"predicting"})

    response = client.put(f"/api/sessions/{body['id']}/properties", json={"properties": keep})

    assert response.status_code == 409


def test_failed_exploration_goes_back_to_properties_confirmed(make_client):
    llm = merge_llm()
    llm.replies["StrategyDraft"] = [{"code": "nope("}] * 3
    client, orchestrator = make_client(llm)
    body = create(client)
    session = orchestrator.store.get(body["id"])
    session.params[0].kind = "other"  # no fallback strategy either

    keep = [{"id": p["id"], "statement": p["statement"]} for p in body["properties"]]
    client.put(f"/api/sessions/{body['id']}/properties", json={"properties": keep})

    final = wait_for_state(client, body["id"], {"properties_confirmed", "predicting"})
    assert final["state"] == "properties_confirmed"
    errors = [e for e in orchestrator.store.history(body["id"]) if e.type == "error"]
    assert "input strategy" in errors[0].message


def test_unknown_session_is_404(make_client):
    client, _ = make_client(FakeLLM())
    assert client.get("/api/sessions/nope").status_code == 404
    response = client.put(
        "/api/sessions/nope/properties", json={"properties": [{"statement": "abc"}]}
    )
    assert response.status_code == 404


# Server-only data never leaks
def test_response_schemas_have_no_server_only_fields():
    assert not SERVER_ONLY & set(SessionOut.model_fields)

    def field_names(schema):
        if isinstance(schema, dict):
            for key, value in schema.items():
                if key == "properties" and isinstance(value, dict):
                    yield from value
                yield from field_names(value)
        elif isinstance(schema, list):
            for item in schema:
                yield from field_names(item)

    openapi = app.openapi()
    assert not SERVER_ONLY & set(field_names(openapi)), "server-only field in the API schema"


def test_diagnosis_and_counterexamples_never_reach_the_client(make_client):
    client, orchestrator = make_client(merge_llm())
    body = create(client)
    keep = [{"id": p["id"], "statement": p["statement"]} for p in body["properties"]]
    with client.websocket_connect(f"/api/sessions/{body['id']}/stream") as ws:
        client.put(f"/api/sessions/{body['id']}/properties", json={"properties": keep})
        events = []
        while not events or events[-1].get("data", {}).get("state") not in (
            "predicting",
            "postmortem",
        ):
            events.append(ws.receive_json())
    session = orchestrator.store.get(body["id"])
    assert session.diagnosis is not None and session.diagnosis.verified
    assert session.trace is not None

    texts = [client.get(f"/api/sessions/{body['id']}").text, str(events)]
    texts += [e.message for e in orchestrator.store.history(body["id"])]
    leaked = [ce["call"] for ce in session.counterexamples] + [
        ce["output"] for ce in session.counterexamples
    ]
    leaked += ["intervals = sorted(intervals)", "reference_fix", "root_cause", "steps"]
    for text in texts:
        assert "SECRET" not in text
        for secret in leaked:
            assert secret not in text


# WebSocket stream
def test_stream_sends_backlog_then_live_events(make_client):
    client, _ = make_client(merge_llm())
    body = create(client)

    with client.websocket_connect(f"/api/sessions/{body['id']}/stream") as ws:
        backlog = [ws.receive_json(), ws.receive_json()]
        assert [e["seq"] for e in backlog] == [1, 2]
        assert backlog[0]["message"] == "Reading merge_intervals() and proposing properties"
        assert backlog[1]["data"] == {"count": 4}

        keep = [{"id": p["id"], "statement": p["statement"]} for p in body["properties"]]
        client.put(f"/api/sessions/{body['id']}/properties", json={"properties": keep})
        states = []
        while "predicting" not in states:
            event = ws.receive_json()
            if event["type"] == "state":
                states.append(event["data"]["state"])
    assert states == ["properties_confirmed", "exploring", "predicting"]


def test_stream_for_unknown_session_closes(make_client):
    from starlette.websockets import WebSocketDisconnect

    client, _ = make_client(FakeLLM())
    with (
        pytest.raises(WebSocketDisconnect) as closed,
        client.websocket_connect("/api/sessions/nope/stream") as ws,
    ):
        ws.receive_json()
    assert closed.value.code == 4404


def test_a_wrong_model_id_is_a_503_that_says_what_to_fix(make_client):
    def unknown_model(messages):
        raise ModelConfigError("Token Factory has no model 'your_model_id_here' (MODEL_FAST).")

    client, _ = make_client(FakeLLM(Proposal=[unknown_model], InputSpec=[unknown_model]))
    response = client.post("/api/sessions", json={"code": MERGE_CODE, "intent": "merge intervals"})

    assert response.status_code == 503
    assert "MODEL_FAST" in response.json()["detail"]
