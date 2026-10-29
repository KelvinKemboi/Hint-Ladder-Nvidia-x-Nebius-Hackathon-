"""The post-mortem: what kind of bug it was, how to spot it next time, what to try next.

Structured so sessions can feed a personal bug profile later. Written once the
fix is verified, so there is nothing left to give away: it isn't gated.
"""

from typing import Any

from pydantic import BaseModel, Field

from sparring_partner.agents.testgen import JsonLLM
from sparring_partner.llm import Message
from sparring_partner.session import Session

SYSTEM = "You are a debugging coach writing a short, encouraging post-mortem. Reply with JSON only."


class PostmortemDraft(BaseModel):
    bug_class: str = Field(
        description="short label: 'off-by-one', 'boundary condition', 'mutation during "
        "iteration', 'wrong input assumption', 'missing edge case', ..."
    )
    how_to_spot: str = Field(description="how to spot this kind of bug next time, 1-2 sentences")
    other_edge_cases: list[str] = Field(
        min_length=1, max_length=4, description="other inputs worth thinking about"
    )


async def write_postmortem(llm: JsonLLM, session: Session) -> dict[str, Any]:
    stats = {
        "cases": session.solved_cases,
        "hint_level_reached": max((c["hint_level"] for c in session.solved_cases), default=0),
        "fix_attempts": len(session.fix_attempts),
        "inputs_checked": next(
            (
                a["inputs_checked"]
                for a in reversed(session.fix_attempts)
                if a["outcome"] == "passed"
            ),
            0,
        ),
    }
    if not session.solved_cases:
        return {
            "bug_class": None,
            "bug_classes": [],
            "how_to_spot": "No failures were found against your spec.",
            "other_edge_cases": [],
            **stats,
        }

    cases = "\n".join(
        f"- {c['call']}: {c['bug_class'] or 'unknown'} ({c['root_cause'] or 'no diagnosis'}); "
        f"hints used: {c['hint_level']}"
        for c in session.solved_cases
    )
    messages: list[Message] = [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": (
                f"Intent: {session.intent}\n\nOriginal code:\n```python\n{session.original_code}"
                f"\n```\n\nFixed code:\n```python\n{session.code}\n```\n\n"
                f"Bugs the learner found and fixed:\n{cases}\n\n"
                "Write the post-mortem: the main bug class, how to spot this kind of bug next "
                "time (1-2 sentences), and 1-4 other edge cases worth thinking about."
            ),
        },
    ]
    draft = await llm.complete_json("deep", messages, PostmortemDraft)
    return {
        "bug_class": draft.bug_class,
        "bug_classes": [c["bug_class"] for c in session.solved_cases],
        "how_to_spot": draft.how_to_spot,
        "other_edge_cases": draft.other_edge_cases,
        **stats,
    }
