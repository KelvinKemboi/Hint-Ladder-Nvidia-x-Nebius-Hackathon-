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
