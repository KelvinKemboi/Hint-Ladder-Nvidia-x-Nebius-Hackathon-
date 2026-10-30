from types import SimpleNamespace

import httpx
import pytest
from openai import NotFoundError
from pydantic import BaseModel

from sparring_partner.config import Settings
from sparring_partner.llm import LLM, ModelConfigError, StructuredOutputError, extract_json

pytestmark = pytest.mark.anyio


class FakeCompletions:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.replies.pop(0)))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )


def make_llm(*replies, **settings):
    completions = FakeCompletions(replies)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    models = {"model_fast": "nano", "model_deep": "ultra", "model_gate": "super"}
    config = Settings(_env_file=None, **(models | settings))
    return LLM(config, client=client), completions


class Hint(BaseModel):
    level: int
    text: str


async def test_routes_each_role_to_its_model():
    llm, completions = make_llm("a", "b", "c")
    for role in ("fast", "deep", "gate"):
        await llm.complete(role, [{"role": "user", "content": "hi"}], temperature=0)

    assert [c["model"] for c in completions.calls] == ["nano", "ultra", "super"]
    assert completions.calls[0]["temperature"] == 0


async def test_missing_model_is_a_clear_error():
    llm, _ = make_llm("x", model_deep="")
    with pytest.raises(ValueError, match="MODEL_DEEP"):
        await llm.complete("deep", [])


async def test_unknown_model_names_the_setting_to_fix(monkeypatch):
    monkeypatch.delenv("MODEL_FAST", raising=False)
    llm, completions = make_llm(model_fast="your_model_id_here")

    async def not_found(**kwargs):
        request = httpx.Request("POST", "https://example.test/v1/chat/completions")
        response = httpx.Response(404, request=request)
        raise NotFoundError("The model does not exist.", response=response, body=None)

    completions.create = not_found
    with pytest.raises(ModelConfigError, match="'your_model_id_here' .MODEL_FAST.*restart"):
        await llm.complete("fast", [])


async def test_a_shell_variable_overriding_env_is_named(monkeypatch):
    monkeypatch.setenv("MODEL_FAST", "your_model_id_here")
    llm, completions = make_llm(model_fast="your_model_id_here")

    async def not_found(**kwargs):
        request = httpx.Request("POST", "https://example.test/v1/chat/completions")
        raise NotFoundError("nope", response=httpx.Response(404, request=request), body=None)

    completions.create = not_found
    with pytest.raises(ModelConfigError, match="set in the shell.*unset MODEL_FAST"):
        await llm.complete("fast", [])


async def test_records_latency_and_tokens():
    llm, _ = make_llm("a", "b", "c")
    completion = await llm.complete("fast", [])
    await llm.complete("fast", [])
    await llm.complete("deep", [])

    assert completion.text == "a"
    assert completion.latency_s >= 0
    summary = llm.usage.summary()
    assert summary["fast"]["calls"] == 2
    assert summary["fast"]["prompt_tokens"] == 20
    assert summary["deep"]["completion_tokens"] == 5


async def test_complete_json_parses_reply_with_reasoning_and_fence():
    reply = '<think>level 1 means symptom</think>\n```json\n{"level": 1, "text": "Try []"}\n```'
    llm, completions = make_llm(reply)

    hint = await llm.complete_json("deep", [{"role": "user", "content": "hint"}], Hint)

    assert hint == Hint(level=1, text="Try []")
    assert "JSON Schema" in completions.calls[0]["messages"][-1]["content"]
    assert completions.calls[0]["response_format"] == {"type": "json_object"}


async def test_complete_json_retries_with_the_validation_error():
    llm, completions = make_llm('{"level": "one"}', '{"level": 1, "text": "ok"}')

    hint = await llm.complete_json("deep", [], Hint)

    assert hint.level == 1
    retry_messages = completions.calls[1]["messages"]
    assert retry_messages[-2] == {"role": "assistant", "content": '{"level": "one"}'}
    assert "not valid" in retry_messages[-1]["content"]


async def test_complete_json_gives_up_after_retries():
    llm, completions = make_llm("nope", "still nope")
    with pytest.raises(StructuredOutputError, match="Hint"):
        await llm.complete_json("deep", [], Hint, retries=1)
    assert len(completions.calls) == 2


def test_extract_json():
    assert extract_json('Sure! {"a": 1} hope that helps') == '{"a": 1}'
    assert extract_json('<think>{"wrong": 1}</think>{"a": 1}') == '{"a": 1}'
    assert extract_json("no json") == "no json"
