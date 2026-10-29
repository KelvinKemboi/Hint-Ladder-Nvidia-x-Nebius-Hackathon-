# Sparring Partner — Project Outline

> A debugging coach that knows the answer but won't tell you.

**Repo:** Hint-Ladder · **Track:** Coding and Agentic Engineering — *build coding agents and developer tools: agents that write, run, and test code in Token Factory* · **Platform:** Nebius Token Factory (inference + sandboxed code execution)

---

## 1. Pitch

AI tools now write much of our code, and developers accept it without fully understanding it. Sparring Partner takes your code (or AI-generated code), quietly finds the bugs and risky edge cases, then coaches you to discover and fix them yourself. You come out with working code and a better instinct for where bugs hide.

## 2. Problem and goal

| | |
|---|---|
| **Problem** | Developers paste AI-generated code that passes the obvious test and ship it. Autocomplete-style tools fix bugs *for* you, so nothing is learned. |
| **Goal** | An agent that writes, runs, and tests code to find the bug, then holds the answer back and guides the user to it step by step. |
| **Who it's for** | Students, junior developers, and anyone reviewing AI-written code. |
| **Success in the demo** | A user finds and fixes a real bug in AI-written `merge_intervals`, and the fix is verified on 500 fresh inputs, without the agent ever showing the fix. |

## 3. How a session works

1. **State intent.** The user pastes code and says what it should do (for example "merge overlapping intervals"). The agent proposes properties the code should satisfy ("output intervals never overlap", "every input point is still covered"). The user confirms or edits them. Writing the spec is part of the learning.
2. **Explore in secret.** The agent generates edge-case inputs (empty, single element, duplicates, boundaries, touching ranges, unsorted, huge values), runs property-based tests in the sandbox, and shrinks any failure to the smallest input. It writes a **private diagnosis**: root cause, line, and reference fix. The user never sees it.
3. **Predict, then reveal.** The agent asks: "What should `merge([[1,4],[4,5]])` return?" The user answers, then the agent runs the code and shows the actual output. The gap between prediction and output is where the learning happens.
4. **Hint ladder.** If the user is stuck, they ask for help one step at a time:

   | Level | Name | What the user gets |
   |---|---|---|
   | 1 | Symptom | Smallest failing input and the wrong output |
   | 2 | Region | Which function or block contains the bug |
   | 3 | Evidence | A trace of variable values at the moment things go wrong |
   | 4 | Concept | The rule being broken ("touching intervals count as overlapping, so check your comparison operator") |
   | 5 | Line | The exact line. Still no fix. |

5. **Verify the fix.** The user submits a fix. The agent reruns every test, then generates fresh inputs of the same type to confirm the user fixed the cause and didn't special-case the one failing input.
6. **Post-mortem.** A short summary: bug class (off-by-one, boundary condition, mutation during iteration), how to spot it next time, and which other edge cases are worth thinking about. These build into a personal profile of the bugs the user tends to miss.

## 4. Modes

| Mode | When | Scope |
|---|---|---|
| **Debug** | The user knows something is broken. The agent leads them to the bug with the hint ladder. | MVP |
| **AI Code Review** | The user pastes Copilot/ChatGPT output. The agent checks it against the stated intent and looks for common AI-code failures: invented APIs, missing edge cases, wrong input assumptions, silently swallowed exceptions. | MVP |
| **Explore** | The code may work. The agent walks the user through edge cases they haven't considered, as questions. | Stretch |

## 5. Architecture

```
┌──────────────────────── Web UI ────────────────────────┐
│  Code editor  │  Chat panel  │  Hint-level meter (0–5) │
└───────────────────────────┬────────────────────────────┘
                            │ REST / WebSocket
┌───────────────────────────▼────────────────────────────┐
│                  Session orchestrator                  │
│  state machine: intent → explore → predict → hints     │
│                 → verify → post-mortem                 │
│                                                        │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │ Fast loop    │  │ Deep analysis│  │ Private      │  │
│  │ (Nano/Super) │  │ (Ultra)      │  │ diagnosis    │  │
│  │ inputs, test │  │ root cause,  │  │ store        │  │
│  │ harnesses    │  │ hint drafts  │  │ (server only)│  │
│  └──────┬───────┘  └──────┬───────┘  └──────────────┘  │
│         │                 │                            │
│         │          ┌──────▼───────┐                    │
│         │          │  HINT GATE   │ every outbound     │
│         │          │  leak check  │ message passes here│
│         │          └──────┬───────┘                    │
└─────────┼─────────────────┼────────────────────────────┘
          │                 │
┌─────────▼─────────────────▼────────────────────────────┐
│               Nebius Token Factory                     │
│  • Model inference (OpenAI-compatible API)             │
│  • Sandbox: runs Hypothesis tests, mutation tests,     │
│    tracing harnesses against the user's code           │
└────────────────────────────────────────────────────────┘
```

### 5.1 Components

| Component | Responsibility |
|---|---|
| **Session orchestrator** | Owns session state and the step state machine. Decides which model and tool run next. |
| **Token Factory client** | Thin wrapper over Token Factory's OpenAI-compatible API. Routes calls by role (`fast`, `deep`, `gate`) to models set in config. |
| **Sandbox runner** | Sends code plus a generated harness to the Token Factory sandbox and returns stdout, exceptions, results, and traces. Enforces timeouts and memory limits. A local subprocess fallback is used for development only. |
| **Property proposer** | Turns intent plus code into candidate properties, which the user edits. |
| **Test generator** | Writes Hypothesis strategies and property tests, plus a hand-picked edge-case list per input type. |
| **Shrinker / counterexample store** | Captures Hypothesis's minimal failing example and the actual vs. expected output. |
| **Diagnoser** | Ultra model. Produces the private diagnosis (root cause, line, bug class, reference fix). It is never serialised to the client. |
| **Tracer** | `sys.settrace` harness that records variable values on the failing input around the suspect line (level 3 evidence). |
| **Hint writer** | Ultra model. Drafts the hint for the requested level using the diagnosis. |
| **Hint gate** | Separate check on **every** outbound message (see 5.3). |
| **Fix verifier** | Reruns all tests, then runs N≥500 fresh generated inputs. Flags fixes that special-case the counterexample. |
| **AI review checks** | Static checks (AST) for swallowed exceptions and unresolvable imports/attributes, plus LLM checks against the stated intent. |
| **Post-mortem writer** | Summarises the bug class, how to spot it, and related edge cases. Writes to the bug profile (stretch). |

### 5.2 Model split

| Role | Model tier | Used for |
|---|---|---|
| `fast` | Nemotron Nano / Super class | Edge-case inputs, Hypothesis strategies, harness code, the fast test loop |
| `deep` | Nemotron Ultra class | Root-cause analysis, private diagnosis, hint drafting, post-mortem |
| `gate` | Super class (separate call, separate prompt) | Leak judgment for the hint gate |

Model IDs live in config (`.env`), not in code, so they can be swapped to whatever Token Factory currently serves.

### 5.3 The hint gate (key technical piece)

Every message to the user goes through the gate before sending. It blocks any message that contains the fix or reveals more than the current hint level allows. This is what makes it a tutor instead of an autocomplete.

Two layers:

1. **Deterministic checks** (fast, explainable):
   - No code token sequence from the reference fix's diff appears in the message.
   - Line numbers are mentioned only at level ≥ 5. Function/block names only at level ≥ 2.
   - Trace data only at level ≥ 3. Concept vocabulary tied to the diagnosis (for example "operator", "`<=`") only at level ≥ 4.
   - No fenced code block that, applied to the user's code, makes the failing test pass. This is checked by running it in the sandbox.
2. **LLM judge** (`gate` model): given the diagnosis, the current level, and the draft, it answers *"Does this reveal more than level N allows?"* with a reason.

On a block, the hint writer gets the gate's reason and retries (up to 2 times), then falls back to a safe templated hint for that level. All gate decisions are logged so they can be shown to judges.

### 5.4 Fix verification

1. Rerun the original property tests and the shrunk counterexample.
2. Generate fresh inputs (default 500) from the same strategies with a new seed.
3. Anti-special-casing heuristics: the diff mentions literal values from the counterexample; the fix passes the counterexample but fails nearby variants.
4. (Stretch) Mutation testing on the fixed code to check the properties are strong enough.

## 6. Tech stack (proposed)

| Layer | Choice |
|---|---|
| Backend | Python 3.11+, FastAPI, Pydantic |
| Testing engine | Hypothesis, `ast`, `sys.settrace`; mutmut-style mutation testing (stretch) |
| LLM / sandbox | Nebius Token Factory (OpenAI-compatible SDK with `base_url` pointed at Token Factory) |
| Frontend | React + Vite, Monaco editor, plain chat panel, hint meter component |
| State | In-memory per session for MVP. SQLite for the bug profile (stretch) |
| Tooling | `uv`, `ruff`, `pytest`, GitHub Actions |

## 7. Data model (MVP)

```text
Session
  id, mode (debug | review | explore), language = "python"
  code, intent
  properties[]        # confirmed by the user
  counterexamples[]   # minimal failing inputs + actual/expected output
  diagnosis           # PRIVATE: root_cause, line, bug_class, reference_fix
  hint_level          # 0..5
  messages[]          # only gate-approved messages
  gate_log[]          # decisions, reasons, retries
  fix_attempts[]      # code, results, fresh_input_count, verdict
  postmortem
```

## 8. API sketch

| Method | Path | Purpose |
|---|---|---|
| POST | `/sessions` | Create a session (`code`, `intent`, `mode`) → proposed properties |
| PUT | `/sessions/{id}/properties` | User confirms/edits properties → starts secret exploration |
| GET | `/sessions/{id}/prediction` | Next predict-then-reveal question |
| POST | `/sessions/{id}/prediction` | User's prediction → actual output and comparison |
| POST | `/sessions/{id}/hint` | Climb one level → gated hint |
| POST | `/sessions/{id}/fix` | Submit a fix → verification result |
| GET | `/sessions/{id}/postmortem` | Post-mortem summary |
| WS | `/sessions/{id}/stream` | Progress events ("running 200 tests…") |

## 9. Scope

### MVP (weekend)
- Python only, single function or single file.
- Debug mode and AI Code Review mode.
- Hint ladder (levels 1–5) with the hint gate.
- Predict-then-reveal.
- Fix verification on fresh inputs.
- Post-mortem summary.
- Three prepared demo bugs: AI-generated `merge_intervals`, a binary search, an LRU cache.
- Simple web UI: code editor, chat panel, hint-level meter.

### Stretch
- Explore mode.
- Personal bug profile across sessions.
- JavaScript support.
- VS Code extension.
- Mutation testing in fix verification.

### Out of scope
- Multi-file projects, arbitrary dependencies, non-deterministic or I/O-heavy code.
- User accounts and auth (beyond a local profile).

## 10. Demo bugs

| Bug | Code | Bug class | Counterexample | Follow-up question |
|---|---|---|---|---|
| `merge_intervals` (AI-generated) | Uses `<` instead of `<=` when checking overlap, and assumes sorted input | Boundary condition + wrong input assumption | `[[1,4],[4,5]]` → `[[1,4],[4,5]]`, expected `[[1,5]]` | "What happens if the input isn't sorted?" |
| Binary search | `hi = mid` / `lo = mid` loop bounds | Off-by-one / non-termination | Target at last index, or a 2-element list | "What about duplicates?" |
| LRU cache | `get` doesn't refresh recency, or eviction removes the wrong end | State/ordering bug | `put(1), put(2), get(1), put(3)` evicts 1 | "What about capacity 0?" |

## 11. Demo script (~2 minutes)

1. Ask an AI chatbot to write `merge_intervals`. It passes the obvious test.
2. Paste it in. Sparring Partner asks what `[[1,4],[4,5]]` should return. Answer `[[1,5]]`. The code returns `[[1,4],[4,5]]`.
3. Climb to hint level 3. The trace shows the comparison at the moment the touching intervals are checked.
4. Change `<` to `<=`. The agent verifies the fix on 500 fresh inputs, and it passes.
5. The agent asks one more question: "What happens if the input isn't sorted?" The AI's code breaks there too, and the session continues.
6. *(If time allows)* Open the gate log to show a blocked hint that would have leaked the fix.

## 12. Judging talking points

- **Agents that write, run, and test code:** the agent writes Hypothesis property tests, tracing harnesses, and (stretch) mutation tests, and runs them in the Token Factory sandbox.
- **Model split on Token Factory:** a cheap, fast model for the test loop and a strong model for reasoning and pedagogy.
- **Hint gate:** a separate, auditable guardrail on every outbound message, with both deterministic and LLM layers. It is what turns a code fixer into a tutor.
- **Fix verification on fresh inputs:** catches fixes that special-case the one failing input.

## 13. Milestones

| Milestone | Contents |
|---|---|
| **M0 — Foundations** | Repo scaffold, Token Factory client and model routing, sandbox runner, session model |
| **M1 — Core engine** | Properties, test generation, shrinking, diagnosis, tracer, hint ladder, hint gate, predict-then-reveal, fix verification, post-mortem |
| **M2 — AI Code Review** | Static and LLM checks for AI-code failures |
| **M3 — Web UI** | Editor, chat, hint meter, property editor, prediction widget |
| **M4 — Demo** | Three demo bugs, gate eval suite, deployment, rehearsal, backup video |
| **Stretch** | Explore mode, bug profile, JavaScript, VS Code extension, mutation testing |

GitHub issues for all of the above are created by [`scripts/create_github_issues.sh`](../scripts/create_github_issues.sh).

## 14. Risks

| Risk | Mitigation |
|---|---|
| Hint gate too strict (useless hints) or too loose (leaks) | Templated fallback per level. Red-team eval set in CI. Tune per level. |
| Sandbox latency slows the demo | Cache exploration results for the demo bugs. Stream progress events. Keep N configurable. |
| LLM diagnosis is wrong | Ground the diagnosis in the shrunk counterexample and trace. Check the reference fix passes all tests in the sandbox before using it. |
| Untrusted user code | Execute only in the sandbox with timeouts and resource limits. Never `exec` in the API process. |
| Token Factory model availability changes | Model IDs set in config. Role-based routing. |
