# Hint-Ladder

Debugging coach that finds the bugs in your code (or AI-generated code) and guides you to fix them yourself.

**Sparring Partner** knows the answer but won't tell you. It runs property-based tests on your code in a
Nebius Token Factory sandbox, then coaches you to the bug with predict-then-reveal questions and a
5-level hint ladder. A hint gate checks every message so it never hands you the fix.

Built for the **Coding and Agentic Engineering** track: agents that write, run, and test code in Token Factory.

## Quickstart

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 20.19+.

```bash
cp .env.example .env # add your Token Factory key and model IDs
```

**Backend** (http://localhost:8000):

```bash
cd backend
uv sync
uv run uvicorn sparring_partner.main:app --reload
```

**Frontend** (http://localhost:5173, proxies `/api` to the backend):

```bash
cd frontend
npm install
npm run dev
```

The UI has the code editor on the left and the coaching chat on the right. Pick a mode (Debug or
AI Code Review), say what the code should do, and press Start. Then confirm the properties, predict
what the failing call should return, climb the hint meter if you need to, and submit your fix from
the editor. For demos, set `EXPOSE_GATE_LOG=true` to show the **Gate log** drawer: every message
the gate blocked, and why.

**What it can test**: a Python function, or a class. A class is tested as a state machine with
Hypothesis's `RuleBasedStateMachine`: it is created, its public methods are called in random
order, and the properties are checked after every call; a failure shrinks to the shortest call
sequence, like `cache = LRUCache(2); cache.put(1, 1); cache.put(2, 1); cache.get(1);
cache.put(3, 1); cache.get(1)`. Code that never finishes (an infinite loop) is stopped after
`EXPLORE_CALL_TIMEOUT_S` and shown as a symptom to predict, not an error.

**Sandbox**: user code only ever runs in the Token Factory sandbox (beta; [request access](https://tokenfactory.nebius.com/sandboxes/about)).
Set `NEBIUS_PROJECT_ID`, then build the image once (Python 3.12 + Hypothesis) and smoke-test it:

```bash
cd backend
uv run python scripts/build_sandbox_image.py
uv run python scripts/build_sandbox_image.py --check
```

Without sandbox access, set `SANDBOX_BACKEND=docker` to use an isolated local container
instead (no network, read-only, capped memory, CPU and processes). Build its image with
`build_sandbox_image.py --docker`. `SANDBOX_BACKEND=local` runs code in an unisolated
subprocess on your machine (and macOS ignores the memory limit), so only use it in development,
with code you trust.

**Checks** (the same ones CI runs):

```bash
cd backend && uv run ruff check . && uv run ruff format --check . && uv run pytest
cd frontend && npm run build
```

## Layout

```
backend/ FastAPI app (src/sparring_partner), tests
frontend/ React + Vite web UI
demo/ The three demo bugs, their walkthrough and recorded runs
docs/ Project outline
scripts/ GitHub issue setup
```

## Docs

- [Project outline](docs/PROJECT_OUTLINE.md): pitch, architecture, the hint gate, scope and milestones
- [Demo walkthrough](demo/README.md): three bugs end to end, with transcripts of real runs
  (`backend/scripts/run_demo.py` records new ones)
- [`scripts/create_github_issues.sh`](scripts/create_github_issues.sh): creates the labels, milestones and issues on GitHub (`DRY_RUN=1` to preview)

## License

[MIT](LICENSE)
