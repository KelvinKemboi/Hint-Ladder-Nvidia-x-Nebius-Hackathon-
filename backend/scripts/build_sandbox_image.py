"""Build the Token Factory sandbox image: Python 3.12 + Hypothesis, tagged SANDBOX_IMAGE.

Run once per account (and again after bumping Hypothesis):
    uv run python scripts/build_sandbox_image.py
Then check it with a real run:
    uv run python scripts/build_sandbox_image.py --check
"""

import argparse
import asyncio
from importlib.metadata import version

from sparring_partner.config import get_settings
from sparring_partner.sandbox import ContreeBackend, Sandbox

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


async def check() -> None:
    settings = get_settings()
    sandbox = Sandbox(ContreeBackend(settings), settings)
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
    asyncio.run(check() if parser.parse_args().check else build())
