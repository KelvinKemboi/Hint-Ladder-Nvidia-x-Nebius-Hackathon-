import pytest
from fakes import COVERED, MERGE_CODE, NO_OVERLAP, FakeLLM
from test_diagnosis import TOUCHING

from sparring_partner.agents.review import (
    Reviewer,
    api_references,
    swallowed_exceptions,
    undefined_names,
)
from sparring_partner.config import Settings
from sparring_partner.sandbox import LocalBackend, Sandbox
from sparring_partner.session import Mode, ParamSpec, PropertySpec, Session


def wrap(body: str) -> str:
    return "def f(x):\n    try:\n        return x[0]\n" + body


@pytest.mark.parametrize(
    "handler, question",
    [
        ("    except:\n        pass\n", "does anyone ever find out"),
        ("    except Exception:\n        pass\n", "does anyone ever find out"),
        ("    except Exception:\n        return []\n", "quietly returns `[]`"),
        ("    except (ValueError, Exception):\n        return None\n", "quietly returns `None`"),
        ("    except:\n        log(x)\n        return x\n", "catches every possible exception"),
    ],
)
def test_swallowed_exceptions(handler, question):
    [finding] = swallowed_exceptions(wrap(handler))
    assert finding.kind == "swallowed_exception"
    assert question in finding.question
    assert "line" not in finding.question.lower()  # the gate allows no line numbers early
    assert finding.detail.startswith("line 4:")


@pytest.mark.parametrize(
    "handler",
    [
        "    except IndexError:\n        return None\n",  # narrow: handled on purpose
        "    except Exception as e:\n        raise ValueError('bad') from e\n",  # re-raised
    ],
)
def test_handled_exceptions_are_fine(handler):
    assert swallowed_exceptions(wrap(handler)) == []


def test_undefined_names():
    code = (
        "import math\nfrom collections import deque\n\n"
        "def f(xs, *rest, key=None):\n"
        "    out = [y for y in xs]\n"
        "    total = math.fsum(out) + len(rest)\n"
        "    return sorted_by(out, key), deque(out), total\n"
    )
    [finding] = undefined_names(code)
    assert "`sorted_by`" in finding.question and finding.detail.startswith("line 7:")


def test_api_references_keep_the_longest_chain():
    code = (
        "import math\nimport os\nimport numpy as np\nfrom collections import OrderedDictt\n"
        "math.clamp(1, 2, 3)\nos.path.joinn('a')\nos.path.join('b')\nnp.zeros(3)\n"
    )
    refs = {".".join([r["module"], *r["attrs"]]) for r in api_references(code)}
    assert refs == {
        "math.clamp",
        "os.path.joinn",
        "os.path.join",
        "numpy.zeros",
        "collections.OrderedDictt",
    }


# In the sandbox
REVIEW_CODE = '''\
import math
import xml


def merge_intervals(intervals):
    """Merge overlapping intervals."""
    try:
        merged = [list(intervals[0])]
    except Exception:
        return []
    for start, end in intervals[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def width(interval):
    xml.etree.ElementTree  # a real submodule, just not imported yet
    return math.clampp(interval[1] - interval[0], 0, 100)
'''


def review_session() -> Session:
    s = Session(
        code=REVIEW_CODE,
        intent="merge overlapping intervals",
        function="merge_intervals",
        mode=Mode.REVIEW,
    )
    s.params = [ParamSpec("intervals", "intervals")]
    s.properties = [PropertySpec(*NO_OVERLAP), PropertySpec(*COVERED)]
    s.counterexamples = [TOUCHING]  # stands in for what exploration found
    return s


@pytest.mark.anyio
async def test_review_finds_the_usual_ai_code_failures():
    concerns = {
        "concerns": [
            {
                "kind": "wrong_assumption",
                "detail": "assumes the input is sorted by start",
                "question": "What order do the intervals arrive in?",
                "example_args": "([[4, 5], [1, 2], [2, 3]],)",
            },
            {
                "kind": "wrong_assumption",
                "detail": "invalid input from the model",
                "question": "What about backwards intervals?",
                "example_args": "([(5, 2), (3, 4)],)",  # start > end: not a valid input
            },
            {
                "kind": "missing_edge_case",
                "detail": "fine on a single interval",
                "question": "What about just one interval?",
                "example_args": "([[1, 2]],)",
            },
        ]
    }
    llm = FakeLLM(ReviewDraft=[concerns])
    reviewer = Reviewer(llm, Sandbox(LocalBackend(), Settings(_env_file=None)))
    session = review_session()

    findings = await reviewer.review(session)
    by_kind = {}
    for f in findings:
        by_kind.setdefault(f.kind, []).append(f)

    assert "quietly returns `[]`" in by_kind["swallowed_exception"][0].question
    [api] = by_kind["invented_api"]
    assert "`math.clampp`" in api.question and "does not exist" in api.detail
    assumptions = [f.question for f in by_kind["assumption"]]
    assert any("input that isn't sorted" in q for q in assumptions)
    # The LLM's suggested inputs were run: the failing one is now a counterexample.
    wrong, invalid = by_kind["wrong_assumption"]
    missing = by_kind["missing_edge_case"][0]
    assert wrong.counterexample["call"] == "merge_intervals([[4, 5], [1, 2], [2, 3]])"
    assert invalid.counterexample is None  # never run: not a valid input
    assert missing.counterexample is None
    assert [ce["call"] for ce in session.counterexamples] == [
        TOUCHING["call"],
        "merge_intervals([[4, 5], [1, 2], [2, 3]])",
    ]
    assert session.findings and session.findings[0]["detail"]  # details kept server-side
    prompt = llm.asked("ReviewDraft")[0][-1]["content"]
    assert "math.clampp does not exist" in prompt  # told what's already found
    assert MERGE_CODE not in prompt
