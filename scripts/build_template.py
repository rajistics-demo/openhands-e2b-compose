"""Build a deferred-init E2B template for the Enterprise 1.88.0 adapter."""

import argparse
import asyncio
import re
from pathlib import Path

from e2b import AsyncTemplate, wait_for_port
from e2b.exceptions import (
    AuthenticationException,
    BuildException,
    SandboxException,
    ServiceBusyException,
)


async def build():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--init-key-file", type=Path, required=True)
    parser.add_argument(
        "--image", default="ghcr.io/openhands/agent-server:1.60.1-python"
    )
    parser.add_argument("--cpu-count", type=int, default=2)
    parser.add_argument("--memory-mb", type=int, default=2048)
    args = parser.parse_args()
    key = args.init_key_file.read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        parser.error("generate a private bootstrap key with openssl rand -hex 32")
    if args.cpu_count <= 0 or args.memory_mb <= 0:
        parser.error("CPU count and memory must be positive")
    command = (
        f"OH_DEFERRED_INIT=1 OH_SECRET_KEY={key} "
        "/usr/local/bin/openhands-agent-server --host 0.0.0.0 --port 8000"
    )
    template = (
        AsyncTemplate()
        .from_image(args.image)
        .set_user("root")
        .run_cmd("apt-get update -qq && apt-get install -y -qq iproute2")
        .set_envs({"OH_DEFERRED_INIT": "1", "OH_SECRET_KEY": key})
        .set_start_cmd(command, wait_for_port(8000))
    )
    # Provider error payloads and build logs can contain the build command.
    try:
        result = await AsyncTemplate.build(
            template,
            name=args.name,
            cpu_count=args.cpu_count,
            memory_mb=args.memory_mb,
        )
    except (
        AuthenticationException,
        BuildException,
        SandboxException,
        ServiceBusyException,
    ) as error:
        raise SystemExit(
            f"E2B template build failed ({type(error).__name__})."
        ) from None
    print(result.template_id)


if __name__ == "__main__":
    asyncio.run(build())
