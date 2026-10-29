"""Fix verification, the post-mortem and AI Code Review mode, end to end through the API."""

from fakes import COVERED, DIAGNOSIS, FIXED_MERGE, HALF_FIXED_MERGE, NO_OVERLAP, SORTED, prop
from test_api import create, merge_llm, wait_for_state
from test_coaching import ALLOW, predicting
from test_review import REVIEW_CODE
from test_verify import FIXED_WITH_LITERAL

from sparring_partner.agents.properties import REVIEW_FOCUS

POSTMORTEM = {
    "bug_class": "boundary condition",
    "how_to_spot": "Ask what happens at the exact edge.",
    "other_edge_cases": ["nested intervals"],
}
SAFE_RISK = {"question": "What about a single interval?", "example_args": "([[1, 2]],)"}


def fix(client, sid, code):
    return client.post(f"/api/sessions/{sid}/fix", json={"code": code})


def test_partial_fix_continues_with_the_next_failure_then_a_full_fix_passes(make_client):
    client, orchestrator, sid, _ = predicting(
        make_client,
        DiagnosisDraft=[DIAGNOSIS],  # re-diagnosis for the next case
        PostmortemDraft=[POSTMORTEM],
        RiskDraft=[SAFE_RISK],
        GateVerdict=[ALLOW] * 5,
        HintDraft=[{"text": "Look at the inputs again."}],
    )
    client.post(f"/api/sessions/{sid}/hint")  # used one hint on the first case

    first = fix(client, sid, HALF_FIXED_MERGE).json()

    assert first["outcome"] == "failed"
    assert first["state"] == "predicting" and first["hint_level"] == 0  # a fresh ladder
    assert first["message"].startswith("That one's fixed, but your spec still breaks")
    session = orchestrator.store.get(sid)
    new_call = session.counterexamples[0]["call"]
    assert f"What should `{new_call}` return?" in first["message"]
    assert session.counterexamples[0]["output"] not in first["message"]  # not revealed
    assert session.code == HALF_FIXED_MERGE
    question = client.get(f"/api/sessions/{sid}/prediction").json()
    assert question["call"] == new_call

    wait_for_state(client, sid, {"predicting"})
    client.portal.call(orchestrator.wait_idle)  # background re-diagnosis
    second = fix(client, sid, FIXED_MERGE).json()

    assert second["outcome"] == "passed" and second["state"] == "postmortem"
    assert second["inputs_checked"] >= 500
    assert second["next_question"] == SAFE_RISK["question"]
    pm = client.get(f"/api/sessions/{sid}/postmortem").json()
    assert pm["bug_class"] == "boundary condition"
    assert pm["fix_attempts"] == 2 and pm["hint_level_reached"] == 1
    assert [c["hint_level"] for c in pm["cases"]] == [1, 0]
    body = client.get(f"/api/sessions/{sid}").json()
    assert body["has_postmortem"]
    assert [m["kind"] for m in body["messages"]][-2:] == ["fix_result", "risk"]
    assert body["messages"][-2]["data"] == {
        "outcome": "passed",
        "inputs_checked": second["inputs_checked"],
    }
    progress = [e for e in orchestrator.store.history(sid) if e.data.get("phase") == "verify"]
    assert progress[-1].data == {"phase": "verify", "checked": 500, "total": 500}


def test_a_failing_risk_becomes_the_next_question(make_client):
    failing_risk = {
        "question": "What if the input isn't sorted?",
        "example_args": "([[3, 4], [1, 2]],)",
    }
    llm = merge_llm(PostmortemDraft=[POSTMORTEM], RiskDraft=[failing_risk], GateVerdict=[ALLOW] * 3)
    # A spec that draws only sorted input can't see the unsorted bug, so the fix "passes".
    llm.replies["StrategyDraft"] = [{"code": SORTED_STRATEGY}]
    client, orchestrator = make_client(llm)
    body = create(client)
    session = orchestrator.store.get(body["id"])
    session.params[0].kind = "other"  # no hand-picked (unsorted) edge cases either
    keep = [{"id": p["id"], "statement": p["statement"]} for p in body["properties"]]
    client.put(f"/api/sessions/{body['id']}/properties", json={"properties": keep})
    wait_for_state(client, body["id"], {"predicting"})

    result = fix(client, body["id"], HALF_FIXED_MERGE).json()

    assert result["outcome"] == "passed"
    assert result["next_question"] == (
        "What if the input isn't sorted? Predict: what should "
        "`merge_intervals([[3, 4], [1, 2]])` return?"
    )
    assert result["state"] == "predicting"  # the session continues
    assert client.get(f"/api/sessions/{body['id']}/postmortem").status_code == 200


SORTED_STRATEGY = (
    "def strategy():\n"
    "    interval = st.tuples(st.integers(0, 20), st.integers(0, 5)).map(\n"
    "        lambda p: [p[0], p[0] + p[1]]\n"
    "    )\n"
    "    return st.tuples(st.lists(interval, max_size=6).map(sorted))\n"
)


def test_invalid_and_hardcoded_fixes_keep_the_case(make_client):
    client, orchestrator, sid, _ = predicting(make_client)

    invalid = fix(client, sid, "def merge_intervals(:\n").json()
    assert invalid["outcome"] == "invalid" and invalid["state"] == "predicting"
    assert invalid["message"] == "That fix can't be checked."  # line numbers stay gated

    hard = fix(client, sid, FIXED_WITH_LITERAL).json()
    assert hard["outcome"] == "special_cased"
    assert "exact failing input (`[[0, 0], [0, 0]]`)" in hard["message"]
    assert orchestrator.store.get(sid).code != FIXED_WITH_LITERAL  # not adopted


def test_postmortem_before_the_fix_is_a_409(make_client):
    client, _, sid, _ = predicting(make_client)
    assert client.get(f"/api/sessions/{sid}/postmortem").status_code == 409


def test_fix_is_only_accepted_while_working_on_a_failure(make_client):
    client, _ = make_client(merge_llm())
    body = create(client)
    assert fix(client, body["id"], FIXED_MERGE).status_code == 409


# AI Code Review mode
def test_review_mode_end_to_end(make_client):
    concerns = {
        "concerns": [
            {
                "kind": "wrong_assumption",
                "detail": "assumes sorted input",
                "question": "What order do the intervals arrive in?",
                "example_args": "([[4, 5], [1, 2], [2, 3]],)",
            }
        ]
    }
    llm = merge_llm(ReviewDraft=[concerns], GateVerdict=[ALLOW] * 3)
    llm.replies["Proposal"] = [{"properties": [prop(NO_OVERLAP), prop(COVERED), prop(SORTED)]}]
    client, orchestrator = make_client(llm)
    response = client.post(
        "/api/sessions",
        json={"code": REVIEW_CODE, "intent": "merge overlapping intervals", "mode": "review"},
    )
    body = response.json()
    assert body["mode"] == "review"
    assert REVIEW_FOCUS.strip() in llm.asked("Proposal")[0][-1]["content"]

    keep = [{"id": p["id"], "statement": p["statement"]} for p in body["properties"]]
    client.put(f"/api/sessions/{body['id']}/properties", json={"properties": keep})
    final = wait_for_state(client, body["id"], {"predicting", "postmortem"})

    assert final["state"] == "predicting"
    questions = [m["text"] for m in final["messages"] if m["kind"] == "review"]
    assert any("quietly returns `[]`" in q for q in questions)
    assert any("`math.clampp`" in q for q in questions)
    assert any("input that isn't sorted" in q for q in questions)
    assert "What order do the intervals arrive in?" in questions  # LLM-written, gated
    # Findings are questions: the details (what's actually wrong) stay server-side.
    session = orchestrator.store.get(body["id"])
    for finding in session.findings:
        assert all(finding["detail"] not in q for q in questions)
    review_log = [e for e in session.gate_log if e["kind"] == "review"]
    assert len(review_log) == len(session.findings)
    assert any(e["layer"] == "pass" for e in review_log)
