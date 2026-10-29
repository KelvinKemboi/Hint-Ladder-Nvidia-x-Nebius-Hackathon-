"""Entry point that runs *inside* the sandbox. Standard library only.

Reads request.json from its own directory, imports solution.py (the user's code)
and then either calls harness.run(solution, inputs) or, in batch mode, calls one
function of solution.py on every input with a per-call time limit. Prints one
line, MARKER + JSON, as the last thing on stdout; the user's own prints are
captured and returned inside that JSON.
"""

import ast
import contextlib
import dataclasses
import importlib.util
import io
import json
import os
import resource
import signal
import sys
import time
import traceback

MARKER = "@@SPARRING_RESULT@@"
MAX_CAPTURE = 64 * 1024
MAX_REPR = 2000

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


class CallTimeout(BaseException):
    """BaseException so `except Exception` in user code can't swallow it."""


def _on_alarm(signum, frame):
    raise CallTimeout


@contextlib.contextmanager
def time_limit(seconds):
    if not seconds:
        yield
        return
    previous = signal.signal(signal.SIGALRM, _on_alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def limit_memory(mb):
    if not mb:
        return
    limit = mb * 1024 * 1024
    with contextlib.suppress(ValueError, OSError): # not enforceable on every OS (e.g. macOS)
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def short_repr(value):
    text = repr(value)
    return text if len(text) <= MAX_REPR else text[: MAX_REPR - 3] + "..."


def error_info(exc):
    frames = traceback.extract_tb(exc.__traceback__)
    lines = [f.lineno for f in frames if os.path.basename(f.filename) == "solution.py"]
    if isinstance(exc, SyntaxError) and os.path.basename(exc.filename or "") == "solution.py":
        lines.append(exc.lineno)
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "line": lines[-1] if lines else None,
        "traceback": "".join(traceback.format_exception(exc))[-4000:],
    }


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def call_each(fn, inputs, call_timeout):
    outcomes = []
    for args in inputs:
        args = args if isinstance(args, tuple) else (args,)
        outcome = {"args": short_repr(args), "output": None, "error": None, "timed_out": False}
        start = time.perf_counter()
        try:
            with time_limit(call_timeout):
                outcome["output"] = short_repr(fn(*args))
        except CallTimeout:
            outcome["timed_out"] = True
        except BaseException as exc:  # user code may raise SystemExit etc.
            outcome["error"] = error_info(exc)
        outcome["elapsed_s"] = round(time.perf_counter() - start, 6)
        outcomes.append(outcome)
    return outcomes


def to_json(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, set | frozenset | tuple):
        return list(value)
    return repr(value)


def main():
    with open(os.path.join(HERE, "request.json")) as f:
        request = json.load(f)
    inputs = [ast.literal_eval(i) for i in request.get("inputs", [])]
    limit_memory(request.get("memory_mb"))

    result = {"phase": "import", "error": None, "value": None, "outcomes": []}
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with time_limit(request.get("import_timeout_s")):
                solution = load("solution")
            result["phase"] = "harness"
            if request["mode"] == "batch":
                fn = getattr(solution, request["function"])
                result["outcomes"] = call_each(fn, inputs, request.get("call_timeout_s"))
            else:
                result["value"] = load("harness").run(solution, inputs)
            result["phase"] = "done"
    except CallTimeout:
        result["error"] = {"type": "TimeoutError", "message": "import timed out", "line": None}
    except BaseException as exc:
        result["error"] = error_info(exc)

    result["stdout"] = out.getvalue()[-MAX_CAPTURE:]
    result["stderr"] = err.getvalue()[-MAX_CAPTURE:]
    sys.stdout.write("\n" + MARKER + json.dumps(result, default=to_json) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
