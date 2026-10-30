"""Predict-then-reveal: ask what the failing call should return, then compare."""

import ast
import contextlib
import json
import re
from dataclasses import dataclass
from typing import Any

from sparring_partner.session import Session

LEAD_IN = re.compile(
    r"^(?:(?:i\s+think|i\s+expect)\s+)?(?:it\s+)?(?:should\s+)?"
    r"(?:return(?:s)?|give(?:s)?|be|is|=>|->|=)\s*:?\s*",
    re.I,
)
ERROR_WORDS = re.compile(r"\b(?:raises?|error|exception|crash(?:es)?|throws?)\b", re.I)
ERROR_NAME = re.compile(r"\b([A-Z]\w*(?:Error|Exception))\b")
JSON_WORDS = {"true": "True", "false": "False", "null": "None", "none": "None"}


class PredictionParseError(ValueError):
    pass


@dataclass(frozen=True)
class Prediction:
    kind: str  # "value" or "error"
    value: Any = None
    error_name: str | None = None

    def as_config(self) -> dict[str, Any]:
        return {"kind": self.kind, "value": self.value}

    def describe(self) -> str:
        if self.kind == "error":
            return f"an error ({self.error_name})" if self.error_name else "an error"
        return f"`{self.value!r}`"


def parse_prediction(answer: str) -> Prediction:
    """Read the user's answer as a Python literal, tolerating formatting differences:
    code fences or backticks, "it returns ...", a trailing period, JSON spellings
    (true/null), and "it raises an error"."""
    text = answer.strip()
    if fenced := re.fullmatch(r"```(?:\w+)?\s*(.*?)\s*```", text, re.DOTALL):
        text = fenced.group(1)
    text = text.strip().strip("`").strip()
    text = LEAD_IN.sub("", text).strip().strip("`").rstrip(".").strip()

    for candidate in (text, _python_words(text)):
        try:
            return Prediction("value", ast.literal_eval(candidate))
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            pass
    try:
        return Prediction("value", json.loads(text))
    except (ValueError, RecursionError):
        pass
    if ERROR_WORDS.search(answer):
        name = ERROR_NAME.search(answer)
        return Prediction("error", error_name=name.group(1) if name else None)
    raise PredictionParseError(
        "Couldn't read that as a Python value. Try something like [[1, 5]] or 'raises an error'."
    )


def _python_words(text: str) -> str:
    return re.sub(
        r"\b(true|false|null|none)\b", lambda m: JSON_WORDS[m.group(1).lower()], text, flags=re.I
    )


def current_case(session: Session) -> dict[str, Any]:
    return session.counterexamples[session.current_case]


def question(session: Session) -> str:
    return what_should(session, current_case(session)["call"])


def what_should(session: Session, call: str) -> str:
    """What should `f(x)` return? For a class, the question is about the last call:
    After `cache = LRUCache(2); cache.put(1, 1)`, what should `cache.get(1)` return?"""
    setup, _, last = call.rpartition("; ")
    if session.stateful and setup:
        return f"After `{setup}`, what should `{last}` return?"
    return f"What should `{call}` return?"


def describe_actual(actual: dict[str, Any]) -> str:
    if actual.get("timed_out"):
        return f"never finishes: it was still running when stopped after {stopped_after(actual)}"
    return f"raises `{actual['error']}`" if actual["error"] else f"returns `{actual['output']}`"


def case_outcome(session: Session, ce: dict[str, Any]) -> dict[str, Any]:
    """A counterexample's outcome as `describe_actual` takes it. For a class, the
    output is the last call's result (what the prediction question asks about)."""
    if ce["exception_type"]:
        error = f"{ce['exception_type']}: {ce['exception_message']}"
        return {"output": None, "error": error, "timed_out": ce.get("timed_out", False)}
    output = ce["output"]
    if session.stateful:
        with contextlib.suppress(ValueError, SyntaxError, TypeError, IndexError):
            output = repr(ast.literal_eval(output)[-1])
    return {"output": output, "error": None, "timed_out": False}


def stopped_after(actual: dict[str, Any]) -> str:
    """'1.0s' from 'TimeoutError: did not finish within 1.0s'."""
    return actual["error"].rpartition(" ")[2] if actual.get("error") else "the time limit"
