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
)
from sparring_partner.testing.counterexamples import Property


def load_predicate(source: str) -> Callable[[tuple[Any, ...], Any], Any]:
    """Compile a property's predicate source, which must define `check(args, output)`."""
    namespace: dict[str, Any] = {}
    exec(compile(source, "<property>", "exec"), namespace)
    check = namespace.get("check")
    if not callable(check):
        raise ValueError("predicate must define check(args, output)")
    return check


def check_predicates(solution: Any, config: dict[str, Any]) -> dict[str, Any]:
    """Run every predicate on the user's output for a few trivial inputs.

    A predicate is broken if it fails to load, raises, or returns a non-bool.
    Returning False there is fine: the user's code may well be wrong. It is also
    broken if it rejects a worked example's known-correct output.
    """
    fn = getattr(solution, config["function"])
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


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_interval(value: Any) -> bool:
    return (
        isinstance(value, list | tuple)
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


def explore(
    solution: Any, config: dict[str, Any], strategy_fn: Callable[[], Any]
) -> dict[str, Any]:
    """Hunt for bugs: Hypothesis per property plus the hand-picked edge cases."""
    fn = getattr(solution, config["function"])
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
    )
    return {
        "counterexamples": [ce.to_dict() for ce in found],
        "property_errors": property_errors,
        "edge_cases": len(config["edge_cases"]),
        "max_examples": config["max_examples"],
    }


def trace_call(solution: Any, config: dict[str, Any]) -> dict[str, Any]:
    """Run one call and record the line and local variables at each step in the user's code.

    Each step is the state *before* that line runs. Values are short reprs.
    """
    fn = getattr(solution, config["function"])
    filename = fn.__code__.co_filename
    max_steps = config.get("max_steps", 200)
    steps: list[dict[str, Any]] = []

    def local_trace(frame: Any, event: str, arg: Any) -> Any:
        if event == "line" and len(steps) < max_steps:
            steps.append(
                {
                    "line": frame.f_lineno,
                    "function": frame.f_code.co_name,
                    "locals": {k: _short(v) for k, v in frame.f_locals.items()},
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


def _short(value: Any, limit: int = 80) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def evaluate_prediction(solution: Any, config: dict[str, Any]) -> dict[str, Any]:
    """Compare the user's prediction with the actual output, the reference fix and the spec."""
    timeout = config.get("call_timeout_s")
    name = config["function"]
    args = _as_args(config["args"])
    predicted = config["predicted"]
    actual = _call(getattr(solution, name), args, timeout)

    reference = None
    if config.get("reference_fix"):
        reference_fn = _load_function(config["reference_fix"], name, timeout)
        if reference_fn is not None:
            reference = _call(reference_fn, args, timeout)

    violates = []
    if predicted["kind"] == "value":
        for statement, source in config["predicates"].items():
            try:
                prop = Property(statement, load_predicate(source))
            except Exception:
                continue
            if _check(prop, args, predicted["value"], timeout) == "":
                violates.append(statement)

    return {
        "actual": _describe_outcome(actual),
        "reference": reference and _describe_outcome(reference),
        "matches_actual": _matches(predicted, actual),
        "matches_reference": None if reference is None else _matches(predicted, reference),
        "violates": violates,
    }


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
    return fn if callable(fn) else None


def _describe_outcome(outcome: _Outcome) -> dict[str, Any]:
    if outcome.exception is not None:
        exc = outcome.exception
        return {"output": None, "error": f"{type(exc).__name__}: {exc}"}
    return {"output": _short(outcome.output, 2000), "error": None}


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
    fn = getattr(solution, config["function"])
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
    fn = getattr(solution, config["function"])
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
    )
    variants = check_inputs(solution, {**config, "inputs": config["variants"]})["failures"]
    originals = check_inputs(solution, {**config, "inputs": config["originals"]})["failures"]
    return {
        "counterexamples": [ce.to_dict() for ce in found],
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
