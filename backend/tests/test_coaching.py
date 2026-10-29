"""Predict-then-reveal and the hint ladder, end to end through the API."""

import pytest
from fakes import COVERED, NO_OVERLAP
from test_api import create, merge_llm, wait_for_state

from sparring_partner.config import get_settings

ALLOW = {"allow": True, "reason": "fine"}
DENY = {"allow": False, "reason": "names the operator to use"}
TOUCHING_CALL = "merge_intervals([[0, 0], [0, 0]])"
GOOD_HINTS = {
    1: "Try that input again: it returns two separate intervals. Which part of your spec breaks?",
    2: "Look inside the for loop in merge_intervals.",
    3: "Watch the row marked ◀ and compare what merged holds before and after.",
    4: "Two intervals that share an endpoint still overlap. Which comparison decides a merge?",
    5: "Look at line 7. What should it do when start equals the last end?",
}
LEAKY = {"text": "Use <= instead of < there."}


def predicting(make_client, **llm_extra):
    llm = merge_llm(**llm_extra)
    client, orchestrator = make_client(llm)
    body = create(client)
    keep = [{"id": p["id"], "statement": p["statement"]} for p in body["properties"]]
    client.put(f"/api/sessions/{body['id']}/properties", json={"properties": keep})
    wait_for_state(client, body["id"], {"predicting"})
    return client, orchestrator, body["id"], llm


def hint(client, sid):
    return client.post(f"/api/sessions/{sid}/hint")


# Predict, then reveal
def test_question_names_the_simplest_failing_call(make_client):
    client, _, sid, _ = predicting(make_client)

    response = client.get(f"/api/sessions/{sid}/prediction")

    assert response.json() == {
        "question": f"What should `{TOUCHING_CALL}` return?",
        "call": TOUCHING_CALL,
    }
    client.get(f"/api/sessions/{sid}/prediction")  # asking twice records it once
    messages = client.get(f"/api/sessions/{sid}").json()["messages"]
    assert [m["kind"] for m in messages] == ["question"]


@pytest.mark.parametrize("answer", ["[[0, 0]]", "it should return [(0, 0)].", "`[[0,0]]`"])
def test_correct_prediction_reveals_the_bug(make_client, answer):
    client, _, sid, _ = predicting(make_client)

    result = client.post(f"/api/sessions/{sid}/prediction", json={"answer": answer}).json()

    assert result["outcome"] == "revealed"
    assert result["actual"] == "returns `[[0, 0], [0, 0]]`"
    assert "that gap is the bug" in result["message"]
    assert result["state"] == "hinting" and result["hint_level"] == 1


def test_predicting_what_the_buggy_code_does_gets_a_nudge_to_the_spec(make_client):
    client, _, sid, _ = predicting(make_client)

    result = client.post(
        f"/api/sessions/{sid}/prediction", json={"answer": "[[0, 0], [0, 0]]"}
    ).json()

    assert result["outcome"] == "matches_code"
    assert f'Check it against: "{NO_OVERLAP[0]}"' in result["message"]
    assert result["state"] == "predicting"  # they can try again
    assert "[[0, 0]]`" not in result["message"].replace(result["actual"], "")  # no answer given


def test_wrong_in_a_different_way_gets_the_output_and_a_nudge(make_client):
    client, _, sid, _ = predicting(make_client)

    result = client.post(f"/api/sessions/{sid}/prediction", json={"answer": "[]"}).json()

    assert result["outcome"] == "both_differ"
    assert result["message"].startswith("The code returns `[[0, 0], [0, 0]]`.")
    assert COVERED[0] in result["message"]
    assert result["state"] == "predicting"


def test_unreadable_prediction_is_a_422(make_client):
    client, _, sid, _ = predicting(make_client)
    response = client.post(f"/api/sessions/{sid}/prediction", json={"answer": "banana"})
    assert response.status_code == 422


def test_prediction_before_exploring_is_a_409(make_client):
    client, _ = make_client(merge_llm())
    body = create(client)
    assert client.get(f"/api/sessions/{body['id']}/prediction").status_code == 409


# The hint ladder
def test_climbing_all_five_levels(make_client):
    drafts = [{"text": GOOD_HINTS[n]} for n in range(1, 6)]
    client, orchestrator, sid, llm = predicting(
        make_client, HintDraft=drafts, GateVerdict=[ALLOW] * 5
    )

    hints = [hint(client, sid).json() for _ in range(5)]

    assert [h["level"] for h in hints] == [1, 2, 3, 4, 5]
    assert hints[0]["text"] == GOOD_HINTS[1]
    level3 = hints[2]["text"]
    assert level3.startswith(GOOD_HINTS[3])
    assert "| step | values |" in level3 and "◀ goes wrong here" in level3
    assert "line" not in level3.lower()  # no line numbers below level 5
    assert hints[4]["text"] == GOOD_HINTS[5]

    last = hint(client, sid)
    assert last.status_code == 409 and "last hint" in last.json()["detail"]
    session = client.get(f"/api/sessions/{sid}").json()
    assert session["state"] == "hinting" and session["hint_level"] == 5
    assert [m["level"] for m in session["messages"] if m["kind"] == "hint"] == [1, 2, 3, 4, 5]
    # The deep model wrote the hints, the gate model judged them.
    roles = {name: role for role, name, _ in llm.calls if name in ("HintDraft", "GateVerdict")}
    assert roles == {"HintDraft": "deep", "GateVerdict": "gate"}


def test_level_prompts_carry_the_do_not_reveal_rules(make_client):
    client, _, sid, llm = predicting(
        make_client, HintDraft=[{"text": GOOD_HINTS[1]}], GateVerdict=[ALLOW]
    )
    hint(client, sid)
    prompt = llm.asked("HintDraft")[0][-1]["content"]
    assert "Level 1, the symptom" in prompt
    assert "Never write code" in prompt and "Never go beyond this level" in prompt
    assert "level 2" not in prompt  # only what's allowed so far is listed


def test_blocked_drafts_are_retried_with_the_reason_then_fall_back(make_client):
    client, orchestrator, sid, llm = predicting(make_client, HintDraft=[LEAKY] * 3)

    text = hint(client, sid).json()["text"]

    assert text == (
        f"Try `{TOUCHING_CALL}`. It returns `[[0, 0], [0, 0]]`, "
        f'but your spec says: "{NO_OVERLAP[0]}".'
    )
    retries = [m[-1]["content"] for m in llm.asked("HintDraft")[1:]]
    assert len(retries) == 2 and all("contains fix code: '<='" in r for r in retries)
    log = orchestrator.store.get(sid).gate_log
    hint_log = [(e["kind"], e["allowed"]) for e in log if e["kind"].startswith("hint")]
    assert hint_log == [("hint", False)] * 3 + [("hint_fallback", True)]


def test_llm_gate_blocks_lead_to_the_fallback_too(make_client):
    drafts = [{"text": GOOD_HINTS[1]}] * 3  # passes the rules; the gate model objects
    client, orchestrator, sid, llm = predicting(
        make_client, HintDraft=drafts, GateVerdict=[DENY] * 3
    )

    text = hint(client, sid).json()["text"]

    assert text.startswith(f"Try `{TOUCHING_CALL}`.")  # the level 1 template
    log = [e for e in orchestrator.store.get(sid).gate_log if e["kind"] == "hint"]
    assert [(e["layer"], e["reason"]) for e in log] == [("llm", DENY["reason"])] * 3
    retries = [m[-1]["content"] for m in llm.asked("HintDraft")[1:]]
    assert all(DENY["reason"] in r for r in retries)


def test_hints_need_a_diagnosis(make_client):
    client, orchestrator, sid, _ = predicting(make_client)
    orchestrator.store.get(sid).diagnosis = None
    response = hint(client, sid)
    assert response.status_code == 409 and "diagnosed" in response.json()["detail"]


def test_nothing_leaks_beyond_the_level_reached(make_client):
    drafts = [{"text": GOOD_HINTS[n]} for n in range(1, 4)]
    client, orchestrator, sid, _ = predicting(
        make_client, HintDraft=drafts, GateVerdict=[ALLOW] * 3
    )
    client.get(f"/api/sessions/{sid}/prediction")
    client.post(f"/api/sessions/{sid}/prediction", json={"answer": "[[0, 0]]"})
    for _ in range(3):
        hint(client, sid)

    texts = [m["text"] for m in client.get(f"/api/sessions/{sid}").json()["messages"]]
    texts += [e.message for e in orchestrator.store.history(sid)]
    for text in texts:
        assert "SECRET" not in text  # root cause and concept stay hidden below level 4
        assert "<=" not in text and "sorted(" not in text  # never the fix
        assert "line 7" not in text.lower()


def test_gate_log_is_hidden_unless_enabled(make_client, monkeypatch):
    client, _, sid, _ = predicting(make_client)
    client.get(f"/api/sessions/{sid}/prediction")
    assert client.get(f"/api/sessions/{sid}/gate-log").status_code == 404

    monkeypatch.setattr(get_settings(), "expose_gate_log", True)
    log = client.get(f"/api/sessions/{sid}/gate-log").json()
    assert log[0]["kind"] == "question" and log[0]["allowed"] is True
