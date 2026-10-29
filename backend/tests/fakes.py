"""Test doubles shared across test modules."""

from typing import Any

from sparring_partner.llm import StructuredOutputError


class FakeLLM:
    """Scripted structured replies, keyed by the requested schema's class name.

    A reply may be a dict or a callable taking the messages and returning a dict.
    An empty queue raises StructuredOutputError, like a model that keeps failing.
    """

    def __init__(self, **replies: list[Any]):
        self.replies = {name: list(queue) for name, queue in replies.items()}
        self.calls: list[tuple[str, str, list[dict[str, Any]]]] = []

    async def complete_json(self, role, messages, schema, **kwargs):
        self.calls.append((role, schema.__name__, list(messages)))  # callers may extend it
        queue = self.replies.get(schema.__name__)
        if queue is None:
            raise AssertionError(f"unexpected {schema.__name__} request")
        if not queue:  # scripted to run dry: behave like a model that never gets it right
            raise StructuredOutputError(f"{schema.__name__}: no valid reply")
        reply = queue.pop(0)
        return schema.model_validate(reply(messages) if callable(reply) else reply)

    def asked(self, schema_name: str) -> list[list[dict[str, Any]]]:
        return [messages for _, name, messages in self.calls if name == schema_name]


# Demo bug 1: touching intervals aren't merged, and the input is assumed sorted.
MERGE_CODE = '''\
def merge_intervals(intervals):
    """Merge overlapping intervals."""
    if not intervals:
        return []
    merged = [list(intervals[0])]
    for start, end in intervals[1:]:
        if start < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged
'''

NO_OVERLAP = (
    "output intervals never overlap",
    "def check(args, output):\n    return all(a[1] < b[0] for a, b in zip(output, output[1:]))\n",
)
COVERED = (
    "every input point is still covered",
    "def check(args, output):\n"
    "    def points(xs):\n"
    "        return {x for s, e in xs for x in range(s, e + 1)}\n"
    "    return points(args[0]) == points(output)\n",
)
SORTED = (
    "output is sorted",
    "def check(args, output):\n    return output == sorted(output)\n",
)
INTERVALS_STRATEGY = (
    "def strategy():\n"
    "    interval = st.tuples(st.integers(0, 20), st.integers(0, 5)).map(\n"
    "        lambda p: [p[0], p[0] + p[1]]\n"
    "    )\n"
    "    return st.tuples(st.lists(interval, max_size=6))\n"
)


def prop(pair: tuple[str, str]) -> dict[str, str]:
    return {"statement": pair[0], "predicate": pair[1]}


INPUT_SPEC = {
    "params": [{"name": "intervals", "kind": "intervals", "description": "[start, end] pairs"}],
    "examples": [
        {"args": "([[1, 3], [2, 4]],)", "output": "[[1, 4]]"},
        {"args": "([],)", "output": "[]"},
    ],
}

# The reference fix for MERGE_CODE: sort first, and merge touching intervals.
FIXED_MERGE = MERGE_CODE.replace(
    "    merged = [list(intervals[0])]\n",
    "    intervals = sorted(intervals)\n    merged = [list(intervals[0])]\n",
).replace("if start < merged", "if start <= merged")
# Fixes only the comparison; unsorted input still loses intervals.
HALF_FIXED_MERGE = MERGE_CODE.replace("if start < merged", "if start <= merged")
BUG_LINE = 7  # `if start < merged[-1][1]:`

DIAGNOSIS = {
    "root_cause": "SECRET_ROOT_CAUSE: the strict < treats touching intervals as separate",
    "line": BUG_LINE,
    "region": "the for loop in merge_intervals",
    "bug_class": "boundary condition",
    "concept": "SECRET_CONCEPT: intervals that share an endpoint overlap",
    "reference_fix": FIXED_MERGE,
}
