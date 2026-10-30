"""Find, shrink and record counterexamples with Hypothesis.

Each property gets its own search, so Hypothesis shrinks towards the smallest
input that breaks *that* property. Exceptions from the user's function count
only against the implicit NO_CRASH property; the other searches skip those
inputs so one crash doesn't hide a wrong-output bug.

A class is tested as a state machine (Hypothesis RuleBasedStateMachine): each
public method is a rule, and every property is checked after every call. Its
"input" is the operation sequence, which shrinks like any other input; see
`stateful_target`.

Depends only on Hypothesis and the standard library so the sandbox harness can
run it next to the user's code.
"""

import contextlib
import copy
import inspect
import math
import re
import signal
import threading
import time
import traceback
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass
from typing import Any

from hypothesis import HealthCheck, Verbosity, given, reject, seed, settings
from hypothesis import strategies as st
from hypothesis.errors import Flaky
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    rule,
    run_state_machine_as_test,
)

from sparring_partner.testing.counterexamples import (
    NO_CRASH,
    Counterexample,
    Property,
    complexity,
    dedupe,
    short_repr,
)

INIT = "__init__"  # key of the constructor's strategy for a class
STEPS = 50  # most method calls in one generated operation sequence (Hypothesis's default)
# Hypothesis may spend thousands of runs shrinking an operation sequence; after this
# long it stops, and _simplify finishes the job in far fewer replays.
SHRINK_S = 2.0

# searches stop a call once it runs STUCK_FACTOR times longer than the slowest call
# that finished (at least STUCK_FLOOR_S). Failures are still recorded with the full
# limit, so a call that was only slow is never reported as a hang.
STUCK_FACTOR = 20
STUCK_FLOOR_S = 0.02


class _PropertyFailed(Exception):
    pass


@dataclass
class _Outcome:
    output: Any = None
    exception: BaseException | None = None
    timed_out: bool = False


class _Runner:
    """Calls the user's code and checks properties under the time limits."""

    def __init__(self, call_timeout_s: float | None, budget_s: float | None = None):
        self.limit = call_timeout_s
        self.slowest = 0.0
        self.stuck = False
        self.ends = time.monotonic() + budget_s if budget_s else None

    @property
    def out_of_time(self) -> bool:
        return self.ends is not None and time.monotonic() > self.ends

    def timeout(self) -> float | None:
        if not self.limit or not self.stuck:
            return self.limit
        return min(self.limit, max(STUCK_FLOOR_S, STUCK_FACTOR * self.slowest))

    def run(self, thunk: Callable[[], Any]) -> _Outcome:
        start = time.monotonic()
        outcome = _invoke(thunk, self.timeout(), self.limit)
        if outcome.timed_out:
            self.stuck = True
        else:
            self.slowest = max(self.slowest, time.monotonic() - start)
        return outcome

    def call(self, fn: Callable[..., Any], args: tuple[Any, ...]) -> _Outcome:
        args = copy.deepcopy(args)
        return self.run(lambda: fn(*args))

    def check(self, prop: Property, args: tuple[Any, ...], output: Any) -> str | None:
        return _check(prop, args, output, self.limit)


def find_counterexamples(
    fn: Callable[..., Any],
    args_strategy: st.SearchStrategy[Any] | dict[str, st.SearchStrategy[Any]],
    properties: Sequence[Property],
    *,
    max_examples: int = 200,
    random_seed: int | None = None,
    call_timeout_s: float | None = None,
    examples: Sequence[Any] = (),
    budget_s: float | None = None,
    report: dict[str, Any] | None = None,
) -> list[Counterexample]:
    """Return one minimal counterexample per distinct failure, best first.

    `args_strategy` draws a tuple of positional arguments (a non-tuple value is
    treated as the only argument). For a class (see `stateful_target`) it is a
    dict of argument-tuple strategies: INIT for the constructor, and one per method.
    With `call_timeout_s`, a call that runs longer (e.g. an infinite loop) counts
    as raising TimeoutError at the line it was stuck on.
    `examples` are hand-picked argument tuples (edge cases) checked as well; a
    failing one competes with the shrunk counterexamples in deduplication.
    After `budget_s` seconds the searches stop early and return what they found;
    `report["out_of_time"]` then says so.
    """
    runner = _Runner(call_timeout_s, budget_s)
    strict = _Runner(call_timeout_s)  # re-runs get the full limit
    search = _shrink_stateful if isinstance(args_strategy, dict) else _shrink
    found = []
    for prop in [None, *properties]:
        if runner.out_of_time:
            break
        args = search(fn, args_strategy, prop, max_examples, random_seed, runner)
        if args is None:
            continue
        ce = _record(
            fn, args, prop.name if prop else NO_CRASH, properties, strict.call, strict.check
        )
        # Re-running must reproduce the failure; drop flaky ones.
        if ce.violated_property in ce.violated_properties:
            found.append(ce)
    for example in examples:
        if runner.out_of_time:
            break
        ce = _record(fn, _as_args(example), "", properties, strict.call, strict.check)
        if ce.violated_properties:
            ce.violated_property = ce.violated_properties[0]
            ce.hand_picked = True
            found.append(ce)
    if report is not None:
        report["out_of_time"] = runner.out_of_time
    return dedupe(found, [NO_CRASH, *(p.name for p in properties)])


def _search_settings(max_examples: int, random_seed: int | None) -> settings:
    return settings(
        max_examples=max_examples,
        database=None,
        deadline=None,
        derandomize=random_seed is None,
        report_multiple_bugs=False,
        print_blob=False,
        stateful_step_count=STEPS,
        verbosity=Verbosity.quiet,
        suppress_health_check=list(HealthCheck),
    )


def _shrink(
    fn: Callable[..., Any],
    args_strategy: st.SearchStrategy[Any],
    prop: Property | None,
    max_examples: int,
    random_seed: int | None,
    runner: _Runner,
) -> tuple[Any, ...] | None:
    """Search for inputs breaking `prop` (None = NO_CRASH); return the shrunk one."""
    failures: list[tuple[Any, ...]] = []

    @_search_settings(max_examples, random_seed)
    @given(args_strategy)
    def search(drawn: Any) -> None:
        if runner.out_of_time:
            return
        args = _as_args(drawn)
        outcome = runner.call(fn, args)
        if prop is None:
            failed = outcome.exception is not None
        elif outcome.exception is not None:
            return
        else:
            failed = runner.check(prop, args, outcome.output) is not None
        if failed:
            failures.append(args)
            raise _PropertyFailed

    if random_seed is not None:
        search = seed(random_seed)(search)

    with contextlib.suppress(_PropertyFailed, Flaky):
        search()
    # Hypothesis replays the minimal example last, so the final failure is the shrunk one
    return failures[-1] if failures else None


# Classes: operation sequences
def stateful_target(cls: type) -> Callable[[list[list[Any]]], list[Any]]:
    """`cls` as a function of one argument, an operation sequence.

    `[["LRUCache", 2], ["put", 1, 1], ["get", 1]]` creates `LRUCache(2)`, then
    calls `put(1, 1)` and `get(1)` on it, and returns what each call returned:
    `[None, None, 1]` (None for the constructor).
    """

    def run_operations(ops: list[list[Any]]) -> list[Any]:
        if not ops or ops[0][0] != cls.__name__:
            raise ValueError(f"the first operation must create the object: [{cls.__name__!r}, ...]")
        obj = cls(*ops[0][1:])
        results: list[Any] = [None]
        for name, *args in ops[1:]:
            results.append(getattr(obj, name)(*args))
        return results

    run_operations.__name__ = cls.__name__
    run_operations.target_class = cls  # type: ignore[attr-defined]
    run_operations.user_file = _class_file(cls)  # type: ignore[attr-defined]
    run_operations.describe = lambda args: describe_operations(cls.__name__, args[0])  # type: ignore[attr-defined]
    return run_operations


def describe_operations(class_name: str, ops: list[list[Any]]) -> str:
    """`cache = LRUCache(2); cache.put(1, 1); cache.get(1)`"""
    var = object_name(class_name)
    calls = []
    for i, (name, *args) in enumerate(ops):
        shown = ", ".join(short_repr(a) for a in args)
        calls.append(f"{var} = {name}({shown})" if i == 0 else f"{var}.{name}({shown})")
    return "; ".join(calls)


def object_name(class_name: str) -> str:
    """A variable name for an instance: LRUCache -> cache, BankAccount -> account."""
    words = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", class_name)
    name = (words[-1] if words else class_name).lower()
    return name if name.isidentifier() else "obj"


def _class_file(cls: type) -> str | None:
    for member in vars(cls).values():
        code = getattr(member, "__code__", None)
        if code is not None:
            return code.co_filename
    return None


def _shrink_stateful(
    fn: Callable[..., Any],
    strategies: dict[str, st.SearchStrategy[Any]],
    prop: Property | None,
    max_examples: int,
    random_seed: int | None,
    runner: _Runner,
) -> tuple[Any, ...] | None:
    """Run the class as a RuleBasedStateMachine and return the shrunk operation sequence
    that breaks `prop` (None = NO_CRASH), as an argument tuple `(ops,)`."""
    cls = fn.target_class  # type: ignore[attr-defined]
    failures: list[tuple[Any, ...]] = []
    seen: dict[tuple[str, int], set[Any]] = {}  # values drawn per (method, argument)
    shrink_until = [math.inf]  # set at the first failure

    class Machine(RuleBasedStateMachine):
        def __init__(self) -> None:
            super().__init__()
            self.obj: Any = None
            self.ops: list[list[Any]] = []
            self.results: list[Any] = []

        @initialize(args=strategies[INIT])
        def create(self, args: tuple[Any, ...]) -> None:
            if time.monotonic() > shrink_until[0]:
                reject()  # out of shrinking time: every later candidate is skipped
            self.step([cls.__name__, *args], lambda: cls(*copy.deepcopy(args)))

        def step(self, op: list[Any], thunk: Callable[[], Any]) -> None:
            if runner.out_of_time:
                return
            self.ops.append(op)
            for i, value in enumerate(op[1:]):
                seen.setdefault((op[0], i), set()).add(value)
            outcome = runner.run(thunk)
            if outcome.exception is not None:
                if prop is not None:
                    reject()  # a crash is NO_CRASH's to find
                self.fail()
            if len(self.ops) == 1:
                self.obj = outcome.output
                outcome.output = None
            self.results.append(outcome.output)
            if prop is not None and runner.check(prop, (self.ops,), self.results) is not None:
                self.fail()

        def fail(self) -> None:
            if not failures:
                shrink_until[0] = time.monotonic() + SHRINK_S
            failures.append((copy.deepcopy(self.ops),))  # the latest is the smallest so far
            raise _PropertyFailed

    def method_rule(name: str) -> Callable[..., None]:
        def call(self: Machine, args: tuple[Any, ...]) -> None:
            if self.obj is not None:
                self.step([name, *args], lambda: getattr(self.obj, name)(*copy.deepcopy(args)))

        call.__name__ = f"call_{name}"
        return rule(args=strategies[name])(call)

    rules = {f"call_{name}": method_rule(name) for name in strategies if name != INIT}
    machine = type(f"{cls.__name__}Machine", (Machine,), rules)
    if random_seed is not None:
        machine = seed(random_seed)(machine)
    with contextlib.suppress(_PropertyFailed, Flaky):
        run_state_machine_as_test(machine, settings=_search_settings(max_examples, random_seed))
    if not failures:
        return None

    def still_fails(ops: list[list[Any]]) -> bool:
        outcome = runner.call(fn, (ops,))
        if prop is None or outcome.exception is not None:
            return prop is None and outcome.exception is not None
        return runner.check(prop, (ops,), outcome.output) is not None

    return (_simplify(failures[-1][0], still_fails, seen),)


def _simplify(
    ops: list[list[Any]],
    still_fails: Callable[[list[list[Any]]], bool],
    seen: dict[tuple[str, int], set[Any]],
) -> list[list[Any]]:
    """Finish shrinking an operation sequence, which Hypothesis can leave a call or two
    long: drop every call the failure doesn't need, then lower integer arguments to
    smaller values the strategy drew for the same argument (all occurrences of a value
    at once first, so a key put and then read changes together)."""
    for _ in range(50):  # each round makes progress or stops; bounded all the same
        if shorter := next(
            (
                cand
                for i in range(len(ops) - 1, 0, -1)
                if still_fails(cand := ops[:i] + ops[i + 1 :])
            ),
            None,
        ):
            ops = shorter
            continue
        if lower := next((cand for cand in _lowered(ops, seen) if still_fails(cand)), None):
            ops = lower
            continue
        break
    return ops


def _lowered(
    ops: list[list[Any]], seen: dict[tuple[str, int], set[Any]]
) -> Generator[list[list[Any]]]:
    """Copies of `ops` with smaller values: first renumbered, then one value replaced by a
    smaller one drawn for that argument."""
    slots = [(i, j) for i, op in enumerate(ops) for j in range(1, len(op)) if _is_int(op[j])]
    if (renumbered := _renumbered(ops, slots)) != ops:
        yield renumbered
    for i, j in slots:
        value = ops[i][j]
        for smaller in sorted(
            v for v in seen.get((ops[i][0], j - 1), ()) if _is_int(v) and v < value
        ):
            # Every occurrence of the value first, then only this one.
            everywhere = [[smaller if (k, m) in slots and op[m] == value else x
                           for m, x in enumerate(op)] for k, op in enumerate(ops)]  # fmt: skip
            yield everywhere
            yield [[smaller if (k, m) == (i, j) else x for m, x in enumerate(op)]
                   for k, op in enumerate(ops)]  # fmt: skip


def _renumbered(ops: list[list[Any]], slots: list[tuple[int, int]]) -> list[list[Any]]:
    """The integers at each argument position, across calls, renumbered in order of first
    appearance using the same values: put(3, 1); get(3); put(1, 1) -> put(1, 1); get(1); put(3, 1).
    The constructor (operation 0) keeps its arguments."""
    renamed = [list(op) for op in ops]
    for position in {j for i, j in slots if i > 0}:
        cells = [(i, j) for i, j in slots if i > 0 and j == position]
        order = list(dict.fromkeys(ops[i][j] for i, j in cells))
        mapping = dict(zip(order, sorted(order), strict=True))
        for i, j in cells:
            renamed[i][j] = mapping[ops[i][j]]
    return renamed


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _record(
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    found_by: str,
    properties: Sequence[Property],
    call: Callable[..., _Outcome],
    check: Callable[..., str | None],
) -> Counterexample:
    """Run `fn` on `args` again and record everything that goes wrong."""
    name = getattr(fn, "__name__", repr(fn))
    arg_reprs = [short_repr(a) for a in args]
    outcome = call(fn, args)

    describe = getattr(fn, "describe", None)
    ce = Counterexample(
        function=name,
        args=arg_reprs,
        call=describe(args) if describe else f"{name}({', '.join(arg_reprs)})",
        violated_property=found_by,
        complexity=sum(complexity(a) for a in args),
    )

    if outcome.exception is not None:
        exc = outcome.exception
        ce.violated_properties = [NO_CRASH]
        ce.exception_type = type(exc).__name__
        ce.exception_message = str(exc)
        ce.exception_line = _user_line(fn, exc)
        ce.timed_out = outcome.timed_out
        return ce

    ce.output = short_repr(outcome.output)
    for prop in properties:
        error = check(prop, args, outcome.output)
        if error is not None:
            ce.violated_properties.append(prop.name)
            if error:
                ce.check_errors[prop.name] = error
    return ce


def _as_args(drawn: Any) -> tuple[Any, ...]:
    return drawn if isinstance(drawn, tuple) else (drawn,)


def _call(fn: Callable[..., Any], args: tuple[Any, ...], timeout_s: float | None) -> _Outcome:
    # Copy so a function that mutates its input can't change what we record
    args = copy.deepcopy(args)
    return _invoke(lambda: fn(*args), timeout_s, timeout_s)


def _invoke(thunk: Callable[[], Any], timeout_s: float | None, shown_s: float | None) -> _Outcome:
    """Run `thunk`, stopping it after `timeout_s`; a hang is reported against `shown_s`."""
    try:
        with _time_limit(timeout_s):
            return _Outcome(output=thunk())
    except _CallTimeout as stuck:
        exc = TimeoutError(f"did not finish within {shown_s}s")
        exc.__traceback__ = stuck.__traceback__  # keeps the line it was stuck on
        return _Outcome(exception=exc, timed_out=True)
    except Exception as exc:
        return _Outcome(exception=exc)


class _CallTimeout(BaseException):
    """BaseException so `except Exception` in user code can't swallow it."""


def _on_alarm(signum: int, frame: Any) -> None:
    raise _CallTimeout


@contextlib.contextmanager
def _time_limit(seconds: float | None) -> Generator[None]:
    # SIGALRM only works on the main thread; elsewhere rely on the sandbox's wall-clock limit.
    if not seconds or threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.signal(signal.SIGALRM, _on_alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _check(
    prop: Property, args: tuple[Any, ...], output: Any, timeout_s: float | None = None
) -> str | None:
    """None if the property holds, "" if it is false, or the predicate's own error.

    A predicate that runs out of time or memory (e.g. it builds a huge range) can't
    tell us anything, so it counts as holding rather than blaming the user's code.
    """
    args = copy.deepcopy(args)
    try:
        with _time_limit(timeout_s):
            return None if prop.check(args, output) else ""
    except (_CallTimeout, MemoryError):
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def user_file(fn: Callable[..., Any]) -> str | None:
    """The file holding the user's code: `fn`'s own, or its class's for a stateful target."""
    if hasattr(fn, "user_file"):
        return fn.user_file
    code = getattr(fn, "__code__", None)
    return code.co_filename if code is not None else None


def _user_line(fn: Callable[..., Any], exc: BaseException) -> int | None:
    """Line of the deepest traceback frame inside the user's source file."""
    filename = user_file(fn)
    if filename is None:
        return None
    lines = [
        frame.lineno
        for frame in traceback.extract_tb(exc.__traceback__)
        if frame.filename == filename
    ]
    return lines[-1] if lines else None


def resolve_target(obj: Any) -> Callable[..., Any]:
    """What the harness calls: a function as is, a class as its operation runner."""
    return stateful_target(obj) if inspect.isclass(obj) else obj
