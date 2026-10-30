"""Play one demo bug end to end against a running backend and record a transcript.

    uv run python scripts/run_demo.py merge    # or: search, lru
    uv run python scripts/run_demo.py lru --url http://localhost:8001 --out ../demo/transcripts

It plays the learner: confirms the proposed properties, predicts what a correct
version returns (worked out with the fixed demo code), climbs the hint ladder to
level 3, submits the demo's fix, and answers a follow-up question if one comes.
Everything the coach says, and every progress event, goes into a Markdown file.
"""

import argparse
import asyncio
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import websockets

DEMO_DIR = Path(__file__).resolve().parents[2] / "demo"
TERMINAL = {"postmortem"}


@dataclass(frozen=True)
class Demo:
    title: str
    file: str
    intent: str
    fix: Callable[[str], str]  # the learner's fix, applied to the demo code
    reference: Callable[[str], str]  # a fully correct version, for the predictions


DEMOS = {
    "merge": Demo(
        "Demo 1: AI-written merge_intervals",
        "merge_intervals.py",
        "Merge overlapping intervals, given as [start, end] lists sorted by start.",
        fix=lambda code: code.replace("current[0] < last[1]", "current[0] <= last[1]"),
        reference=lambda code: code.replace(
            "current[0] < last[1]", "current[0] <= last[1]"
        ).replace(
            "    merged = [intervals[0]]",
            "    intervals = sorted(map(list, intervals))\n    merged = [intervals[0]]",
        ),  # fmt: skip
    ),
    "search": Demo(
        "Demo 2: binary search that can stop making progress",
        "binary_search.py",
        "Binary search: return the index of target in the sorted list nums, or -1 if absent.",
        fix=lambda code: code.replace("lo = mid\n", "lo = mid + 1\n"),
        reference=lambda code: code.replace("lo = mid\n", "lo = mid + 1\n"),
    ),
    "lru": Demo(
        "Demo 3: LRU cache whose get() doesn't count as a use",
        "lru_cache.py",
        "An LRU cache with a fixed capacity. get(key) returns the value or -1; put(key, "
        "value) stores it, evicting the least recently used key when the cache is full.",
        fix=lambda code: code.replace(
            "        return self.cache[key]\n",
            "        self.cache.move_to_end(key)\n        return self.cache[key]\n",
        ),
        reference=lambda code: code.replace(
            "        return self.cache[key]\n",
            "        self.cache.move_to_end(key)\n        return self.cache[key]\n",
        ),
    ),
}


class Recorder:
    def __init__(self, title: str):
        self.start = time.monotonic()
        self.lines = [f"# {title}", ""]

    def at(self) -> str:
        return f"{time.monotonic() - self.start:6.1f}s"

    def say(self, text: str = "") -> None:
        print(text, flush=True)
        self.lines.append(text)

    def event(self, kind: str, text: str) -> None:
        self.say(f"- `{self.at()}` **{kind}**: {text}")

    def block(self, text: str, lang: str = "") -> None:
        self.say(f"```{lang}\n{text.rstrip()}\n```")


def predict(reference_code: str, call: str) -> str:
    """What the correct code returns for the call (for a class, its last call)."""
    namespace: dict[str, Any] = {}
    exec(compile(reference_code, "<reference>", "exec"), namespace)
    if "; " not in call:
        return repr(eval(call, namespace))  # noqa: S307: our own demo code and call
    *setup, last = call.split("; ")
    for statement in setup:
        exec(statement, namespace)
    return repr(eval(last, namespace))  # noqa: S307


async def stream(url: str, sid: str, rec: Recorder) -> None:
    """Record progress events as they arrive (the verify counter among them)."""
    ws_url = url.replace("http", "ws", 1) + f"/api/sessions/{sid}/stream"
    async with websockets.connect(ws_url) as ws:
        async for raw in ws:
            event = json.loads(raw)
            if event["type"] in ("progress", "error"):
                rec.event(event["type"], event["message"])


async def wait_for(client: httpx.AsyncClient, sid: str, states: set[str], limit: float = 600):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        body = (await client.get(f"/api/sessions/{sid}")).json()
        if body["state"] in states:
            return body
        await asyncio.sleep(1)
    raise TimeoutError(f"never reached {states}")


async def run(name: str, url: str, out: Path) -> None:
    demo = DEMOS[name]
    code = (DEMO_DIR / demo.file).read_text()
    reference = demo.reference(code)
    rec = Recorder(demo.title)
    rec.say(f"Intent: *{demo.intent}*")
    rec.say()
    rec.block(code, "python")
    rec.say()

    async with httpx.AsyncClient(base_url=url, timeout=900) as client:
        rec.say("## Properties")
        response = await client.post("/api/sessions", json={"code": code, "intent": demo.intent})
        response.raise_for_status()
        session = response.json()
        sid = session["id"]
        rec.event(
            "proposed", f"{len(session['properties'])} properties for `{session['function']}`"
        )
        for p in session["properties"]:
            rec.say(f"  - {p['statement']}")
        listener = asyncio.create_task(stream(url, sid, rec))

        rec.say()
        rec.say("## The hunt")
        keep = [{"id": p["id"], "statement": p["statement"]} for p in session["properties"]]
        (
            await client.put(f"/api/sessions/{sid}/properties", json={"properties": keep})
        ).raise_for_status()
        body = await wait_for(client, sid, {"predicting", *TERMINAL})
        if body["state"] in TERMINAL:
            rec.event("result", "no failures found")
            return finish(rec, out, name, listener)

        await coach(client, sid, reference, rec, hints=True)

        rec.say()
        rec.say("## The fix")
        fixed = demo.fix(code)
        rec.block(_diff(code, fixed), "diff")
        result = (await client.post(f"/api/sessions/{sid}/fix", json={"code": fixed})).json()
        rec.event("fix", f"{result['outcome']} ({result['inputs_checked']} inputs checked)")
        rec.say(f"> {result['message']}")
        if result.get("next_question"):
            rec.event("follow-up", result["next_question"])
        if result["state"] == "postmortem" or result["outcome"] == "passed":
            postmortem = await client.get(f"/api/sessions/{sid}/postmortem")
            if postmortem.status_code == 200:
                rec.say()
                rec.say("## Post-mortem")
                rec.block(json.dumps(postmortem.json(), indent=2), "json")
        if result["state"] == "predicting":
            rec.say()
            rec.say(
                "## The follow-up breaks it again"
                if result["outcome"] == "passed"
                else "## The fix still fails on another input"
            )
            await coach(client, sid, reference, rec, hints=False)
    finish(rec, out, name, listener)


async def coach(client: httpx.AsyncClient, sid: str, reference: str, rec: Recorder, *, hints: bool):
    question = (await client.get(f"/api/sessions/{sid}/prediction")).json()
    rec.event("question", question["question"])
    answer = predict(reference, question["call"])
    reveal = (await client.post(f"/api/sessions/{sid}/prediction", json={"answer": answer})).json()
    rec.event("prediction", f"`{answer}` (what a correct version returns)")
    rec.event(
        "reveal",
        f"{reveal['outcome']}: predicted {reveal['predicted']['text']} vs "
        f"{reveal['returned']['kind']} {reveal['returned']['text']}",
    )
    rec.say(f"> {reveal['message']}")
    if not hints:
        return
    level = reveal["hint_level"]
    while level < 3:
        response = await client.post(f"/api/sessions/{sid}/hint")
        if response.status_code != 200:
            rec.event("hint", f"unavailable: {response.json().get('detail')}")
            return
        hint = response.json()
        level = hint["level"]
        rec.say()
        rec.event(f"hint level {level}", hint.get("prose") or hint["text"])
        if hint.get("table"):
            rec.block(_table(hint["table"]))


def finish(rec: Recorder, out: Path, name: str, listener: asyncio.Task[None]) -> None:
    listener.cancel()
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{name}.md"
    path.write_text("\n".join(rec.lines) + "\n")
    print(f"\nTranscript written to {path}")


def _table(table: dict[str, Any]) -> str:
    rows = ["…"] if table["truncated_before"] else []
    for row in table["rows"]:
        values = ", ".join(f"{k} = {v}" for k, v in row["vars"].items()) or "(no change)"
        rows.append(f"{row['step']:>4}  {values}{'   ◀ goes wrong here' if row['marked'] else ''}")
    ending = f"raises {table['exception']}" if table["exception"] else f"returns {table['output']}"
    return "\n".join([*rows, f"      → {ending}"])


def _diff(before: str, after: str) -> str:
    import difflib

    lines = difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=1)
    return "\n".join(line for line in lines if not re.match(r"^(---|\+\+\+)", line))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("demo", choices=sorted(DEMOS))
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--out", type=Path, default=DEMO_DIR / "transcripts")
    args = parser.parse_args()
    asyncio.run(run(args.demo, args.url, args.out))


if __name__ == "__main__":
    main()
