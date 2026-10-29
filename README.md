# Hint-Ladder

Debugging coach that finds the bugs in your code (or AI-generated code) and guides you to fix them yourself.

**Sparring Partner** knows the answer but won't tell you. It runs property-based tests on your code in a
Nebius Token Factory sandbox, then coaches you to the bug with predict-then-reveal questions and a
5-level hint ladder. A hint gate checks every message so it never hands you the fix.

Built for the **Coding and Agentic Engineering** track: agents that write, run, and test code in Token Factory.

## Quickstart

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 20.19+.

```bash
cp .env.example .env        # add your Token Factory key and model IDs
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

**Sandbox**: user code only ever runs in the Token Factory sandbox (beta; [request access](https://tokenfactory.nebius.com/sandboxes/about)).
Set `NEBIUS_PROJECT_ID`, then build the image once (Python 3.12 + Hypothesis) and smoke-test it:

```bash
cd backend
uv run python scripts/build_sandbox_image.py
uv run python scripts/build_sandbox_image.py --check
```

For local development without sandbox access, set `SANDBOX_BACKEND=local`. That runs code in an
unisolated subprocess on your machine (and macOS ignores the memory limit), so only use it with code you trust.

**Checks** (the same ones CI runs):

```bash
cd backend && uv run ruff check . && uv run ruff format --check . && uv run pytest
cd frontend && npm run build
```

## Layout

```
backend/    FastAPI app (src/sparring_partner), tests
frontend/   React + Vite web UI
docs/       Project outline
scripts/    GitHub issue setup
```

## Docs

- [Project outline](docs/PROJECT_OUTLINE.md): pitch, architecture, the hint gate, scope and milestones
- [`scripts/create_github_issues.sh`](scripts/create_github_issues.sh): creates the labels, milestones and issues on GitHub (`DRY_RUN=1` to preview)

## License

[MIT](LICENSE)
