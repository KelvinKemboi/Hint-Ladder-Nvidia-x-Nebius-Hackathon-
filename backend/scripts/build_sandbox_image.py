"""Build the sandbox image: Python 3.12 + Hypothesis, tagged SANDBOX_IMAGE.

Token Factory sandbox (run once per account, and again after bumping Hypothesis):
    uv run python scripts/build_sandbox_image.py
    uv run python scripts/build_sandbox_image.py --check
Local Docker instead (SANDBOX_BACKEND=docker), e.g. until you have Sandboxes access:
    uv run python scripts/build_sandbox_image.py --docker
    uv run python scripts/build_sandbox_image.py --docker --check
"""

import argparse
import asyncio
import subprocess
from importlib.metadata import version

from sparring_partner.config import get_settings
from sparring_partner.sandbox import ContreeBackend, DockerBackend, Sandbox

BASE_IMAGE = "python:3.12-slim"


async def build() -> None:
    settings = get_settings()
    client = ContreeBackend(settings).client
    hypothesis = f"hypothesis=={version('hypothesis')}"  # same version the backend tests use

    print(f"Importing {BASE_IMAGE} ...")
    base = await client.images.oci(BASE_IMAGE)
    print(f"Installing {hypothesis} and tagging {settings.sandbox_image} ...")
    image = await base.run(
        shell=f"pip install --no-cache-dir '{hypothesis}' && python3 -c 'import hypothesis'",
        disposable=False,
        tag=settings.sandbox_image,
        timeout=600,
    )
    if image.exit_code != 0:
        raise SystemExit(f"Build failed (exit {image.exit_code}):\n{image.stderr}")
    print(f"Built {settings.sandbox_image} ({image.uuid})")


def build_docker() -> None:
    settings = get_settings()
    hypothesis = f"hypothesis=={version('hypothesis')}"
    dockerfile = (
        f"FROM {BASE_IMAGE}\n"
        f"RUN pip install --no-cache-dir '{hypothesis}' && python3 -c 'import hypothesis'\n"
    )
    print(f"Building {settings.sandbox_image} ({BASE_IMAGE} + {hypothesis}) with Docker ...")
    subprocess.run(
        ["docker", "build", "-t", settings.sandbox_image, "-"],
        input=dockerfile.encode(),
        check=True,
    )
    print(f"Built {settings.sandbox_image}. Set SANDBOX_BACKEND=docker in .env to use it.")


async def check(docker: bool) -> None:
    settings = get_settings()
    backend = DockerBackend(settings) if docker else ContreeBackend(settings)
    sandbox = Sandbox(backend, settings)
    harness = "import hypothesis\ndef run(solution, inputs):\n    return hypothesis.__version__\n"
    result = await sandbox.run("x = 1", harness)
    print(result.model_dump_json(indent=2))
    batch = await sandbox.run_batch(
        "def f(n):\n    while n:\n        pass\n", "f", [0, 1], call_timeout_s=0.5
    )
    print([(o.args, o.output, o.timed_out) for o in batch.outcomes])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="run a smoke test instead")
    parser.add_argument("--docker", action="store_true", help="use local Docker, not Token Factory")
    args = parser.parse_args()
    if args.check:
        asyncio.run(check(args.docker))
    elif args.docker:
        build_docker()
    else:
        asyncio.run(build())
