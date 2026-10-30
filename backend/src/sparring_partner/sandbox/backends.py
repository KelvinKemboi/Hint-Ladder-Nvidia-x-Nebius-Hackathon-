"""Where sandbox runs execute: the Token Factory sandbox, a local Docker container, or a
local subprocess for dev."""

import asyncio
import contextlib
import logging
import os
import signal
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sparring_partner.config import Settings

logger = logging.getLogger(__name__)

RUNNER = "_runner.py"
WORKDIR = "/sandbox"
MAX_OUTPUT = 4 * 1024 * 1024


class SandboxConfigError(Exception):
    pass


@dataclass
class RawRun:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    elapsed_s: float


class Backend(Protocol):
    name: str

    async def execute(self, files: dict[str, bytes], timeout_s: float) -> RawRun:
        """Write `files` (relative path -> content) to a fresh workdir and run the runner."""
        ...


class LocalBackend:
    """Runs in a subprocess on this machine. NOT isolated: development only."""

    name = "local"

    def __init__(self) -> None:
        logger.warning("Sandbox backend is 'local': user code runs unisolated on this machine")

    async def execute(self, files: dict[str, bytes], timeout_s: float) -> RawRun:
        with tempfile.TemporaryDirectory(prefix="sparring-") as workdir:
            for path, content in files.items():
                target = Path(workdir, path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)

            start = time.perf_counter()
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                RUNNER,
                cwd=workdir,
                env={"PATH": os.environ.get("PATH", ""), "PYTHONHASHSEED": "0"},
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # own process group, so a timeout kills any children too
            )
            timed_out = False
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout_s)
            except TimeoutError:
                timed_out = True
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                stdout, stderr = await proc.communicate()
            return RawRun(
                exit_code=None if timed_out else proc.returncode,
                stdout=stdout[-MAX_OUTPUT:].decode(errors="replace"),
                stderr=stderr[-MAX_OUTPUT:].decode(errors="replace"),
                timed_out=timed_out,
                elapsed_s=time.perf_counter() - start,
            )


class DockerBackend:
    """A fresh container per run: no network, capped memory, CPU and processes, a read-only
    filesystem, no capabilities, and an unprivileged user. Isolated, but on this machine:
    a stand-in for the Token Factory sandbox (scripts/build_sandbox_image.py --docker)."""

    name = "docker"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def command(self, workdir: str, container: str) -> list[str]:
        mb = self.settings.sandbox_memory_mb
        return [
            "docker", "run", "--rm", "--name", container, "--pull", "never",
            "--network", "none",
            f"--memory={mb}m", f"--memory-swap={mb}m", "--cpus=1", "--pids-limit=64",
            "--read-only", "--tmpfs", "/tmp:size=64m",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user=65534:65534",
            "-v", f"{workdir}:{WORKDIR}:ro", "-w", WORKDIR,
            "-e", "PYTHONHASHSEED=0", "-e", "PYTHONDONTWRITEBYTECODE=1",
            self.settings.sandbox_image, "python3", "-I", RUNNER,
        ]  # fmt: skip

    async def execute(self, files: dict[str, bytes], timeout_s: float) -> RawRun:
        with tempfile.TemporaryDirectory(prefix="sparring-") as workdir:
            os.chmod(workdir, 0o755)  # readable by the container's unprivileged user
            for path, content in files.items():
                target = Path(workdir, path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                target.chmod(0o644)

            container = f"sparring-{uuid.uuid4().hex[:12]}"
            start = time.perf_counter()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self.command(workdir, container),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                raise SandboxConfigError(
                    "SANDBOX_BACKEND=docker, but the docker command isn't installed"
                ) from None
            timed_out = False
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout_s)
            except TimeoutError:
                timed_out = True
                # Killing the docker client would leave the container running.
                killer = await asyncio.create_subprocess_exec(
                    "docker", "kill", container,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )  # fmt: skip
                await killer.wait()
                stdout, stderr = await proc.communicate()

            error = stderr.decode(errors="replace")
            if proc.returncode == 125:  # docker itself failed, before running anything
                raise SandboxConfigError(_docker_problem(error, self.settings.sandbox_image))
            return RawRun(
                exit_code=None if timed_out else proc.returncode,
                stdout=stdout[-MAX_OUTPUT:].decode(errors="replace"),
                stderr=error[-MAX_OUTPUT:],
                timed_out=timed_out,
                elapsed_s=time.perf_counter() - start,
            )


def _docker_problem(stderr: str, image: str) -> str:
    if "Cannot connect to the Docker daemon" in stderr:
        return "SANDBOX_BACKEND=docker, but Docker isn't running: start Docker Desktop"
    if "No such image" in stderr or "Unable to find image" in stderr:
        return (
            f"Docker image {image} doesn't exist yet. Build it once: "
            "cd backend && uv run python scripts/build_sandbox_image.py --docker"
        )
    return f"Docker couldn't start the sandbox container: {stderr.strip()[:300]}"


class ContreeBackend:
    """Nebius Token Factory sandbox (ConTree): a fresh disposable VM per run."""

    name = "contree"

    def __init__(self, settings: Settings, client: Any = None) -> None:
        self.settings = settings
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            from contree_sdk import Contree
            from contree_sdk.auth import IAMAuth
            from contree_sdk.config import ContreeConfig

            key = self.settings.nebius_api_key.get_secret_value()
            if not key or not self.settings.nebius_project_id:
                raise SandboxConfigError(
                    "Token Factory sandbox needs NEBIUS_API_KEY and NEBIUS_PROJECT_ID "
                    "(or set SANDBOX_BACKEND=local for development)"
                )
            auth = IAMAuth(token=key, project_id=self.settings.nebius_project_id)
            self._client = Contree(config=ContreeConfig(auth=auth))
        return self._client

    async def execute(self, files: dict[str, bytes], timeout_s: float) -> RawRun:
        from contree_sdk.sdk.exceptions import OperationTimedOutError

        image = await self.client.images.use(self.settings.sandbox_image)
        start = time.perf_counter()
        try:
            result = await image.run(
                "python3",
                args=["-I", f"{WORKDIR}/{RUNNER}"],
                cwd=WORKDIR,
                env={"PYTHONHASHSEED": "0"},
                files={f"{WORKDIR}/{path}": content for path, content in files.items()},
                timeout=timeout_s,
                truncate_output_at=MAX_OUTPUT,
            )
        except OperationTimedOutError:
            return RawRun(None, "", "", timed_out=True, elapsed_s=time.perf_counter() - start)

        return RawRun(
            exit_code=result.exit_code,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
            timed_out=_process_timed_out(result),
            elapsed_s=result.elapsed.total_seconds(),
        )


def _process_timed_out(result: Any) -> bool:
    # The SDK exposes this only on the raw operation metadata.
    try:
        return bool(result.result._raw.result.state.timed_out)
    except AttributeError:
        return False
