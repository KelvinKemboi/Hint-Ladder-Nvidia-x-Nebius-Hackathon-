import ast
import json
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

import sparring_partner
from sparring_partner.config import Settings, get_settings
from sparring_partner.sandbox.backends import (
    RUNNER,
    Backend,
    ContreeBackend,
    LocalBackend,
    RawRun,
)
from sparring_partner.sandbox.models import ErrorInfo, RunResult

MARKER = "@@SPARRING_RESULT@@"  # must match _runner.MARKER
IMPORT_TIMEOUT_S = 10.0

PACKAGE_DIR = Path(sparring_partner.__file__).parent
# Shipped with every run so harnesses can `from sparring_partner.testing import ...`.
SUPPORT_FILES = [
    "__init__.py",
    "testing/__init__.py",
    "testing/capture.py",
    "testing/counterexamples.py",
    "testing/harness.py",
]
IMPORT_ONLY_HARNESS = "def run(solution, inputs):\n    return None\n"


class Sandbox:
    """Runs untrusted code: the user's solution plus a harness, never in the API process.

    Harness contract: a module defining `run(solution, inputs)`, where `solution` is
    the imported user code and `inputs` the decoded input list. Its return value
    (results, counterexamples, trace payloads) comes back as `RunResult.value`.
    """

    def __init__(self, backend: Backend, settings: Settings | None = None):
        self.backend = backend
        self.settings = settings or get_settings()

    async def run(
        self,
        code: str,
        harness: str | None = None,
        *,
        inputs: Sequence[Any] = (),
        timeout_s: float | None = None,
    ) -> RunResult:
        """Import `code` and call `harness.run(solution, inputs)` in the sandbox."""
        request = {"mode": "harness", "inputs": _encode(inputs)}
        return await self._execute(code, request, harness or IMPORT_ONLY_HARNESS, timeout_s)

    async def run_batch(
        self,
        code: str,
        function: str,
        inputs: Sequence[Any],
        *,
        call_timeout_s: float = 2.0,
        timeout_s: float | None = None,
    ) -> RunResult:
        """Call `function` once per input (a tuple of positional args, or a single arg).

        Each call has its own time limit, so one infinite loop is recorded as a
        timed-out outcome instead of sinking the whole batch.
        """
        request = {
            "mode": "batch",
            "function": function,
            "inputs": _encode(inputs),
            "call_timeout_s": call_timeout_s,
        }
        return await self._execute(code, request, None, timeout_s)

    async def _execute(
        self, code: str, request: dict[str, Any], harness: str | None, timeout_s: float | None
    ) -> RunResult:
        request |= {
            "import_timeout_s": IMPORT_TIMEOUT_S,
            "memory_mb": self.settings.sandbox_memory_mb,
        }
        files = {path: (PACKAGE_DIR / path).read_bytes() for path in SUPPORT_FILES}
        files = {f"sparring_partner/{path}": content for path, content in files.items()}
        files[RUNNER] = (Path(__file__).parent / RUNNER).read_bytes()
        files["solution.py"] = code.encode()
        files["request.json"] = json.dumps(request).encode()
        if harness is not None:
            files["harness.py"] = harness.encode()

        raw = await self.backend.execute(files, timeout_s or self.settings.sandbox_timeout_s)
        return _parse(raw, self.backend.name)


def _encode(inputs: Sequence[Any]) -> list[str]:
    """Inputs travel as Python literals so tuples, sets and None survive the trip."""
    encoded = []
    for value in inputs:
        text = repr(value)
        try:
            ast.literal_eval(text)
        except (ValueError, SyntaxError) as exc:
            raise ValueError(f"Sandbox inputs must be Python literals, got {text[:100]}") from exc
        encoded.append(text)
    return encoded


def _parse(raw: RawRun, backend: str) -> RunResult:
    result = RunResult(
        backend=backend,
        exit_code=raw.exit_code,
        timed_out=raw.timed_out,
        elapsed_s=raw.elapsed_s,
    )
    _, found, payload = raw.stdout.rpartition(MARKER)
    if not found:
        # The runner never reported: killed by the time or memory limit, or crashed.
        result.stdout, result.stderr = raw.stdout, raw.stderr
        if not raw.timed_out:
            result.error = ErrorInfo(
                type="SandboxError",
                message=f"sandbox process exited with code {raw.exit_code} without a result",
                traceback=raw.stderr[-4000:],
            )
        return result

    data = json.loads(payload.strip())
    return RunResult.model_validate(result.model_dump() | {k: data[k] for k in _PAYLOAD_FIELDS})


_PAYLOAD_FIELDS = ("stdout", "stderr", "error", "value", "outcomes")


@lru_cache
def get_sandbox() -> Sandbox:
    settings = get_settings()
    backend: Backend = (
        LocalBackend() if settings.sandbox_backend == "local" else ContreeBackend(settings)
    )
    return Sandbox(backend, settings)
