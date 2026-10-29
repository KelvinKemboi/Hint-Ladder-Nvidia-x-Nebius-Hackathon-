#!/usr/bin/env bash
# Creates labels, milestones and issues for Sparring Partner (Hint-Ladder).
# See docs/PROJECT_OUTLINE.md for the full plan.
#
# Usage:
#   scripts/create_github_issues.sh                 # uses the current repo
#   REPO=owner/name scripts/create_github_issues.sh
#   DRY_RUN=1 scripts/create_github_issues.sh       # print what would be created
#
# Safe to re-run: existing labels, milestones and issues (matched by title) are skipped.
# Requires: gh (authenticated), jq.

set -euo pipefail

command -v gh >/dev/null || { echo "gh CLI is required" >&2; exit 1; }
command -v jq >/dev/null || { echo "jq is required" >&2; exit 1; }

REPO="${REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
DRY_RUN="${DRY_RUN:-0}"

echo "Target repo: $REPO (dry run: $DRY_RUN)"

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] $*"
  else
    "$@"
  fi
}

# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------
existing_labels="$(gh label list --repo "$REPO" --limit 200 --json name -q '.[].name')"

label() {
  local name="$1" color="$2" desc="$3"
  if grep -Fxq "$name" <<<"$existing_labels"; then
    echo "label exists: $name"
  else
    run gh label create "$name" --repo "$REPO" --color "$color" --description "$desc"
  fi
}

label "mvp"            "0e8a16" "Required for the hackathon MVP"
label "stretch"        "c5def5" "Stretch goal"
label "backend"        "1d76db" "Python / FastAPI backend"
label "frontend"       "5319e7" "Web UI"
label "agent"          "d93f0b" "LLM agent logic and prompts"
label "token-factory"  "fbca04" "Nebius Token Factory inference or sandbox"
label "testing-engine" "006b75" "Hypothesis, tracing, mutation testing"
label "hint-gate"      "b60205" "Hint gate / leak prevention"
label "demo"           "f9d0c4" "Demo bugs, script and presentation"
label "infra"          "bfdadc" "Repo, CI, deployment"
label "docs"           "0075ca" "Documentation"

# ---------------------------------------------------------------------------
# Milestones
# ---------------------------------------------------------------------------
existing_milestones="$(gh api "repos/$REPO/milestones?state=all&per_page=100" -q '.[].title')"

milestone() {
  local title="$1" desc="$2"
  if grep -Fxq "$title" <<<"$existing_milestones"; then
    echo "milestone exists: $title"
  else
    run gh api "repos/$REPO/milestones" -f title="$title" -f description="$desc" --silent
  fi
}

M0="M0 — Foundations"
M1="M1 — Core engine"
M2="M2 — AI Code Review"
M3="M3 — Web UI"
M4="M4 — Demo"
MS="Stretch"

milestone "$M0" "Repo scaffold, Token Factory client, sandbox runner, session model"
milestone "$M1" "Properties, test generation, diagnosis, hint ladder, hint gate, fix verification"
milestone "$M2" "AI Code Review mode checks"
milestone "$M3" "Editor, chat panel, hint meter"
milestone "$M4" "Demo bugs, gate evals, deployment, rehearsal"
milestone "$MS" "Explore mode, bug profile, JavaScript, VS Code extension"

# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------
existing_issues="$(gh issue list --repo "$REPO" --state all --limit 500 --json title -q '.[].title')"

# issue <title> <comma-separated labels> <milestone>   (body read from stdin)
issue() {
  local title="$1" labels="$2" ms="$3" body
  body="$(cat)"
  if grep -Fxq "$title" <<<"$existing_issues"; then
    echo "issue exists: $title"
    return
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[dry-run] issue: $title  [$labels]  ($ms)"
  else
    gh issue create --repo "$REPO" --title "$title" --label "$labels" --milestone "$ms" --body "$body"
  fi
}

# ----- M0: Foundations -----------------------------------------------------

issue "Scaffold repo: backend, frontend, tooling and CI" "mvp,infra" "$M0" <<'EOF'
Set up the project skeleton.

- [ ] `backend/`: Python 3.11+, FastAPI, Pydantic, managed with `uv`
- [ ] `frontend/`: React + Vite
- [ ] `ruff` + `pytest` config; GitHub Actions running lint and tests
- [ ] `.env.example` with `NEBIUS_API_KEY`, `NEBIUS_BASE_URL`, `MODEL_FAST`, `MODEL_DEEP`, `MODEL_GATE`
- [ ] README: quickstart and a link to `docs/PROJECT_OUTLINE.md`
EOF

issue "Token Factory client with role-based model routing" "mvp,backend,token-factory" "$M0" <<'EOF'
Thin wrapper around Nebius Token Factory's OpenAI-compatible API.

- [ ] `llm.complete(role, messages, **kw)` where `role ∈ {fast, deep, gate}`
- [ ] Model IDs come from env (`MODEL_FAST` = Nano/Super class, `MODEL_DEEP` = Ultra class, `MODEL_GATE` = Super class)
- [ ] Structured/JSON output helper with Pydantic validation and retry
- [ ] Log per-call latency and token usage (useful for the judges' slide)
- [ ] Unit tests with a mocked client

See outline §5.2.
EOF

issue "Sandbox runner: execute user code + harnesses in Token Factory sandbox" "mvp,backend,token-factory,testing-engine" "$M0" <<'EOF'
All untrusted code runs in the sandbox, never in the API process.

- [ ] `sandbox.run(code, harness, timeout_s, inputs?) -> RunResult` (stdout, stderr, exception, return values, trace payload)
- [ ] Target: Token Factory sandbox; local subprocess fallback behind `SANDBOX_BACKEND=local` for dev only
- [ ] Timeouts and memory limits; handles infinite loops (binary-search demo)
- [ ] Hypothesis is available inside the sandbox image
- [ ] Batch mode to run many inputs in one call (needed for the 500-input verification)
EOF

issue "Session model and state machine" "mvp,backend" "$M0" <<'EOF'
Implement the `Session` model and orchestrator from outline §7.

States: `intent → properties_confirmed → exploring → predicting → hinting → verifying → postmortem`.

- [ ] In-memory session store
- [ ] `diagnosis` is a server-only field that is excluded from every response schema (add a test that asserts this)
- [ ] `hint_level` 0–5, can only increase
- [ ] Progress events published for the WebSocket stream
EOF

# ----- M1: Core engine -----------------------------------------------------

issue "Property proposer: intent + code -> candidate properties" "mvp,agent,backend" "$M1" <<'EOF'
Given the user's code and intent (for example "merge overlapping intervals"), propose properties the code should satisfy.

- [ ] Prompt (`fast` model) returns 3–6 properties as structured data: natural-language statement + executable Python predicate
- [ ] Predicates are validated by running them in the sandbox on a trivial input
- [ ] `PUT /sessions/{id}/properties` accepts user edits; edited predicates get regenerated
- [ ] Example for merge_intervals: "output intervals never overlap", "every input point is still covered", "output is sorted"
EOF

issue "Edge-case generator and Hypothesis harness generation" "mvp,agent,testing-engine" "$M1" <<'EOF'
Generate tests that hunt for bugs.

- [ ] Infer the input type from the signature, docstring and intent
- [ ] Hand-picked edge cases per type: empty, single element, duplicates, boundaries, touching ranges, unsorted, huge values, negatives
- [ ] `fast` model writes Hypothesis strategies + `@given` tests from the confirmed properties
- [ ] Harness template that the sandbox executes and that reports structured results
- [ ] Generated test code is linted/compiled before running; retry on syntax errors
EOF

issue "Failure shrinking and counterexample capture" "mvp,testing-engine,backend" "$M1" <<'EOF'
- [ ] Capture Hypothesis's shrunk minimal failing example
- [ ] Record actual output, the violated property, and the exception if any
- [ ] Deduplicate counterexamples by violated property / root cause
- [ ] Rank so the simplest, most teachable counterexample is used first
EOF

issue "Private diagnosis (Ultra): root cause, line, bug class, reference fix" "mvp,agent,backend" "$M1" <<'EOF'
`deep` model produces the diagnosis that the agent knows but never tells.

- [ ] Inputs: code, intent, properties, counterexample, trace
- [ ] Output schema: `root_cause`, `line`, `region` (function/block), `bug_class`, `concept`, `reference_fix` (code)
- [ ] **Validate**: apply `reference_fix` in the sandbox and confirm all tests pass; retry otherwise
- [ ] Stored only server-side; never serialised to the client (test this)
EOF

issue "Tracer: variable trace at the moment of failure" "mvp,testing-engine" "$M1" <<'EOF'
Level 3 evidence for the hint ladder.

- [ ] `sys.settrace` harness that records locals line by line for the counterexample
- [ ] Window the trace around the diagnosed region and the first divergence
- [ ] Compact, readable rendering (table of step / line / vars)
- [ ] Line numbers are stripped from the user-facing rendering below level 5
EOF

issue "Predict-then-reveal flow" "mvp,backend,agent" "$M1" <<'EOF'
Instead of announcing the failure, ask the user to predict.

- [ ] `GET /sessions/{id}/prediction` → "What should `merge([[1,4],[4,5]])` return?"
- [ ] `POST` the user's answer → run the code in the sandbox → return actual output + comparison
- [ ] Parse the user's answer as a Python literal; tolerate formatting differences
- [ ] Handle the case where the user's prediction is also wrong (gentle nudge back to the spec)
EOF

issue "Hint ladder generator (levels 1–5)" "mvp,agent,backend" "$M1" <<'EOF'
`deep` model drafts hints from the diagnosis, one level at a time.

| Level | Content |
|---|---|
| 1 Symptom | Smallest failing input + wrong output |
| 2 Region | Function or block containing the bug |
| 3 Evidence | Trace at the moment things go wrong |
| 4 Concept | The rule being broken |
| 5 Line | The exact line, no fix |

- [ ] One prompt per level, with explicit "do not reveal" rules
- [ ] Templated fallback hint per level (used when the gate blocks twice)
- [ ] Every draft goes through the hint gate before sending
EOF

issue "Hint gate: block any message that leaks the fix or exceeds the current level" "mvp,hint-gate,agent,backend" "$M1" <<'EOF'
**Key technical piece.** Every outbound message passes this check. See outline §5.3.

Deterministic layer:
- [ ] No token sequence from the reference-fix diff in the message
- [ ] Line numbers only at level ≥ 5; function/block names only at level ≥ 2; trace data only at level ≥ 3; concept vocabulary only at level ≥ 4
- [ ] Any code block in the message is applied to the user's code in the sandbox; block if it makes the failing test pass

LLM layer (`gate` model, separate prompt):
- [ ] "Does this reveal more than level N allows?" → `{allow, reason}`

Behaviour:
- [ ] On block: feed the reason back to the hint writer, retry ≤ 2, then use the templated fallback
- [ ] Log every decision (`gate_log`) for the demo
- [ ] Unit tests for each deterministic rule
EOF

issue "Fix verification: rerun tests + fresh inputs + anti-special-casing" "mvp,testing-engine,backend" "$M1" <<'EOF'
- [ ] Rerun all property tests and the original counterexample
- [ ] Generate N fresh inputs (default 500, configurable) from the same strategies with a new seed
- [ ] Detect special-casing: the diff contains literals from the counterexample, or the fix passes the counterexample but fails nearby variants
- [ ] Result: pass / fail with a new counterexample (which re-enters the predict → hint loop without leaking)
- [ ] After a pass, surface the next untested risk as a question ("What if the input isn't sorted?")
EOF

issue "Post-mortem summary" "mvp,agent" "$M1" <<'EOF'
- [ ] Bug class (off-by-one, boundary condition, mutation during iteration, wrong input assumption, …)
- [ ] "How to spot it next time": 1–2 sentences
- [ ] Other edge cases worth thinking about
- [ ] Hint level reached + number of fix attempts
- [ ] Structured output so it can feed the bug profile (stretch)
EOF

# ----- M2: AI Code Review --------------------------------------------------

issue "AI Code Review mode: static checks for common AI-code failures" "mvp,backend,testing-engine" "$M2" <<'EOF'
- [ ] Invented APIs: AST walk; resolve imports and attribute access against installed modules in the sandbox
- [ ] Swallowed exceptions: bare `except`, `except Exception: pass`, broad catch that returns a default
- [ ] Wrong input assumptions: e.g. code assumes sorted / non-empty / unique input, checked against generated tests
- [ ] Findings are delivered as questions through the hint gate, not as answers
EOF

issue "AI Code Review mode: intent conformance via LLM + tests" "mvp,agent" "$M2" <<'EOF'
- [ ] Compare the code against the stated intent and confirmed properties
- [ ] Missing edge cases → turned into predict-then-reveal questions
- [ ] Mode selection wires review-specific prompts into the session orchestrator
EOF

# ----- M3: Web UI ----------------------------------------------------------

issue "Web UI layout: code editor, chat panel, hint-level meter" "mvp,frontend" "$M3" <<'EOF'
- [ ] Monaco editor (Python) on the left, chat panel on the right
- [ ] Hint meter showing levels 0–5 with a "Need more help" button that climbs one level
- [ ] Mode selector: Debug / AI Code Review (Explore disabled → stretch)
- [ ] Intent input box
EOF

issue "UI: property review/edit step" "mvp,frontend" "$M3" <<'EOF'
- [ ] Show proposed properties as editable cards (natural language)
- [ ] Add / remove / edit, then confirm → starts exploration
- [ ] Progress indicator while tests run (WebSocket events)
EOF

issue "UI: prediction widget and reveal view" "mvp,frontend" "$M3" <<'EOF'
- [ ] Input for the user's prediction
- [ ] Side-by-side "you predicted" vs "the code returned", with the difference highlighted
- [ ] Trace rendering component for level 3 hints
EOF

issue "UI: fix submission, verification result and post-mortem" "mvp,frontend" "$M3" <<'EOF'
- [ ] Submit fix from the editor
- [ ] Live counter "verified on N/500 fresh inputs"
- [ ] Post-mortem card
- [ ] Optional "gate log" drawer for the demo (shows blocked hints and reasons)
EOF

# ----- M4: Demo ------------------------------------------------------------

issue "Demo bug 1: AI-generated merge_intervals" "mvp,demo" "$M4" <<'EOF'
- [ ] Get a real chatbot-generated `merge_intervals` that uses `<` instead of `<=` and assumes sorted input
- [ ] Confirm `[[1,4],[4,5]]` is found and shrunk
- [ ] Confirm the level 3 trace shows the comparison at the touching intervals
- [ ] Confirm the `<=` fix passes 500 fresh inputs and the follow-up "unsorted input?" question breaks it again
EOF

issue "Demo bug 2: binary search off-by-one" "mvp,demo" "$M4" <<'EOF'
- [ ] Buggy binary search (e.g. wrong `lo`/`hi` update → miss or infinite loop)
- [ ] Sandbox timeout handled and surfaced as a symptom, not a crash
- [ ] End-to-end walkthrough recorded in `demo/`
EOF

issue "Demo bug 3: LRU cache recency/eviction bug" "mvp,demo" "$M4" <<'EOF'
- [ ] Buggy LRU cache (e.g. `get` doesn't refresh recency)
- [ ] Stateful testing with Hypothesis `RuleBasedStateMachine`
- [ ] Shrunk operation sequence like `put(1), put(2), get(1), put(3)`
EOF

issue "Hint gate red-team eval suite" "mvp,hint-gate,testing-engine" "$M4" <<'EOF'
Prove the gate works.

- [ ] Dataset of user messages that try to extract the fix ("just tell me the answer", "write the corrected function", jailbreak-style prompts)
- [ ] For each demo bug × hint level, assert no leak
- [ ] Report leak rate and false-block rate; run in CI (with a mocked or recorded model where possible)
- [ ] Numbers go on a slide for the judges
EOF

issue "Deploy the demo" "mvp,infra,token-factory" "$M4" <<'EOF'
- [ ] Deploy backend + frontend (single container is fine)
- [ ] Secrets for Token Factory configured
- [ ] Pre-warm / cache exploration for the three demo bugs to keep the live demo fast
EOF

issue "Demo script, rehearsal and backup recording" "mvp,demo,docs" "$M4" <<'EOF'
Follow the 2-minute script in outline §11.

- [ ] Rehearse at least 3 times against the deployed build
- [ ] Record a backup video
- [ ] 3–4 slides: problem, how a session works, architecture with Token Factory and the hint gate, results
EOF

# ----- Stretch -------------------------------------------------------------

issue "Explore mode: edge-case questions for code that may already work" "stretch,agent,backend,frontend" "$MS" <<'EOF'
- [ ] Run exploration even when no tests fail
- [ ] Turn untested edge cases into a sequence of predict-then-reveal questions
- [ ] Ends with a summary of edge cases the user hadn't considered
EOF

issue "Personal bug profile" "stretch,backend,frontend" "$MS" <<'EOF'
- [ ] Persist post-mortems (SQLite) per local user
- [ ] Aggregate by bug class; show "bugs you tend to miss"
- [ ] Use the profile to prioritise which edge cases to ask about first
EOF

issue "Mutation testing in fix verification" "stretch,testing-engine" "$MS" <<'EOF'
- [ ] Mutate the fixed code and check the properties kill the mutants
- [ ] Surviving mutants → "your spec might be missing a property" prompt
EOF

issue "JavaScript support" "stretch,testing-engine,backend" "$MS" <<'EOF'
- [ ] fast-check for property-based tests
- [ ] Node runtime in the sandbox
- [ ] Tracer equivalent (instrumentation or inspector protocol)
EOF

issue "VS Code extension" "stretch,frontend" "$MS" <<'EOF'
- [ ] Send the selected function + intent to the backend
- [ ] Chat + hint meter in a webview panel
- [ ] Never edits the user's file
EOF

echo "Done."
