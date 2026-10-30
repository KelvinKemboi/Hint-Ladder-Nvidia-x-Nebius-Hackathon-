"""Generate the bug hunt: input kinds, hand-picked edge cases and Hypothesis strategies.

The fast model infers what each parameter is and writes a Hypothesis strategy
for the argument tuple. The tests themselves come from the confirmed properties:
the harness turns each predicate into a shrinking @given search
(testing.capture.find_counterexamples) and also runs the edge cases below.

A class is tested as a state machine instead: its one "argument" is a sequence of
operations (see OPERATIONS), and the strategy draws arguments for each method.
"""

import ast
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from sparring_partner.agents.code_info import ClassInfo, FunctionInfo, Target
from sparring_partner.agents.errors import GenerationError
from sparring_partner.llm import Message, Role, SchemaT
from sparring_partner.sandbox import Sandbox
from sparring_partner.session import ParamSpec, Session

logger = logging.getLogger(__name__)

Kind = Literal[
    "int",
    "float",
    "str",
    "bool",
    "list_int",
    "sorted_list_int",
    "list_str",
    "intervals",
    "sorted_intervals",
    "dict_str_int",
    "other",
]

KIND_GUIDE = """\
- int: a single integer
- float: a single float
- str: a string
- bool: True/False
- list_int: list of integers, any order
- sorted_list_int: list of integers the function expects to be sorted ascending
- list_str: list of strings
- intervals: list of [start, end] integer pairs with start <= end, in any order
- sorted_intervals: intervals the function expects to be sorted by start
- dict_str_int: dict from str to int
- other: anything else"""

HUGE = 10**18

# How a class's operation sequence is shown to the models.
OPERATIONS = """\
ops is a list of operations run on one object: the first, ["{cls}", *constructor_args], \
creates it, and each later one, ["method_name", *args], calls a method on it. output is a \
list of what each operation returned, in order (None for the constructor), so output[i] is \
the result of ops[i]. Example: ops = {example}."""

# Shared by every prompt that has to decide what a correct answer is.
CONVENTIONS = (
    "Unless the intent says otherwise, treat ranges and intervals as closed: both endpoints "
    "are included, so two ranges that share an endpoint overlap."
)

# Hand-picked edge cases per kind: (label, value).
EDGE_CASES: dict[str, list[tuple[str, Any]]] = {
    "int": [("zero", 0), ("one", 1), ("negative", -1), ("huge", HUGE), ("huge negative", -HUGE)],
    "float": [("zero", 0.0), ("negative zero", -0.0), ("negative", -1.5), ("tiny", 1e-300),
              ("huge", 1e300)],
    "str": [("empty", ""), ("single character", "a"), ("duplicates", "aa"), ("whitespace", " "),
            ("unicode", "é😀"), ("long", "ab" * 200)],
    "bool": [("false", False), ("true", True)],
    "list_int": [("empty", []), ("single element", [0]), ("duplicates", [1, 1]),
                 ("unsorted", [3, 1, 2]), ("negatives", [-2, -1, 0]),
                 ("huge values", [HUGE, -HUGE]), ("already sorted", [1, 2, 3])],
    "sorted_list_int": [("empty", []), ("single element", [5]), ("duplicates", [1, 1, 2]),
                        ("two elements", [1, 2]), ("negatives", [-3, -1, 0]),
                        ("huge values", [-HUGE, HUGE])],
    "list_str": [("empty", []), ("single element", ["a"]), ("duplicates", ["a", "a"]),
                 ("unsorted", ["b", "a"]), ("empty string", [""])],
    "intervals": [("empty", []), ("single interval", [[1, 2]]),
                  ("touching ranges", [[1, 4], [4, 5]]), ("overlapping", [[1, 3], [2, 6]]),
                  ("nested", [[1, 10], [2, 3]]), ("unsorted", [[5, 6], [1, 2]]),
                  ("duplicates", [[1, 2], [1, 2]]), ("zero width", [[3, 3]]),
                  ("negatives", [[-5, -1], [-3, 0]]),
                  ("huge values", [[0, HUGE], [HUGE, HUGE + 1]])],
    "sorted_intervals": [("empty", []), ("single interval", [[1, 2]]),
                         ("touching ranges", [[1, 4], [4, 5]]), ("overlapping", [[1, 3], [2, 6]]),
                         ("nested", [[1, 10], [2, 3]]), ("duplicates", [[1, 2], [1, 2]]),
                         ("zero width", [[3, 3]]), ("negatives", [[-5, -1], [-3, 0]]),
                         ("huge values", [[0, HUGE], [HUGE, HUGE + 1]])],
    "dict_str_int": [("empty", {}), ("single key", {"a": 1}), ("negative value", {"a": -1}),
                     ("huge value", {"a": HUGE})],
}  # fmt: skip

# What the other parameters hold while one parameter takes an edge case.
TYPICAL: dict[str, Any] = {
    "int": 3,
    "float": 1.5,
    "str": "abc",
    "bool": True,
    "list_int": [3, 1, 2],
    "sorted_list_int": [1, 3, 5, 7],
    "list_str": ["b", "a"],
    "intervals": [[1, 3], [2, 4]],
    "sorted_intervals": [[1, 3], [2, 4]],
    "dict_str_int": {"a": 1, "b": 2},
}

# Used when the model can't produce a working strategy.
FALLBACK_STRATEGIES: dict[str, str] = {
    "int": "st.integers(-20, 20)",
    "float": "st.floats(-100, 100, allow_nan=False)",
    "str": "st.text(max_size=8)",
    "bool": "st.booleans()",
    "list_int": "st.lists(st.integers(-20, 20), max_size=8)",
    "sorted_list_int": "st.lists(st.integers(-20, 20), max_size=8).map(sorted)",
    "list_str": "st.lists(st.text(max_size=3), max_size=6)",
    "intervals": (
        "st.lists(st.tuples(st.integers(-10, 20), st.integers(0, 6))"
        ".map(lambda p: [p[0], p[0] + p[1]]), max_size=6)"
    ),
    "sorted_intervals": (
        "st.lists(st.tuples(st.integers(-10, 20), st.integers(0, 6))"
        ".map(lambda p: [p[0], p[0] + p[1]]), max_size=6).map(sorted)"
    ),
    "dict_str_int": "st.dictionaries(st.text(max_size=3), st.integers(-20, 20), max_size=5)",
}

ANNOTATION_KINDS = {
    "int": "int",
    "float": "float",
    "str": "str",
    "bool": "bool",
    "list[int]": "list_int",
    "List[int]": "list_int",
    "list[str]": "list_str",
    "List[str]": "list_str",
    "dict[str, int]": "dict_str_int",
    "Dict[str, int]": "dict_str_int",
}

ALLOWED_IMPORTS = {"hypothesis", "math", "string"}
BANNED_CALLS = {"open", "exec", "eval", "compile", "__import__", "input"}


class JsonLLM(Protocol):
    async def complete_json(
        self, role: Role, messages: list[Message], schema: type[SchemaT], **kwargs: Any
    ) -> SchemaT: ...


@dataclass(frozen=True)
class EdgeCase:
    label: str
    args: tuple[Any, ...]


# Input kinds
class ParamDraft(BaseModel):
    name: str
    kind: Kind
    description: str = Field(description="one short phrase, e.g. 'list of [start, end] pairs'")


class WorkedExample(BaseModel):
    args: str = Field(description="argument tuple as a Python literal, e.g. '([[1, 3], [2, 4]],)'")
    output: str = Field(description="what a CORRECT implementation returns, as a Python literal")


class InputSpecDraft(BaseModel):
    params: list[ParamDraft]
    examples: list[WorkedExample] = Field(min_length=1, max_length=4)


@dataclass
class InputInfo:
    params: list[ParamSpec]
    example_args: tuple[Any, ...] | None  # one trivial valid input
    worked_examples: list[tuple[tuple[Any, ...], Any]]  # (args, correct output)


async def infer_inputs(llm: JsonLLM, fn: FunctionInfo, intent: str, code: str) -> InputInfo:
    """Parameter kinds (from signature, docstring and intent) plus worked examples.

    Worked examples are the model's view of what a correct implementation
    returns; they are used to catch predicates that reject correct output.
    """
    names = [p.name for p in fn.params]
    messages: list[Message] = [
        {"role": "system", "content": "You classify Python function inputs. Reply with JSON only."},
        {
            "role": "user",
            "content": (
                f"Intent: {intent}\nSignature: {fn.signature()}\n"
                f"Docstring: {fn.docstring or '(none)'}\n\nCode:\n```python\n{code}\n```\n\n"
                f"For each parameter ({', '.join(names)}), in order, pick the kind that "
                f"describes the inputs the intent implies:\n{KIND_GUIDE}\n\n"
                "Then give 2 to 4 worked examples: small inputs and the output a CORRECT "
                "implementation of the intent returns (not what this code returns; it may be "
                "buggy). Include the smallest or empty input and one boundary case. " + CONVENTIONS
            ),
        },
    ]
    worked: list[tuple[tuple[Any, ...], Any]] = []
    try:
        draft = await llm.complete_json("fast", messages, InputSpecDraft)
        if [p.name for p in draft.params] != names:
            raise GenerationError(f"model named parameters {[p.name for p in draft.params]}")
        params = [ParamSpec(p.name, p.kind, p.description) for p in draft.params]
        for example in draft.examples:
            args = _literal_args(example.args, len(names))
            try:
                output = ast.literal_eval(example.output)
            except (ValueError, SyntaxError):
                continue
            if args is not None:
                worked.append((args, output))
    except Exception as exc:
        logger.warning("input inference fell back to annotations: %s", exc)
        params = [ParamSpec(p.name, _kind_from_annotation(p.annotation, intent)) for p in fn.params]

    example = worked[0][0] if worked else None
    if example is None and all(p.kind in TYPICAL for p in params):
        example = tuple(TYPICAL[p.kind] for p in params)
    return InputInfo(params, example, worked)


MAX_EXAMPLE_CALLS = 4


class OperationExamples(BaseModel):
    examples: list[WorkedExample] = Field(min_length=1, max_length=4)


async def infer_operations(llm: JsonLLM, cls: ClassInfo, intent: str, code: str) -> InputInfo:
    """A class's one parameter (its operation sequence), plus worked examples:
    operation sequences and what a correct class returns for each operation."""
    params = [ParamSpec("ops", "operations", f"calls on one {cls.name} object")]
    messages: list[Message] = [
        {
            "role": "system",
            "content": "You write worked examples for Python classes. Reply with JSON only.",
        },
        {
            "role": "user",
            "content": (
                f"Intent: {intent}\n\n{cls.signature()}\n\nCode:\n```python\n{code}\n```\n\n"
                "Give 2 to 4 worked examples, each a short sequence of operations (at most "
                f"{MAX_EXAMPLE_CALLS} calls after the constructor) and the outputs a CORRECT "
                "implementation of the intent returns (not what this code returns; it may be "
                "buggy). Include one that exercises a limit or boundary. Work out each output "
                "step by step before writing it down.\n"
                + OPERATIONS.format(cls=cls.name, example=operations_example(cls))
                + "\nargs is the one-element tuple (ops,), so write it as "
                "'([[...], [...]],)'. output is the list of results."
            ),
        },
    ]
    worked: list[tuple[tuple[Any, ...], Any]] = []
    try:
        draft = await llm.complete_json("fast", messages, OperationExamples)
        for example in draft.examples:
            args = _literal_args(example.args, 1)
            try:
                output = ast.literal_eval(example.output)
            except (ValueError, SyntaxError):
                continue
            # Longer sequences are where a model's own "correct" outputs go wrong, and a
            # wrong example would get a correct property thrown out.
            if (
                args is not None
                and args_fit(args, params, cls.arities())
                and len(args[0]) - 1 <= MAX_EXAMPLE_CALLS
                and isinstance(output, list)
            ):
                worked.append((args, output))
    except Exception as exc:
        logger.warning("no worked examples for %s: %s", cls.name, exc)
    return InputInfo(params, worked[0][0] if worked else None, worked)


def operations_example(cls: ClassInfo) -> str:
    """A made-up sequence in the right shape, e.g. [["LRUCache", 1], ["get", 1]]."""
    ops = [[cls.name, *[1] * len(cls.init.params)]]
    ops += [[m.name, *[1] * len(m.params)] for m in cls.methods[:3]]
    return repr(ops)


def _kind_from_annotation(annotation: str | None, intent: str) -> str:
    if annotation is None:
        return "other"
    normalized = annotation.replace(" ", "").replace(",", ", ")
    if normalized in ANNOTATION_KINDS:
        kind = ANNOTATION_KINDS[normalized]
        if kind == "list_int" and "sorted" in intent.lower():
            return "sorted_list_int"
        return kind
    if "interval" in intent.lower() and normalized.lower().startswith(
        ("list[list[int]]", "list[tuple[int")
    ):
        return "sorted_intervals" if "sorted" in intent.lower() else "intervals"
    return "other"


# Inputs the code was told it won't get still fit the shape: "what if it isn't sorted?"
RELAXED = {"sorted_list_int": "list_int", "sorted_intervals": "intervals"}


def args_fit(
    args: tuple[Any, ...],
    params: list[ParamSpec],
    methods: dict[str, int] | None = None,
    *,
    strict: bool = True,
) -> bool:
    """Do the arguments have the shape their parameter kinds promise? With
    `strict=False`, a sorted kind accepts unsorted values too. For a class, `methods`
    (arguments per call) checks each operation's method name and argument count."""
    from sparring_partner.testing.harness import KIND_CHECKS

    if len(args) != len(params):
        return False
    for arg, p in zip(args, params, strict=True):
        kind = p.kind if strict else RELAXED.get(p.kind, p.kind)
        if not KIND_CHECKS.get(kind, lambda _: True)(arg):
            return False
    if methods and params and params[0].kind == "operations":
        first, *calls = args[0]
        return len(first) - 1 == methods.get("__init__") and all(
            op[0] != "__init__" and methods.get(op[0]) == len(op) - 1 for op in calls
        )
    return True


def _literal_args(text: str, arity: int) -> tuple[Any, ...] | None:
    try:
        value = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return None
    if arity == 1 and not (isinstance(value, tuple) and len(value) == 1):
        value = (value,)
    return value if isinstance(value, tuple) and len(value) == arity else None


# Edge cases
def edge_cases(params: list[ParamSpec], limit: int = 60) -> list[EdgeCase]:
    """Hand-picked inputs: each parameter takes each edge case while the rest stay typical."""
    if not params or any(p.kind not in EDGE_CASES for p in params):
        return []
    typical = tuple(TYPICAL[p.kind] for p in params)
    cases = []
    for i, param in enumerate(params):
        for label, value in EDGE_CASES[param.kind]:
            args = (*typical[:i], value, *typical[i + 1 :])
            cases.append(EdgeCase(f"{param.name}: {label}" if len(params) > 1 else label, args))
    if [p.kind for p in params] == ["sorted_list_int", "int"]:
        cases += _search_cases()

    unique: dict[str, EdgeCase] = {}
    for case in cases:
        unique.setdefault(repr(case.args), case)
    return list(unique.values())[:limit]


def _search_cases() -> list[EdgeCase]:
    """Search-style (sorted list, target): targets at every boundary."""
    cases = []
    for xs in ([1, 3, 5, 7], [5], [1, 1, 2], []):
        targets = {"absent, below all": (xs[0] - 1) if xs else 0}
        if xs:
            targets |= {
                "first element": xs[0],
                "last element": xs[-1],
                "absent, above all": xs[-1] + 1,
                "absent, in a gap": xs[0] + 1 if xs[0] + 1 not in xs else xs[-1] + 2,
            }
        cases += [EdgeCase(f"target {label} of {xs}", (xs, t)) for label, t in targets.items()]
    return cases


# Strategy generation
class StrategyDraft(BaseModel):
    code: str = Field(description="Python source defining strategy()")


def lint_strategy(code: str) -> list[str]:
    """Static problems in model-written strategy code; [] if it may be run."""
    try:
        tree = ast.parse(code)
        compile(code, "<strategy>", "exec")
    except SyntaxError as exc:
        return [f"SyntaxError on line {exc.lineno}: {exc.msg}"]

    errors = []
    defines = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "strategy"]
    if not defines:
        errors.append("must define a top-level function `strategy()`")
    elif defines[0].args.args or defines[0].args.posonlyargs:
        errors.append("`strategy()` must take no arguments")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            modules = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for module in modules:
                if module.split(".")[0] not in ALLOWED_IMPORTS:
                    errors.append(
                        f"import of {module!r} is not allowed (use hypothesis, math, string)"
                    )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in BANNED_CALLS
        ):
            errors.append(f"calling {node.func.id}() is not allowed")
    return errors


def clean_code(code: str) -> str:
    """Model-written code as plain source: no markdown fences, real newlines.

    Models sometimes double-escape code inside JSON, leaving literal "\\n"
    sequences and no real newlines; those are unescaped.
    """
    if fenced := re.search(r"```(?:python)?\s*(.*?)```", code, re.DOTALL):
        code = fenced.group(1)
    code = code.strip()
    if "\n" not in code and "\\n" in code:
        code = code.replace("\\n", "\n").replace("\\t", "    ")
    return code


def fallback_strategy(params: list[ParamSpec], methods: dict[str, int] | None = None) -> str | None:
    if methods:  # small positive ints: keys collide and sizes stay readable
        calls = ",\n".join(
            f"        {name!r}: st.tuples({', '.join([bound] * n)})"
            for name, n in methods.items()
            for bound in ["st.integers(1, 3)" if name == "__init__" else "st.integers(1, 5)"]
        )
        return f"def strategy():\n    return {{\n{calls},\n    }}\n"
    if not params or any(p.kind not in FALLBACK_STRATEGIES for p in params):
        return None
    parts = ", ".join(FALLBACK_STRATEGIES[p.kind] for p in params)
    return f"def strategy():\n    return st.tuples({parts})\n"


async def write_strategy(
    llm: JsonLLM, sandbox: Sandbox, session: Session, fn: Target, attempts: int = 3
) -> str:
    """Model-written strategy for the argument tuple (for a class, one per method),
    linted and test-drawn in the sandbox."""
    messages: list[Message] = [
        {
            "role": "system",
            "content": "You write Hypothesis strategies that hunt for bugs. Reply with JSON only.",
        },
        {"role": "user", "content": _strategy_task(session, fn)},
    ]
    errors: list[str] = []
    for _ in range(attempts):
        draft = await llm.complete_json("fast", messages, StrategyDraft)
        code = clean_code(draft.code)
        errors = lint_strategy(code) or await _check_strategy_in_sandbox(sandbox, code, session)
        if not errors:
            return code
        messages += [
            {"role": "assistant", "content": draft.model_dump_json()},
            {
                "role": "user",
                "content": "That strategy failed:\n- " + "\n- ".join(errors) + "\nFix it.",
            },
        ]

    if fallback := fallback_strategy(session.params, session.methods):
        logger.warning("using fallback strategy after %d attempts: %s", attempts, errors)
        return fallback
    raise GenerationError(f"could not write a working input strategy: {'; '.join(errors)}")


def _strategy_task(session: Session, fn: Target) -> str:
    if isinstance(fn, ClassInfo):
        keys = ", ".join(
            f"{name!r}: {n} argument{'' if n == 1 else 's'}" for name, n in session.methods.items()
        )
        return (
            f"Intent: {session.intent}\n\n{fn.signature()}\n\n"
            "The class is tested as a state machine: Hypothesis creates an object, then calls "
            "its methods in random order and checks the spec after every call.\n"
            "Write Python source defining `def strategy():` that returns a dict with one key "
            f"per call ({keys}). Each value is a strategy drawing that call's argument "
            "tuple with `st.tuples(...)` (use `st.tuples()` for a call with no arguments). "
            "`st` (hypothesis.strategies) is already imported.\n"
            "Rules:\n"
            "- Draw keys and items from a SMALL range (e.g. st.integers(1, 5)) so calls hit "
            "the same keys and states repeat; values from a small range too.\n"
            "- Constructor arguments must be valid and small (e.g. a capacity of 1 to 3), so "
            "limits are reached within a few calls.\n"
            "- Draw only plain literals: int, float, str, bool, None, list, tuple, dict.\n"
            "- No imports other than hypothesis, math, string. No randomness, no I/O."
        )
    params = "\n".join(f"- {p.name}: {p.kind} ({p.description})" for p in session.params)
    return (
        f"Intent: {session.intent}\nSignature: {fn.signature()}\nParameters:\n{params}\n\n"
        "Write Python source defining `def strategy():` that returns a Hypothesis strategy "
        f"drawing a tuple of exactly {len(fn.params)} positional argument(s), using "
        "`st.tuples(...)`. `st` (hypothesis.strategies) is already imported.\n"
        "Rules:\n"
        "- Draw every input the intent allows, including the awkward ones: empty, "
        "single element, duplicates, boundaries, negatives, values that touch or tie.\n"
        "- Don't assume the input is sorted, unique or non-empty unless the intent, "
        "signature or parameter kind says so: unsorted input breaks a lot of code.\n"
        "- Keep values small (mostly -20..20, sizes up to 8) so failures shrink well, "
        "and respect real constraints (sorted_list_int is sorted; intervals have "
        "start <= end; sorted_intervals are also sorted by start).\n"
        "- Draw only plain literals: int, float, str, bool, None, list, tuple, dict, in "
        "the shape the intent describes (e.g. lists, not tuples, if it says lists).\n"
        "- No imports other than hypothesis, math, string. No randomness, no I/O."
    )


async def _check_strategy_in_sandbox(sandbox: Sandbox, code: str, session: Session) -> list[str]:
    kinds = [p.kind for p in session.params]
    check = (
        f"check_method_strategies(strategy, {session.methods!r})"
        if session.stateful
        else f"check_strategy(strategy, {len(kinds)}, kinds={kinds!r})"
    )
    harness = (
        "from hypothesis import strategies as st\n"
        "from sparring_partner.testing.harness import check_method_strategies, check_strategy\n\n"
        f"{code}\n\n\n"
        "def run(solution, inputs):\n"
        f"    return {check}\n"
    )
    result = await sandbox.run("", harness)  # an empty solution: only the strategy runs
    if result.timed_out:
        return ["drawing examples timed out"]
    if result.error:
        return [f"{result.error.type}: {result.error.message}"]
    return list(result.value or [])


# Harnesses
def exploration_harness(
    session: Session,
    strategy_code: str,
    cases: list[EdgeCase],
    *,
    max_examples: int = 200,
    call_timeout_s: float = 1.0,
    seed: int | None = None,
    budget_s: float | None = None,
) -> str:
    """Source of the harness that hunts for bugs in the sandbox and reports structured results.

    After `budget_s` the hunt stops and reports what it found (e.g. when every call
    loops forever), instead of running into the sandbox's own time limit.
    """
    config = {
        "function": session.function,
        "predicates": {p.statement: p.predicate for p in session.properties},
        "edge_cases": [case.args for case in cases],
        "max_examples": max_examples,
        "call_timeout_s": call_timeout_s,
        "seed": seed,
        "budget_s": budget_s,
    }
    # The strategy comes first so its names can't shadow CONFIG or run().
    return (
        "from hypothesis import strategies as st\n"
        "from sparring_partner.testing.harness import explore\n\n"
        f"{strategy_code}\n\n\n"
        f"CONFIG = {config!r}\n\n\n"
        "def run(solution, inputs):\n"
        "    return explore(solution, CONFIG, strategy)\n"
    )


def predicate_check_harness(
    function: str,
    predicates: dict[str, str],
    inputs: list[tuple[Any, ...]],
    examples: list[tuple[tuple[Any, ...], Any]] = (),
) -> str:
    config = {
        "examples": list(examples),
        "function": function,
        "predicates": predicates,
        "inputs": inputs,
        "call_timeout_s": 1.0,
    }
    return (
        "from sparring_partner.testing.harness import check_predicates\n\n"
        f"CONFIG = {config!r}\n\n\n"
        "def run(solution, inputs):\n"
        "    return check_predicates(solution, CONFIG)\n"
    )
