"""The hint gate: every outbound message passes this check (docs/PROJECT_OUTLINE.md §5.3).

Three layers, cheapest first; the first one to object blocks the message:
1. Deterministic rules on the text (fix tokens, line numbers, structure, trace
   data and concept vocabulary, each allowed only from a given hint level).
2. Code in the message is spliced into the user's code and run against the
   counterexamples in the sandbox; if any variant fixes one, it is blocked.
3. The gate model judges "does this reveal more than level N allows?".
Every decision is appended to session.gate_log.
"""

import ast
import contextlib
import difflib
import re
import textwrap
import time
from dataclasses import asdict, dataclass

from pydantic import BaseModel

from sparring_partner.agents.code_info import defined_names, find_classes, find_functions
from sparring_partner.agents.diagnosis import counterexample_args
from sparring_partner.agents.testgen import JsonLLM
from sparring_partner.llm import Message
from sparring_partner.sandbox import Sandbox
from sparring_partner.session import Session

# What each hint level adds; every level also allows everything below it.
LEVEL_ALLOWS = {
    0: "the failing input, what the code returned, and the user's own spec (their properties)",
    1: "the symptom: the smallest failing input, the wrong output and which property it breaks",
    2: "the region: which function or block contains the bug (no line numbers)",
    3: "the evidence: variable values at the moment things go wrong (a trace)",
    4: "the concept: the general rule being broken, in words",
    5: "the line: the exact line number of the bug, quoting that line as it is now",
}
NEVER = (
    "the fix itself: corrected code, the replacement operator or expression, or "
    "instructions that amount to the fix"
)

# Review and risk questions exist to point at risks; they still must not answer them.
REVIEW_KINDS = {"review", "risk"}
REVIEW_RULES = (
    "This is a code-review question. Its job is to point the learner at a kind of input or "
    "risk to think about, e.g. 'What if the input isn't sorted?' or 'What happens to an error "
    "inside that try?': that is allowed even though it hints where the problem is. It must "
    "NOT say whether the code handles it, explain why it fails, or say how to fix it, and it "
    f"must never contain {NEVER}."
)

TOKEN = re.compile(r"\w+|<=|>=|==|!=|\+=|-=|\*=|//|\*\*|->|[^\s\w]")
MULTI_CHAR_OPERATORS = {"<=", ">=", "==", "!=", "+=", "-=", "*=", "//", "**"}
LINE_NUMBER = re.compile(
    r"\blines?\s*(?:no\.?|number|#)?\s*\d+|\b\d+(?:st|nd|rd|th)\s+line\b|\bL\d+\b", re.I
)
STRUCTURE = re.compile(
    r"\b(?:for|while)[- ]loops?\b|\bloops?\b|\bif[- ]statements?\b|\bbranch(?:es)?\b"
    r"|\bcode blocks?\b|\bcomparisons?\b",
    re.I,
)
# How a comparison goes wrong is concept-level: "less than", "strict", "equal to"...
OPERATOR_WORDS = re.compile(
    r"\b(?:less|greater|smaller|bigger|larger)\s+than\b|\bstrict(?:ly)?\b|\binequalit(?:y|ies)\b"
    r"|\bequal\s+to\b|\bare\s+equal\b|\bat\s+(?:least|most)\b|\boff[- ]by[- ]one\b",
    re.I,
)
TRACE_TABLE = re.compile(r"\|\s*step\s*\|", re.I)
STOPWORDS = {
    "that", "this", "with", "when", "then", "than", "from", "into", "their", "there", "they",
    "have", "must", "should", "would", "could", "every", "each", "only", "also", "which",
    "where", "what", "because", "while", "does", "both", "same", "other", "more", "less",
    "secret", "being", "rule", "value", "values", "input", "output", "code", "function",
    "wrong", "right", "correct", "incorrect", "error", "problem", "issue", "case", "cases",
    "list", "lists", "result", "results", "number", "numbers", "item", "items", "element",
    "elements", "goes", "here", "check", "expect", "expected",
}  # fmt: skip
CODE_SPAN = re.compile(r"```(?:\w+)?\n?(.*?)```|`([^`\n]+)`", re.DOTALL)
CODE_CHARS = set("=<>()[]+-*/%:")
MAX_VARIANTS = 120


@dataclass
class GateDecision:
    allowed: bool
    layer: str  # "rules", "sandbox", "llm", or "pass"
    rule: str | None = None
    reason: str = ""


class GateVerdict(BaseModel):
    allow: bool
    reason: str


# Deterministic rules (pure, one per check)
def tokens(text: str) -> list[str]:
    return TOKEN.findall(text)


def fix_sequences(original: str, fix: str) -> set[tuple[str, ...]]:
    """Token sequences that only the fix contains: they would give it away.

    From the lines the fix adds or changes: new multi-character operators
    (`<=`), new calls (`sorted (`), and new 3-token runs that include a symbol
    (`start <= merged`). Plain-word runs are left out; they read as English.
    """
    original_tokens = tokens(original)
    known = {
        tuple(original_tokens[i : i + n]) for n in (1, 2, 3) for i in range(len(original_tokens))
    }
    added = [
        line[2:]
        for line in difflib.ndiff(original.splitlines(), fix.splitlines())
        if line.startswith("+ ")
    ]
    sequences: set[tuple[str, ...]] = set()
    for line in added:
        toks = tokens(line)
        for tok in toks:
            if tok in MULTI_CHAR_OPERATORS and (tok,) not in known:
                sequences.add((tok,))
        for a, b in zip(toks, toks[1:], strict=False):
            if a.isidentifier() and b == "(" and (a, b) not in known:
                sequences.add((a, b))
        for i in range(len(toks) - 2):
            run = tuple(toks[i : i + 3])
            if (
                run not in known
                and any(not t.isidentifier() for t in run)
                and any(t.isidentifier() for t in run)
            ):
                sequences.add(run)
    return sequences


def leaks_fix(text: str, sequences: set[tuple[str, ...]]) -> str | None:
    toks = tokens(text)
    for n in (1, 2, 3):
        for i in range(len(toks) - n + 1):
            if tuple(toks[i : i + n]) in sequences:
                return " ".join(toks[i : i + n])
    return None


def mentions_line_number(text: str) -> str | None:
    match = LINE_NUMBER.search(text)
    return match.group(0) if match else None


def mentions_structure(text: str, helpers: set[str], variables: set[str]) -> str | None:
    """Words that point into the code: structure ("the loop"), helper functions, or
    local variables used as code (`merged`, merged[...], merged = ...)."""
    if match := STRUCTURE.search(text):
        return match.group(0)
    for name in helpers:
        if re.search(rf"\b{re.escape(name)}\b", text):
            return name
    for var in variables:  # often English words ("start", "end"): only code-like uses
        v = re.escape(var)
        if match := re.search(rf"`[^`]*\b{v}\b[^`]*`|\b{v}\s*(?:\[|\.\w|=)", text):
            return match.group(0)
    return None


def mentions_trace(text: str, variables: set[str]) -> str | None:
    """Values of the function's variables (`merged = [[0, 0]]`) or a trace table."""
    if match := TRACE_TABLE.search(text):
        return match.group(0)
    for var in variables:
        pattern = (
            rf"\b{re.escape(var)}\s*=\s*[^=\s]"
            rf"|\b{re.escape(var)}\s+(?:is|was|holds|contains|equals|becomes)\s+[\[\(\{{\d'\"-]"
        )
        if match := re.search(pattern, text):
            return match.group(0)
    return None


def concept_terms(concept: str, bug_class: str, user_vocabulary: str) -> set[str]:
    """Distinctive words of the concept, minus words the user already uses."""
    known = {_stem(w) for w in re.findall(r"[a-zA-Z]+", user_vocabulary)}
    words = re.findall(r"[a-zA-Z]+", f"{concept} {bug_class}")
    return {_stem(w) for w in words if len(w) >= 4 and w.lower() not in STOPWORDS} - known


def mentions_concept(text: str, terms: set[str]) -> str | None:
    """The diagnosis's concept words, or the language of a comparison going wrong."""
    if match := OPERATOR_WORDS.search(text):
        return match.group(0)
    for word in re.findall(r"[a-zA-Z]+", text):
        if len(word) >= 4 and _stem(word) in terms:
            return word
    return None


SUFFIXES = ("ings", "ing", "edly", "ed", "es", "s", "ly", "ion", "ions")


def _stem(word: str) -> str:
    """Crude stem so inflections match: merging/merged/merge -> "merg"."""
    word = word.lower()
    for suffix in SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            break
    return word.removesuffix("e")[:6]


def code_snippets(text: str) -> list[str]:
    """Fenced blocks and inline spans that look like code.

    A plain value (`-1`, `[[1, 5]]`) is not: it can't carry a fix, and splicing it in
    over a line only deletes that line, which can make a failing case pass by accident.
    """
    snippets = []
    for fenced, inline in CODE_SPAN.findall(text):
        snippet = (fenced or inline).strip("\n")
        if snippet.strip() and (fenced or CODE_CHARS & set(inline)) and not _is_value(snippet):
            snippets.append(snippet)
    return snippets


def _is_value(snippet: str) -> bool:
    try:
        ast.literal_eval(snippet.strip())
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return False
    return True


def splice_variants(original: str, snippet: str, function: str) -> list[str]:
    """Ways the snippet could be applied to the code: as the whole file, or
    replacing / inserted before each run of lines (re-indented to fit)."""
    snippet = textwrap.dedent(snippet).strip("\n")
    try:
        if function in defined_names(snippet):
            return [snippet]
    except SyntaxError:
        pass
    lines = original.splitlines()
    piece = snippet.splitlines()
    variants: list[str] = []
    seen = {_normalized(original)}
    for i in range(len(lines)):
        indent = re.match(r"\s*", lines[i]).group(0)  # type: ignore[union-attr]
        placed = [indent + p for p in piece]
        for candidate in (
            lines[:i] + placed + lines[i + len(piece) :],  # replace
            lines[:i] + placed + lines[i:],  # insert before
        ):
            source = "\n".join(candidate) + "\n"
            key = _normalized(source)
            if key is not None and key not in seen:
                seen.add(key)
                variants.append(source)
    return variants[:MAX_VARIANTS]


def _normalized(source: str) -> str | None:
    try:
        return ast.dump(ast.parse(source))
    except SyntaxError:
        return None


# The gate
class HintGate:
    def __init__(self, llm: JsonLLM, sandbox: Sandbox, *, call_timeout_s: float = 1.0):
        self.llm = llm
        self.sandbox = sandbox
        self.call_timeout_s = call_timeout_s

    async def check(
        self, session: Session, text: str, level: int, *, kind: str, use_llm: bool = True
    ) -> GateDecision:
        """Is `text` safe to send at hint `level`? Logged in session.gate_log either way.

        `use_llm=False` skips the model layer: for fixed templates, whose wording
        can't drift, and for the last-resort fallback hint.
        """
        decision = (
            self.check_rules(session, text, level)
            or await self._check_code(session, text)
            or (await self._check_llm(session, text, level, kind) if use_llm else None)
            or GateDecision(True, "pass")
        )
        session.gate_log.append(
            {"at": time.time(), "kind": kind, "level": level, "text": text, **asdict(decision)}
        )
        return decision

    def check_rules(self, session: Session, text: str, level: int) -> GateDecision | None:
        diagnosis = session.diagnosis
        if diagnosis is not None and (leak := leaks_fix(text, _fix_sequences(session))):
            return GateDecision(False, "rules", "fix_tokens", f"contains fix code: {leak!r}")
        if level < 5 and (found := mentions_line_number(text)):
            return GateDecision(
                False, "rules", "line_number", f"line number before level 5: {found!r}"
            )
        if level < 2 and (
            found := mentions_structure(text, _helpers(session), _variables(session))
        ):
            return GateDecision(
                False, "rules", "structure", f"points into the code before level 2: {found!r}"
            )
        if level < 3 and (found := mentions_trace(text, _variables(session))):
            return GateDecision(False, "rules", "trace", f"trace data before level 3: {found!r}")
        if level < 4 and diagnosis is not None:
            terms = concept_terms(diagnosis.concept, diagnosis.bug_class, _user_vocabulary(session))
            if found := mentions_concept(text, terms):
                return GateDecision(
                    False, "rules", "concept", f"concept vocabulary before level 4: {found!r}"
                )
        return None

    async def _check_code(self, session: Session, text: str) -> GateDecision | None:
        safe = _safe_snippets(session)
        variants: list[str] = []
        for snippet in code_snippets(text):
            if snippet.strip() not in safe:
                variants += splice_variants(session.code, snippet, session.function)
        if not variants or not session.counterexamples:
            return None
        config = {
            "function": session.function,
            "variants": variants[:MAX_VARIANTS],
            "cases": [counterexample_args(ce) for ce in session.counterexamples],
            "predicates": {p.statement: p.predicate for p in session.properties},
            "call_timeout_s": self.call_timeout_s,
        }
        harness = (
            "from sparring_partner.testing.harness import fixes_failure\n\n"
            f"CONFIG = {config!r}\n\n\n"
            "def run(solution, inputs):\n"
            "    return fixes_failure(solution, CONFIG)\n"
        )
        result = await self.sandbox.run("", harness)
        if not result.ok:
            # Can't tell whether the code fixes anything: err on the side of the learner.
            return GateDecision(False, "sandbox", "code_unchecked", "couldn't check the code in it")
        if result.value["fixed"]:
            return GateDecision(
                False, "sandbox", "code_fixes", "its code makes a failing test pass"
            )
        return None

    async def _check_llm(
        self, session: Session, text: str, level: int, kind: str = "hint"
    ) -> GateDecision | None:
        d = session.diagnosis
        secret = (
            f"Root cause: {d.root_cause}\nBug line: {d.line}\nRegion: {d.region}\n"
            f"Concept: {d.concept}\nReference fix:\n{d.reference_fix}"
            if d
            else "(no diagnosis)"
        )
        if kind in REVIEW_KINDS:
            task = (
                f"{REVIEW_RULES}\n\nQuestion to the learner:\n<<<\n{text}\n>>>\n\n"
                "Does this question answer itself (say whether the code handles the case, why "
                "it fails, or how to fix it)? allow=false only if it does, with a "
                "one-sentence reason."
            )
        else:
            allows = "\n".join(f"- level {n}: {LEVEL_ALLOWS[n]}" for n in range(level + 1))
            not_yet = "\n".join(
                f"- level {n}: {LEVEL_ALLOWS[n]}" for n in range(level + 1, len(LEVEL_ALLOWS))
            )
            task = (
                f"Current hint level: {level}. Allowed so far:\n{allows}\n"
                f"NOT allowed yet:\n{not_yet or '- (nothing: this is the last level)'}\n"
                f"Never allowed at any level: {NEVER}.\n"
                "Hinting counts as revealing: asking about the exact case the concept is about "
                "(e.g. 'what happens when they are equal?') reveals the concept.\n\n"
                f"Message to the learner:\n<<<\n{text}\n>>>\n\n"
                f"Does this message reveal more than level {level} allows, or give away the "
                "fix? allow=false if it does, with a one-sentence reason naming what leaks."
            )
        messages: list[Message] = [
            {
                "role": "system",
                "content": "You are a strict reviewer guarding a debugging tutor. The tutor "
                "knows the bug and its fix but must let the learner find them. "
                "Reply with JSON only.",
            },
            {
                "role": "user",
                "content": f"The secret (never to be given away):\n{secret}\n\n{task}",
            },
        ]
        verdict = await self.llm.complete_json("gate", messages, GateVerdict)
        if not verdict.allow:
            return GateDecision(False, "llm", "llm_judge", verdict.reason)
        return None


def _fix_sequences(session: Session) -> set[tuple[str, ...]]:
    assert session.diagnosis is not None
    return fix_sequences(session.code, session.diagnosis.reference_fix)


def _helpers(session: Session) -> set[str]:
    """Functions in the code other than the one under test."""
    return {fn.name for fn in find_functions(session.code)} - {session.function}


def _variables(session: Session) -> set[str]:
    """Local variables of the traced function, excluding its parameters (the input).
    For a class, every method's parameters are input: `get(key)` names `key` itself."""
    params = {p.name for p in session.params}
    if session.stateful:
        with contextlib.suppress(SyntaxError):
            for cls in find_classes(session.code):
                if cls.name == session.function:
                    params |= {p.name for m in (cls.init, *cls.methods) for p in m.params}
    if not session.trace:
        return set()
    return {k for step in session.trace["steps"] for k in step["locals"]} - params


def _user_vocabulary(session: Session) -> str:
    """Words the user already owns: the intent, the spec and the code."""
    return " ".join([session.intent, session.code, *(p.statement for p in session.properties)])


def _safe_snippets(session: Session) -> set[str]:
    """Snippets that can't fix anything: the failing calls, outputs and original lines."""
    safe = {line.strip() for line in session.code.splitlines()}
    for ce in session.counterexamples:
        safe |= {ce["call"], ce["output"] or "", *ce["args"]}
    return safe
