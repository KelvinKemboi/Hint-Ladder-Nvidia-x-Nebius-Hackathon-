import asyncio
import shutil
import subprocess
import sys
from functools import cache
from types import SimpleNamespace

import pytest

from sparring_partner.config import Settings
from sparring_partner.sandbox import (
    ContreeBackend,
    DockerBackend,
    LocalBackend,
    Sandbox,
    SandboxConfigError,
)

pytestmark = pytest.mark.anyio

SETTINGS = Settings(_env_file=None, sandbox_timeout_s=30)

# Demo bug 2: `lo = mid` never advances, so some inputs loop forever.
BINARY_SEARCH = """\
def bsearch(xs, target):
    lo, hi = 0, len(xs) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if xs[mid] < target:
            lo = mid
        else:
            hi = mid
    return lo if xs and xs[lo] == target else -1


def first(xs):
    return xs[0]

print("loaded")
"""


@cache
def docker_ready() -> bool:
    """Docker is running and the sandbox image is built (not the case in CI)."""
    if shutil.which("docker") is None:
        return False
    found = subprocess.run(
        ["docker", "image", "inspect", SETTINGS.sandbox_image], capture_output=True, timeout=30
    )
    return found.returncode == 0


# Every sandbox test runs on the local backend, and on Docker when it's available.
@pytest.fixture(params=["local", "docker"])
def sandbox(request):
    if request.param == "docker":
        if not docker_ready():
            pytest.skip("Docker or the sandbox image isn't available")
        return Sandbox(DockerBackend(SETTINGS), SETTINGS)
    return Sandbox(LocalBackend(), SETTINGS)


async def test_batch_runs_every_input_and_times_out_infinite_loops(sandbox):
    result = await sandbox.run_batch(
        BINARY_SEARCH, "bsearch", [([1, 2, 3], 1), ([1, 2], 2), ([], 1)], call_timeout_s=0.3
    )

    assert result.ok
    assert result.backend == sandbox.backend.name
    assert result.stdout == "loaded\n"
    found, stuck, empty = result.outcomes
    assert found.output == "0"
    assert stuck.timed_out and stuck.output is None
    assert empty.args == "([], 1)" and empty.output == "-1"


async def test_batch_records_exceptions_with_user_line(sandbox):
    result = await sandbox.run_batch(BINARY_SEARCH, "first", [[]])

    [outcome] = result.outcomes
    assert outcome.error.type == "IndexError"
    assert outcome.error.line == 13


async def test_batch_of_500_inputs_in_one_run(sandbox):
    inputs = [(list(range(n % 7)),) for n in range(500)]
    result = await sandbox.run_batch(BINARY_SEARCH, "first", inputs)

    assert len(result.outcomes) == 500
    assert sum(o.error is not None for o in result.outcomes) == 72  # the empty lists


async def test_harness_can_run_hypothesis_in_the_sandbox(sandbox):
    harness = """\
from hypothesis import strategies as st
from sparring_partner.testing import Property, find_counterexamples

def run(solution, inputs):
    lists = st.lists(st.integers(0, 9), max_size=4).map(sorted)
    found = Property("returns an index of target", lambda a, i: i == -1 or a[0][i] == a[1])
    return find_counterexamples(
        solution.bsearch, st.tuples(lists, st.integers(0, 9)), [found],
        max_examples=50, call_timeout_s=0.2,
    )
"""
    result = await sandbox.run(BINARY_SEARCH, harness)

    assert result.ok, result.error
    [ce] = result.value
    assert ce["exception_type"] == "TimeoutError"
    assert ce["exception_line"] in (3, 4, 5, 6, 7, 8)  # inside the while loop


async def test_harness_receives_inputs(sandbox):
    harness = "def run(solution, inputs):\n    return [solution.first(xs) for xs in inputs]\n"
    result = await sandbox.run(BINARY_SEARCH, harness, inputs=[[3, 1], (7,)])
    assert result.value == [3, 7]


async def test_wall_clock_timeout_kills_the_run(sandbox):
    result = await sandbox.run("while True:\n    pass\n", timeout_s=1)

    assert result.timed_out
    assert not result.ok
    assert result.error is None


async def test_syntax_error_reports_line(sandbox):
    result = await sandbox.run("x = 1\ndef f(:\n    pass\n")

    assert result.error.type == "SyntaxError"
    assert result.error.line == 2


async def test_except_exception_in_user_code_cannot_swallow_the_timeout(sandbox):
    code = (
        "def f():\n"
        "    while True:\n"
        "        try:\n"
        "            pass\n"
        "        except Exception:\n"
        "            pass\n"
    )
    result = await sandbox.run_batch(code, "f", [()], call_timeout_s=0.2)
    assert result.outcomes[0].timed_out


async def test_memory_limit(sandbox):
    if sandbox.backend.name == "local" and sys.platform == "darwin":
        pytest.skip("macOS does not enforce RLIMIT_AS")
    result = await sandbox.run("x = bytearray(2 * 1024 ** 3)\n")
    assert result.error.type == "MemoryError"


async def test_inputs_must_be_literals(sandbox):
    with pytest.raises(ValueError, match="literals"):
        await sandbox.run_batch(BINARY_SEARCH, "first", [object()])


# --- Docker backend: what the container can't do -------------------------------


@pytest.fixture
def docker_sandbox():
    if not docker_ready():
        pytest.skip("Docker or the sandbox image isn't available")
    return Sandbox(DockerBackend(SETTINGS), SETTINGS)


def returns(expression: str) -> str:
    return f"def run(solution, inputs):\n    return {expression}\n"


async def test_docker_has_no_network(docker_sandbox):
    code = (
        "import socket\n"
        "def reach():\n"
        "    try:\n"
        "        socket.create_connection(('1.1.1.1', 53), timeout=2)\n"
        "        return 'connected'\n"
        "    except OSError as exc:\n"
        "        return type(exc).__name__\n"
    )
    result = await docker_sandbox.run(code, returns("solution.reach()"))
    assert result.ok and result.value != "connected"


async def test_docker_filesystem_is_read_only_and_user_is_unprivileged(docker_sandbox):
    code = (
        "import os\n"
        "def probe():\n"
        "    try:\n"
        "        open('/sandbox/x', 'w')\n"
        "        return 'wrote'\n"
        "    except OSError:\n"
        "        return os.getuid()\n"
    )
    result = await docker_sandbox.run(code, returns("solution.probe()"))
    assert result.value == 65534  # nobody


async def test_docker_timeout_removes_the_container(docker_sandbox):
    result = await docker_sandbox.run("while True:\n    pass\n", timeout_s=2)
    assert result.timed_out
    await asyncio.sleep(1)
    running = subprocess.run(
        ["docker", "ps", "-q", "--filter", "name=sparring-"], capture_output=True, text=True
    )
    assert running.stdout.strip() == ""


async def test_docker_missing_image_says_how_to_build_it(docker_sandbox):
    settings = Settings(_env_file=None, sandbox_image="sparring-partner-python:no-such-tag")
    with pytest.raises(SandboxConfigError, match="build_sandbox_image.py --docker"):
        await Sandbox(DockerBackend(settings), settings).run("x = 1")


# --- Token Factory backend, with a fake ConTree client ------------------------


class FakeImage:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def run(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def contree_sandbox(response):
    image = FakeImage(response)

    async def use(tag):
        image.tag = tag
        return image

    client = SimpleNamespace(images=SimpleNamespace(use=use))
    return Sandbox(ContreeBackend(SETTINGS, client=client), SETTINGS), image


def contree_result(stdout, exit_code=0, timed_out=False):
    raw = SimpleNamespace(result=SimpleNamespace(state=SimpleNamespace(timed_out=timed_out)))
    return SimpleNamespace(
        exit_code=exit_code,
        stdout=stdout,
        stderr="",
        elapsed=SimpleNamespace(total_seconds=lambda: 1.5),
        result=SimpleNamespace(_raw=raw),
    )


async def test_contree_backend_uploads_files_and_parses_result():
    payload = '{"stdout": "hi\\n", "stderr": "", "error": null, "value": 42, "outcomes": []}'
    sandbox, image = contree_sandbox(contree_result(f"noise\n@@SPARRING_RESULT@@{payload}\n"))

    result = await sandbox.run("print('hi')", inputs=[1])

    assert result.ok and result.value == 42 and result.stdout == "hi\n"
    assert result.backend == "contree" and result.elapsed_s == 1.5
    assert image.tag == SETTINGS.sandbox_image
    args, kwargs = image.calls[0]
    assert args == ("python3",)
    assert kwargs["args"] == ["-I", "/sandbox/_runner.py"]
    assert kwargs["timeout"] == 30
    files = kwargs["files"]
    assert files["/sandbox/solution.py"] == b"print('hi')"
    assert {
        "/sandbox/_runner.py",
        "/sandbox/harness.py",
        "/sandbox/request.json",
        "/sandbox/sparring_partner/testing/capture.py",
    } <= files.keys()


async def test_contree_process_timeout():
    sandbox, _ = contree_sandbox(contree_result("", exit_code=137, timed_out=True))
    result = await sandbox.run("while True: pass")
    assert result.timed_out and result.error is None


async def test_contree_operation_timeout():
    from contree_sdk.sdk.exceptions import OperationTimedOutError

    sandbox, _ = contree_sandbox(OperationTimedOutError(operation_uuid=None))
    result = await sandbox.run("while True: pass")
    assert result.timed_out


async def test_runner_crash_without_result_is_a_sandbox_error():
    sandbox, _ = contree_sandbox(contree_result("Killed", exit_code=137))
    result = await sandbox.run("x = 1")
    assert result.error.type == "SandboxError"
    assert "137" in result.error.message


async def test_contree_requires_project_id():
    backend = ContreeBackend(Settings(_env_file=None, nebius_api_key="k", nebius_project_id=""))
    with pytest.raises(SandboxConfigError, match="NEBIUS_PROJECT_ID"):
        await backend.execute({}, 1)
