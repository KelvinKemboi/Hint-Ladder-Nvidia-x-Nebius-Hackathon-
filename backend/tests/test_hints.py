import pytest
from test_gate import session

from sparring_partner.agents import tracer
from sparring_partner.agents.gate import HintGate
from sparring_partner.agents.hints import fallback_hint, generic_hint
from sparring_partner.session import MAX_HINT_LEVEL

EVIDENCE = tracer.window(
    {
        "steps": [
            {"line": 6, "locals": {"intervals": "[[0, 0], [0, 0]]", "merged": "[[0, 0]]"}},
            {"line": 10, "locals": {"intervals": "[[0, 0], [0, 0]]", "merged": "[[0, 0], [0, 0]]"}},
        ],
        "truncated": False,
        "output": "[[0, 0], [0, 0]]",
        "exception": None,
    },
    anchor=1,
)


@pytest.mark.parametrize("level", range(1, MAX_HINT_LEVEL + 1))
@pytest.mark.parametrize("template", [fallback_hint, generic_hint])
def test_fallbacks_pass_the_rules_at_their_own_level(template, level):
    s = session()
    text = template(s, level, EVIDENCE)
    gate = HintGate(llm=None, sandbox=None)
    assert gate.check_rules(s, text, level) is None, text


def test_fallback_content_per_level():
    s = session()
    hints = {level: fallback_hint(s, level, EVIDENCE) for level in range(1, 6)}
    assert "merge_intervals([[0, 0], [0, 0]])" in hints[1] and "never overlap" in hints[1]
    assert "for loop" in hints[2]
    assert "| step | values |" in hints[3] and "line" not in hints[3].split("\n\n")[1]
    assert "share an endpoint" in hints[4]
    assert "line 7" in hints[5] and "if start < merged[-1][1]:" in hints[5]
