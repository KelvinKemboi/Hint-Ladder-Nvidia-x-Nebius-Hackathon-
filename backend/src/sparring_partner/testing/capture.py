"""Find, shrink and record counterexamples with Hypothesis.

Each property gets its own search, so Hypothesis shrinks towards the smallest
input that breaks *that* property. Exceptions from the user's function count
only against the implicit NO_CRASH property; the other searches skip those
inputs so one crash doesn't hide a wrong-output bug.

Depends only on Hypothesis and the standard library so the sandbox harness can
run it next to the user's code.
"""

import contextlib
import copy
import functools
import signal
import threading
import traceback
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass
from typing import Any

from hypothesis import HealthCheck, given, seed, settings
from hypothesis import strategies as st
from hypothesis.errors import Flaky

from sparring_partner.testing.counterexamples import (
    NO_CRASH,
    Counterexample,
    Property,
    complexity,
    dedupe,
    short_repr,
)


class _PropertyFailed(Exception):
    pass


@dataclass
class _Outcome:
    output: Any = None
    exception: BaseException | None = None


def find_counterexamples(
    fn: Callable[..., Any],
    args_strategy: st.SearchStrategy[Any],
    properties: Sequence[Property],
    *,
    max_examples: int = 200,
    random_seed: int | None = None,
    call_timeout_s: float | None = None,
) -> list[Counterexample]:
    """Return one minimal counterexample per distinct failure, best first.

    `args_strategy` draws a tuple of positional arguments (a non-tuple value is
    treated as the only argument). With `call_timeout_s`, a call that runs longer
    (e.g. an infinite loop) counts as raising TimeoutError at the line it was stuck on.
    """
    call = functools.partial(_call, timeout_s=call_timeout_s)
    found = []
    for prop in [None, *properties]:
        args = _shrink(fn, args_strategy, prop, max_examples, random_seed, call)
        if args is None:
            continue
        ce = _record(fn, args, prop.name if prop else NO_CRASH, properties, call)
        # Re-running must reproduce the failure; drop flaky ones.
        if ce.violated_property in ce.violated_properties:
            found.append(ce)
    return dedupe(found, [NO_CRASH, *(p.name for p in properties)])


def _shrink(
    fn: Callable[..., Any],
    args_strategy: st.SearchStrategy[Any],
    prop: Property | None,
    max_examples: int,
    random_seed: int | None,
    call: Callable[..., _Outcome],
) -> tuple[Any, ...] | None:
    """Search for inputs breaking `prop` (None = NO_CRASH); return the shrunk one."""
    failures: list[tuple[Any, ...]] = []

    @settings(
        max_examples=max_examples,
        database=None,
        deadline=None,
        derandomize=random_seed is None,
        report_multiple_bugs=False,
        print_blob=False,
        suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
    )
    @given(args_strategy)
    def search(drawn: Any) -> None:
        args = _as_args(drawn)
        outcome = call(fn, args)
        if prop is None:
            failed = outcome.exception is not None
        elif outcome.exception is not None:
            return
        else:
            failed = _check(prop, args, outcome.output) is not None
        if failed:
            failures.append(args)
            raise _PropertyFailed

    if random_seed is not None:
        search = seed(random_seed)(search)

    with contextlib.suppress(_PropertyFailed, Flaky):
        search()
    # Hypothesis replays the minimal example last, so the final failure is the shrunk one
    return failures[-1] if failures else None


def _record(
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    found_by: str,
    properties: Sequence[Property],
    call: Callable[..., _Outcome],
) -> Counterexample:
    """Run `fn` on `args` again and record everything that goes wrong."""
    name = getattr(fn, "__name__", repr(fn))
    arg_reprs = [short_repr(a) for a in args]
    outcome = call(fn, args)

    ce = Counterexample(
        function=name,
        args=arg_reprs,
        call=f"{name}({', '.join(arg_reprs)})",
        violated_property=found_by,
        complexity=sum(complexity(a) for a in args),
    )

    if outcome.exception is not None:
        exc = outcome.exception
        ce.violated_properties = [NO_CRASH]
        ce.exception_type = type(exc).__name__
        ce.exception_message = str(exc)
        ce.exception_line = _user_line(fn, exc)
        return ce

    ce.output = short_repr(outcome.output)
    for prop in properties:
        error = _check(prop, args, outcome.output)
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
    try:
        with _time_limit(timeout_s):
            return _Outcome(output=fn(*args))
    except _CallTimeout as stuck:
        exc = TimeoutError(f"did not finish within {timeout_s}s")
        exc.__traceback__ = stuck.__traceback__  # keeps the line it was stuck on
        return _Outcome(exception=exc)
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


def _check(prop: Property, args: tuple[Any, ...], output: Any) -> str | None:
    """None if the property holds, "" if it is false, or the predicate's own error."""
    try:
        return None if prop.check(copy.deepcopy(args), output) else ""
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


def _user_line(fn: Callable[..., Any], exc: BaseException) -> int | None:
    """Line of the deepest traceback frame inside the user's source file."""
    code = getattr(fn, "__code__", None)
    if code is None:
        return None
    lines = [
        frame.lineno
        for frame in traceback.extract_tb(exc.__traceback__)
        if frame.filename == code.co_filename
    ]
    return lines[-1] if lines else None
