"""Counterexample model, deduplication and ranking.

Depends only on the standard library so it can run inside the sandbox harness
as well as in the backend.
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

# The implicit property every function must satisfy
NO_CRASH = "does not raise an exception"
MAX_REPR = 500


@dataclass(frozen=True)
class Property:
    """A confirmed property: `check(args, output)` returns True when it holds."""

    name: str
    check: Callable[[tuple[Any, ...], Any], bool]


@dataclass
class Counterexample:
    """A minimal failing input and what went wrong on it."""

    function: str
    args: list[str]  # repr of each positional argument
    call: str  # e.g. "merge([[1, 4], [4, 5]])"
    violated_property: str  # the property whose search found this input
    violated_properties: list[str] = field(default_factory=list)  # every property it breaks
    output: str | None = None  # repr of the return value; None if it raised
    exception_type: str | None = None
    exception_message: str | None = None
    exception_line: int | None = None  # line in the user's code where it raised
    check_errors: dict[str, str] = field(default_factory=dict)  # property -> predicate error
    complexity: int = 0
    timed_out: bool = False  # stopped by the per-call time limit (e.g. an infinite loop)
    hand_picked: bool = False  # an edge case rather than a Hypothesis find

    @property
    def raised(self) -> bool:
        return self.exception_type is not None

    def signature(self) -> tuple[Any, ...]:
        """Approximate root-cause key: where it crashed, or which properties it breaks."""
        if self.timed_out:  # where a loop gets interrupted is arbitrary
            return ("timeout",)
        if self.raised:
            return ("raise", self.exception_type, self.exception_line)
        return ("wrong-output", *self.violated_properties)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def short_repr(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= MAX_REPR else text[: MAX_REPR - 3] + "..."


def complexity(value: Any) -> int:
    """How hard an input is to read: element count plus digit and character count."""
    if value is None or isinstance(value, bool):
        return 1
    if isinstance(value, int):
        return 1 + len(str(abs(value)))
    if isinstance(value, float):
        if not math.isfinite(value):
            return 3
        return 2 + len(repr(abs(value)))
    if isinstance(value, str | bytes):
        return 1 + len(value)
    if isinstance(value, dict):
        return 1 + sum(complexity(k) + complexity(v) for k, v in value.items())
    if isinstance(value, list | tuple | set | frozenset):
        return 1 + sum(complexity(v) for v in value)
    return 1 + len(repr(value))


def rank_key(ce: Counterexample, property_order: Sequence[str] = ()) -> tuple[Any, ...]:
    """Simplest and most teachable first.

    1. smallest input (easiest to predict by hand)
    2. a wrong output before a crash (gives the user something to predict)
    3. a hand-picked edge case over an equally small Hypothesis find: edge cases are
       chosen to be readable ([[1, 4], [4, 5]] rather than [[0, 0], [0, 0]])
    4. fewest violated properties (one clear lesson at a time)
    5. the order the user confirmed the properties in
    """
    order = {name: i for i, name in enumerate(property_order)}
    return (
        ce.complexity,
        ce.raised,
        not ce.hand_picked,
        len(ce.violated_properties),
        order.get(ce.violated_property, len(order)),
    )


def rank(
    counterexamples: Sequence[Counterexample], property_order: Sequence[str] = ()
) -> list[Counterexample]:
    return sorted(counterexamples, key=lambda ce: rank_key(ce, property_order))


def dedupe(
    counterexamples: Sequence[Counterexample], property_order: Sequence[str] = ()
) -> list[Counterexample]:
    """Keep one counterexample per root cause, choosing the best-ranked one.

    Two counterexamples count as the same cause when they share an input, or the
    same signature: the same exception at the same line, or the same set of broken
    properties. The real root cause comes later from the diagnosis; this only
    stops the user being asked about one bug several times.
    """
    best: dict[Any, Counterexample] = {}
    for ce in rank(counterexamples, property_order):
        for key in (("call", ce.call), ce.signature()):
            if key in best:
                break
        else:
            best[("call", ce.call)] = ce
            best[ce.signature()] = ce
    unique = {id(ce): ce for ce in best.values()}.values()
    return rank(list(unique), property_order)
