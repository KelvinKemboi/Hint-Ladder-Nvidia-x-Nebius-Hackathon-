"""Token Factory client with role-based model routing.

Roles map to models in config (docs/PROJECT_OUTLINE.md §5.2):
  fast - test inputs and harnesses (Nano/Super class)
  deep - diagnosis, hints, post-mortem (Ultra class)
  gate - hint-gate leak judge (Super class)
"""

import json
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal, TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from sparring_partner.config import Settings, get_settings

logger = logging.getLogger(__name__)

Role = Literal["fast", "deep", "gate"]
Message = dict[str, Any]
SchemaT = TypeVar("SchemaT", bound=BaseModel)

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class StructuredOutputError(Exception):
    """The model never produced JSON matching the schema."""


@dataclass(frozen=True)
class Completion:
    text: str
    role: Role
    model: str
    latency_s: float
    prompt_tokens: int
    completion_tokens: int


@dataclass
class UsageLog:
    """Every call's latency and token usage, for the judges' slide."""

    calls: list[Completion] = field(default_factory=list)

    def record(self, completion: Completion) -> None:
        self.calls.append(completion)

    def summary(self) -> dict[str, dict[str, float]]:
        by_role: dict[str, list[Completion]] = defaultdict(list)
        for call in self.calls:
            by_role[call.role].append(call)
        return {
            role: {
                "calls": len(calls),
                "prompt_tokens": sum(c.prompt_tokens for c in calls),
                "completion_tokens": sum(c.completion_tokens for c in calls),
                "avg_latency_s": round(sum(c.latency_s for c in calls) / len(calls), 3),
                "max_latency_s": round(max(c.latency_s for c in calls), 3),
            }
            for role, calls in by_role.items()
        }


class LLM:
    def __init__(self, settings: Settings | None = None, client: AsyncOpenAI | None = None):
        self.settings = settings or get_settings()
        self.client = client or AsyncOpenAI(
            api_key=self.settings.nebius_api_key.get_secret_value(),
            base_url=self.settings.nebius_base_url,
            timeout=self.settings.llm_timeout_s,
            max_retries=self.settings.llm_max_retries,
        )
        self.usage = UsageLog()

    def model_for(self, role: Role) -> str:
        model = {
            "fast": self.settings.model_fast,
            "deep": self.settings.model_deep,
            "gate": self.settings.model_gate,
        }[role]
        if not model:
            raise ValueError(f"No model configured for role {role!r}; set MODEL_{role.upper()}")
        return model

    async def complete(self, role: Role, messages: list[Message], **kwargs: Any) -> Completion:
        """One chat completion on the model for `role`; extra kwargs go to the API."""
        model = self.model_for(role)
        start = time.perf_counter()
        response = await self.client.chat.completions.create(
            model=model, messages=messages, **kwargs
        )
        latency = time.perf_counter() - start

        usage = response.usage
        completion = Completion(
            text=response.choices[0].message.content or "",
            role=role,
            model=model,
            latency_s=latency,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
        )
        self.usage.record(completion)
        logger.info(
            "llm role=%s model=%s latency=%.2fs tokens=%d+%d",
            role,
            model,
            latency,
            completion.prompt_tokens,
            completion.completion_tokens,
        )
        return completion

    async def complete_json(
        self,
        role: Role,
        messages: list[Message],
        schema: type[SchemaT],
        *,
        retries: int = 2,
        **kwargs: Any,
    ) -> SchemaT:
        """Completion parsed into `schema`; on invalid output, show the error and retry.

        Uses the API's JSON mode unless the caller passes its own `response_format`.
        """
        kwargs.setdefault("response_format", {"type": "json_object"})
        instruction = (
            "Reply with only a JSON object that matches this JSON Schema, with no other text. "
            "Send the data itself (an instance), not a copy of the schema:\n"
            + json.dumps(schema.model_json_schema())
        )
        messages = [*messages, {"role": "system", "content": instruction}]
        error = ""
        for _ in range(retries + 1):
            completion = await self.complete(role, messages, **kwargs)
            try:
                return schema.model_validate_json(extract_json(completion.text))
            except ValidationError as exc:
                error = str(exc)
            messages = [
                *messages,
                {"role": "assistant", "content": completion.text},
                {
                    "role": "user",
                    "content": f"That reply was not valid:\n{error}\n"
                    "Reply again with only the corrected JSON object.",
                },
            ]
        raise StructuredOutputError(f"{schema.__name__} after {retries + 1} attempts: {error}")


def extract_json(text: str) -> str:
    """Pull the JSON object out of a reply that may include reasoning or code fences."""
    text = _THINK.sub("", text).strip()
    if fenced := _FENCE.search(text):
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


@lru_cache
def get_llm() -> LLM:
    return LLM()
