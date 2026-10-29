"""Property-based testing engine: finds and shrinks failures in the user's code."""

from sparring_partner.testing.capture import find_counterexamples
from sparring_partner.testing.counterexamples import (
    NO_CRASH,
    Counterexample,
    Property,
    complexity,
    dedupe,
    rank,
)

__all__ = [
    "NO_CRASH",
    "Counterexample",
    "Property",
    "complexity",
    "dedupe",
    "find_counterexamples",
    "rank",
]
