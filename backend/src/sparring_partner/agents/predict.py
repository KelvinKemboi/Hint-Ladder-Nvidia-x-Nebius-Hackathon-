"""Predict-then-reveal: ask what the failing call should return, then compare."""

import ast
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
    return f"What should `{current_case(session)['call']}` return?"


def describe_actual(actual: dict[str, Any]) -> str:
    return f"raises `{actual['error']}`" if actual["error"] else f"returns `{actual['output']}`"
