"""Sandbox-side entry points for generated harnesses. Hypothesis + standard library only.

A generated harness is a few lines: a CONFIG literal, the model-written
`strategy()` function, and a `run(solution, inputs)` that calls one of these.
Everything here returns plain JSON-able dicts so results travel back intact.
"""

import ast
import copy
import functools
import importlib
import sys
from collections.abc import Callable
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis.strategies import SearchStrategy

from sparring_partner.testing.capture import (
    _as_args,
    _call,
    _CallTimeout,
    _check,
    _Outcome,
    _record,
    _time_limit,
    find_counterexamples,
    resolve_target,
    user_file,
)
from sparring_partner.testing.counterexamples import Property


def _target(solution: Any, config: dict[str, Any]) -> Callable[..., Any]:
    """The function under test, or for a class, its operation runner (see capture)."""
    return resolve_target(getattr(solution, config["function"]))


def load_predicate(source: str) -> Callable[[tuple[Any, ...], Any], Any]:
    """Compile a property's predicate source, which must define `check(args, output)`.

    For a class it may define `check(ops, output)` instead, taking the operation list
    itself rather than the argument tuple `(ops,)` that holds it.
    """
    namespace: dict[str, Any] = {}
    exec(compile(source, "<property>", "exec"), namespace)
    check = namespace.get("check")
    if not callable(check):
        raise ValueError("predicate must define check(args, output)")
    code = getattr(check, "__code__", None)
    if code is not None and code.co_argcount >= 1 and code.co_varnames[0] == "ops":
        takes_ops = check
        return lambda args, output: takes_ops(args[0], output)
    return check


def check_predicates(solution: Any, config: dict[str, Any]) -> dict[str, Any]:
    """Run every predicate on the user's output for a few trivial inputs.

    A predicate is broken if it fails to load, raises, or returns a non-bool.
    Returning False there is fine: the user's code may well be wrong. It is also
    broken if it rejects a worked example's known-correct output.
    """
    fn = _target(solution, config)
    errors: dict[str, str] = {}
    for name, source in config["predicates"].items():
        try:
            check = load_predicate(source)
        except Exception as exc:
            errors[name] = f"{type(exc).__name__}: {exc}"
            continue
        for raw_args in config["inputs"]:
            args = _as_args(raw_args)
            outcome = _call(fn, args, config.get("call_timeout_s"))
            if outcome.exception is not None:
                continue  # can't judge the predicate where the code itself crashes
            try:
                with _time_limit(config.get("call_timeout_s")):
                    value = check(copy.deepcopy(args), outcome.output)
            except _CallTimeout:
                errors[name] = f"timed out on a trivial input (args={args!r})"
                break
            except Exception as exc:
                errors[name] = f"{type(exc).__name__}: {exc} (args={args!r})"
                break
            if not isinstance(value, bool):
                errors[name] = f"returned {type(value).__name__}, expected bool (args={args!r})"
                break
        if name not in errors and (rejected := _rejects_correct_output(check, config)):
            errors[name] = rejected
    return {"errors": errors}


def _rejects_correct_output(check: Callable[..., Any], config: dict[str, Any]) -> str | None:
    """Why `check` fails a worked example (args with a known-correct output), if it does."""
    for raw_args, expected in config.get("examples", []):
        args = _as_args(raw_args)
        try:
            with _time_limit(config.get("call_timeout_s")):
                value = check(copy.deepcopy(args), copy.deepcopy(expected))
        except _CallTimeout:
            continue
        except Exception as exc:
            return f"{type(exc).__name__}: {exc} (args={args!r}, output={expected!r})"
        if value is not True:
            return f"says the correct output {expected!r} for args {args!r} breaks the property"
    return None


def _is_op(value: Any) -> bool:
    return isinstance(value, list) and len(value) >= 1 and isinstance(value[0], str)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_interval(value: Any) -> bool:
    """A [start, end] list. Not a tuple: code that updates an interval in place (common in
    merging) would fail on tuples for a reason the intent never asked about."""
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(_is_int(v) for v in value)
        and value[0] <= value[1]
    )


# What a drawn argument of each input kind must look like (see agents.testgen.Kind).
KIND_CHECKS: dict[str, Callable[[Any], bool]] = {
    "int": _is_int,
    "float": lambda x: isinstance(x, int | float) and not isinstance(x, bool),
    "str": lambda x: isinstance(x, str),
    "bool": lambda x: isinstance(x, bool),
    "list_int": lambda x: isinstance(x, list) and all(_is_int(v) for v in x),
    "sorted_list_int": lambda x: (
        isinstance(x, list) and all(_is_int(v) for v in x) and x == sorted(x)
    ),
    "list_str": lambda x: isinstance(x, list) and all(isinstance(v, str) for v in x),
    "intervals": lambda x: isinstance(x, list) and all(_is_interval(v) for v in x),
    "sorted_intervals": lambda x: (
        isinstance(x, list)
        and all(_is_interval(v) for v in x)
        and [v[0] for v in x] == sorted(v[0] for v in x)
    ),
    "operations": lambda x: isinstance(x, list) and len(x) >= 1 and all(_is_op(v) for v in x),
    "dict_str_int": lambda x: (
        isinstance(x, dict) and all(isinstance(k, str) and _is_int(v) for k, v in x.items())
    ),
}


def check_strategy(
    strategy_fn: Callable[[], Any], arity: int, n: int = 20, kinds: list[str] | None = None
) -> list[str]:
    """Problems with a generated strategy, or [] if it draws valid argument tuples."""
    try:
        strategy = strategy_fn()
    except Exception as exc:
        return [f"strategy() raised {type(exc).__name__}: {exc}"]
    if not isinstance(strategy, SearchStrategy):
        return [f"strategy() must return a Hypothesis strategy, got {type(strategy).__name__}"]

    draws: list[Any] = []

    @settings(max_examples=n, database=None, deadline=None, suppress_health_check=list(HealthCheck))
    @given(strategy)
    def collect(value: Any) -> None:
        draws.append(value)

    try:
        collect()
    except Exception as exc:
        return [f"drawing examples failed: {type(exc).__name__}: {exc}"]
    if not draws:
        return ["the strategy drew no examples"]
    for value in draws:
        if not isinstance(value, tuple) or len(value) != arity:
            return [f"must draw a tuple of {arity} argument(s), got {value!r:.200}"]
        try:
            ast.literal_eval(repr(value))
        except (ValueError, SyntaxError):
            return [f"must draw plain Python literals (lists, ints, str...), got {value!r:.200}"]
        for i, (arg, kind) in enumerate(zip(value, kinds or [], strict=False)):
            check = KIND_CHECKS.get(kind)
            if check is not None and not check(arg):
                return [f"argument {i + 1} must be {kind}, got {arg!r:.200}"]
    return []


def check_method_strategies(
    strategy_fn: Callable[[], Any], methods: dict[str, int], n: int = 20
) -> list[str]:
    """Problems with a class's strategies: `strategy()` must return a dict with an
    argument-tuple strategy for "__init__" and for each method, of the right arity."""
    try:
        strategies = strategy_fn()
    except Exception as exc:
        return [f"strategy() raised {type(exc).__name__}: {exc}"]
    if not isinstance(strategies, dict):
        return [f"strategy() must return a dict of strategies, got {type(strategies).__name__}"]
    if missing := [m for m in methods if m not in strategies]:
        return [f"strategy() must have a key for {', '.join(map(repr, missing))}"]
    if extra := [m for m in strategies if m not in methods]:
        return [f"there are no methods named {', '.join(map(repr, extra))}"]
    for name, arity in methods.items():
        if problems := check_strategy(lambda name=name: strategies[name], arity, n):
            return [f"{name!r}: {problems[0]}"]
    return []


def explore(
    solution: Any, config: dict[str, Any], strategy_fn: Callable[[], Any]
) -> dict[str, Any]:
    """Hunt for bugs: Hypothesis per property plus the hand-picked edge cases."""
    fn = _target(solution, config)
    properties, property_errors = [], {}
    for name, source in config["predicates"].items():
        try:
            properties.append(Property(name, load_predicate(source)))
        except Exception as exc:
            property_errors[name] = f"{type(exc).__name__}: {exc}"

    found = find_counterexamples(
        fn,
        strategy_fn(),
        properties,
        max_examples=config["max_examples"],
        random_seed=config.get("seed"),
        call_timeout_s=config.get("call_timeout_s"),
        examples=config["edge_cases"],
        budget_s=config.get("budget_s"),
        report=(report := {}),
    )
    return {
        "counterexamples": [ce.to_dict() for ce in found],
        "out_of_time": report["out_of_time"],
        "property_errors": property_errors,
        "edge_cases": len(config["edge_cases"]),
        "max_examples": config["max_examples"],
    }


def trace_call(solution: Any, config: dict[str, Any]) -> dict[str, Any]:
    """Run one call and record the line and local variables at each step in the user's code.

    Each step is the state *before* that line runs. Values are short reprs.
    """
    fn = _target(solution, config)
    filename = user_file(fn)
    max_steps = config.get("max_steps", 200)
    steps: list[dict[str, Any]] = []

    def local_trace(frame: Any, event: str, arg: Any) -> Any:
        if event == "line" and len(steps) < max_steps:
            steps.append(
                {
                    "line": frame.f_lineno,
                    "function": frame.f_code.co_name,
                    "locals": _locals(frame.f_locals),
                }
            )
        return local_trace

    def global_trace(frame: Any, event: str, arg: Any) -> Any:
        return local_trace if frame.f_code.co_filename == filename else None

    sys.settrace(global_trace)
    try:
        outcome = _call(fn, _as_args(config["args"]), config.get("call_timeout_s"))
    finally:
        sys.settrace(None)
    exc = outcome.exception
    return {
        "steps": steps,
        "truncated": len(steps) >= max_steps,
        "output": None if exc else _short(outcome.output),
        "exception": f"{type(exc).__name__}: {exc}" if exc else None,
    }


def _locals(names: dict[str, Any]) -> dict[str, str]:
    """Short reprs; `self` as its attributes (self.size = 2), since an object's default
    repr is just its memory address."""
    shown = {}
    for key, value in names.items():
        attrs = getattr(value, "__dict__", None) if key == "self" else None
        if isinstance(attrs, dict):
            shown |= {f"self.{k}": _short(v) for k, v in attrs.items()}
        else:
            shown[key] = _short(value)
    return shown


def _short(value: Any, limit: int = 80) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def evaluate_prediction(solution: Any, config: dict[str, Any]) -> dict[str, Any]:
    """Compare the user's prediction with the actual output, the reference fix and the spec."""
    timeout = config.get("call_timeout_s")
    name = config["function"]
    args = _as_args(config["args"])
    predicted = config["predicted"]
    fn = _target(solution, config)
    actual = _call(fn, args, timeout)

    reference = None
    if config.get("reference_fix"):
        reference_fn = _load_function(config["reference_fix"], name, timeout)
        if reference_fn is not None:
            reference = _call(reference_fn, args, timeout)

    # For a class the prediction is what the last call returns; the properties see
    # it in place of that call's result.
    as_output = predicted["value"]
    if stateful := hasattr(fn, "target_class"):
        before = next((o.output for o in (actual, reference) if o and o.exception is None), None)
        as_output = None if before is None else [*before[:-1], predicted["value"]]
        actual, reference = _last(actual), reference and _last(reference)

    violates = []
    if predicted["kind"] == "value" and not (stateful and as_output is None):
        for statement, source in config["predicates"].items():
            try:
                prop = Property(statement, load_predicate(source))
            except Exception:
                continue
            if _check(prop, args, as_output, timeout) == "":
                violates.append(statement)

    return {
        "actual": _describe_outcome(actual),
        "reference": reference and _describe_outcome(reference),
        "matches_actual": _matches(predicted, actual),
        "matches_reference": None if reference is None else _matches(predicted, reference),
        "violates": violates,
    }


def _last(outcome: _Outcome) -> _Outcome:
    """A class's outcome narrowed to its last call."""
    if outcome.exception is not None or not outcome.output:
        return outcome
    return _Outcome(output=outcome.output[-1])


def fixes_failure(solution: Any, config: dict[str, Any]) -> dict[str, Any]:
    """Does any code variant make a counterexample satisfy every property?

    `solution` is unused: variants are full sources compiled here, in the sandbox.
    """
    timeout = config.get("call_timeout_s")
    properties = []
    for statement, source in config["predicates"].items():
        try:
            properties.append(Property(statement, load_predicate(source)))
        except Exception:
            continue
    for index, variant in enumerate(config["variants"]):
        fn = _load_function(variant, config["function"], timeout)
        if fn is None:
            continue
        for raw_args in config["cases"]:
            args = _as_args(raw_args)
            outcome = _call(fn, args, timeout)
            if outcome.exception is None and all(
                _check(p, args, outcome.output, timeout) is None for p in properties
            ):
                return {"fixed": True, "variant": index}
    return {"fixed": False, "variant": None}


def _load_function(source: str, name: str, timeout: float | None) -> Any:
    namespace: dict[str, Any] = {"__name__": "candidate"}
    try:
        with _time_limit(timeout):
            exec(compile(source, "<candidate>", "exec"), namespace)
    except BaseException:  # a candidate may raise anything, including our timeout
        return None
    fn = namespace.get(name)
    return resolve_target(fn) if callable(fn) else None


def _describe_outcome(outcome: _Outcome) -> dict[str, Any]:
    if outcome.exception is not None:
        exc = outcome.exception
        error = f"{type(exc).__name__}: {exc}"
        return {"output": None, "error": error, "timed_out": outcome.timed_out}
    return {"output": _short(outcome.output, 2000), "error": None, "timed_out": False}


def _matches(predicted: dict[str, Any], outcome: _Outcome) -> bool:
    if predicted["kind"] == "error":
        return outcome.exception is not None
    return outcome.exception is None and _normalize(predicted["value"]) == _normalize(
        outcome.output
    )


def _normalize(value: Any) -> Any:
    """Tolerate list-vs-tuple differences: [(1, 5)] counts as [[1, 5]]."""
    if isinstance(value, list | tuple):
        return [_normalize(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    return value


def _properties(predicates: dict[str, str]) -> list[Property]:
    properties = []
    for statement, source in predicates.items():
        try:
            properties.append(Property(statement, load_predicate(source)))
        except Exception:
            continue
    return properties


def check_inputs(solution: Any, config: dict[str, Any]) -> dict[str, Any]:
    """Which of the given inputs break a property (or crash)? Index -> counterexample."""
    fn = _target(solution, config)
    timeout = config.get("call_timeout_s")
    properties = _properties(config["predicates"])
    call = functools.partial(_call, timeout_s=timeout)
    check = functools.partial(_check, timeout_s=timeout)
    failures = {}
    for index, raw_args in enumerate(config["inputs"]):
        ce = _record(fn, _as_args(raw_args), "", properties, call, check)
        if ce.violated_properties:
            ce.violated_property = ce.violated_properties[0]
            failures[index] = ce.to_dict()
    return {"failures": failures}


def verify_fix(
    solution: Any, config: dict[str, Any], strategy_fn: Callable[[], Any]
) -> dict[str, Any]:
    """Re-run the whole bug hunt on a fix with a fresh seed, then try nearby inputs.

    `config["examples"]` are the original counterexamples and edge cases;
    `config["variants"]` are the counterexamples shifted slightly, to catch a fix
    that only handles the exact failing input.
    """
    fn = _target(solution, config)
    timeout = config.get("call_timeout_s")
    properties = _properties(config["predicates"])
    found = find_counterexamples(
        fn,
        strategy_fn(),
        properties,
        max_examples=config["max_examples"],
        random_seed=config["seed"],
        call_timeout_s=timeout,
        examples=config["examples"],
        budget_s=config.get("budget_s"),
        report=(report := {}),
    )
    variants = check_inputs(solution, {**config, "inputs": config["variants"]})["failures"]
    originals = check_inputs(solution, {**config, "inputs": config["originals"]})["failures"]
    return {
        "counterexamples": [ce.to_dict() for ce in found],
        "out_of_time": report["out_of_time"],
        "original_failures": sorted(originals),
        "variant_failures": [variants[i] for i in sorted(variants)],
        "inputs_checked": config["max_examples"] * (len(properties) + 1)
        + len(config["examples"])
        + len(config["variants"]),
    }


def resolve_apis(solution: Any, config: dict[str, Any]) -> dict[str, Any]:
    """Which `module.attr.chain` references don't exist in this environment?

    `solution` is unused: only the names in config are imported and looked up.
    """
    missing = []
    for ref in config["references"]:
        module_name, attrs = ref["module"], ref["attrs"]
        try:
            obj = importlib.import_module(module_name)
        except Exception as exc:
            missing.append({**ref, "error": f"{type(exc).__name__}: {exc}"})
            continue
        for depth, attr in enumerate(attrs):
            path = ".".join([module_name, *attrs[: depth + 1]])
            if hasattr(obj, attr):
                obj = getattr(obj, attr)
                continue
            try:  # a submodule that isn't imported yet, e.g. xml.etree
                obj = importlib.import_module(path)
            except Exception:
                missing.append({**ref, "error": f"{path} does not exist"})
                break
    return {"missing": missing}
