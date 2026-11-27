"""Qualify native E2B suspend, resume, and fork on a disposable template."""

import argparse
import json
from pathlib import Path
from secrets import token_hex
from sys import stderr
from time import monotonic, sleep
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from e2b import Sandbox, SandboxNotFoundException, SandboxQuery, SandboxState

WORKSPACE = "/tmp/openhands-lifecycle-probe"
MARKER = f"{WORKSPACE}/workspace.txt"
PORT = 18731
TIMEOUT = 180
TRAFFIC_HEADER = "e2b-traffic-access-token"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def preview(sandbox, *, increment=False):
    method, path = ("POST", "check") if increment else ("GET", "state")
    command = f"curl --max-time 2 -fsS -X {method} http://127.0.0.1:{PORT}/{path}"
    return json.loads(sandbox.commands.run(command, timeout=5).stdout)


def start_preview(sandbox):
    sandbox.commands.run(f"mkdir -p {WORKSPACE}")
    source = Path(__file__).with_name("preview_probe.py").read_text(encoding="utf-8")
    sandbox.files.write(f"{WORKSPACE}/preview.py", source)
    sandbox.commands.run(
        f"python3 {WORKSPACE}/preview.py --listen 0.0.0.0 --port {PORT}",
        background=True,
    )
    command = (
        "for attempt in $(seq 1 20); do "
        f"curl --max-time 1 -fsS http://127.0.0.1:{PORT}/state && exit 0; "
        "sleep 0.25; done; exit 1"
    )
    sandbox.commands.run(command, timeout=30)


def require(condition, description):
    if not condition:
        raise RuntimeError(description)


def private_preview(sandbox, *, rejected_token=None):
    host = sandbox.get_host(PORT)
    require(host == f"{PORT}-{sandbox.sandbox_id}.e2b.app", "Unexpected E2B host.")
    endpoint = f"https://{host}/state"
    token = sandbox.traffic_access_token
    require(bool(token), "E2B did not provide a private ingress token.")
    opener = build_opener(NoRedirect)

    for invalid in (None, rejected_token or "invalid-test-token"):
        headers = {TRAFFIC_HEADER: invalid} if invalid else {}
        try:
            with opener.open(Request(endpoint, headers=headers), timeout=8):
                raise RuntimeError("Private E2B preview accepted unauthorized access.")
        except HTTPError as error:
            require(error.code in (401, 403), "Unexpected ingress denial status.")

    started = monotonic()
    with opener.open(
        Request(endpoint, headers={TRAFFIC_HEADER: token}), timeout=8
    ) as response:
        state = json.loads(response.read(4096))
    return state, monotonic() - started


def run_probe(template):
    sandboxes = {}
    timings = {}
    metadata = {"openhands_lifecycle_probe": token_hex(16)}
    try:
        started = monotonic()
        parent = Sandbox.create(
            template=template,
            timeout=TIMEOUT,
            metadata=metadata,
            allow_internet_access=False,
            network={"allow_public_traffic": False},
            lifecycle={"on_timeout": "kill", "auto_resume": False},
        )
        parent_id = parent.sandbox_id
        sandboxes[parent_id] = parent
        start_preview(parent)
        timings["create_to_preview_seconds"] = monotonic() - started
        parent.files.write(MARKER, "original")
        baseline = preview(parent, increment=True)
        state, elapsed = private_preview(parent)
        require(state == baseline, "Private ingress returned the wrong preview state.")
        timings["private_preview_request_seconds"] = elapsed

        started = monotonic()
        parent.pause(mode="full", strict=True)
        deadline = monotonic() + 20
        while parent.get_info(request_timeout=5).state != SandboxState.PAUSED:
            require(monotonic() < deadline, "The parent did not reach PAUSED.")
            sleep(0.25)
        timings["pause_to_settled_seconds"] = monotonic() - started

        started = monotonic()
        parent = Sandbox.connect(parent_id, timeout=TIMEOUT)
        sandboxes[parent_id] = parent
        require(parent.sandbox_id == parent_id, "Resume changed the sandbox ID.")
        require(parent.files.read(MARKER) == "original", "Resume lost the workspace.")
        require(preview(parent) == baseline, "Resume lost the preview process state.")
        timings["resume_to_preview_seconds"] = monotonic() - started
        state, elapsed = private_preview(parent)
        require(state == baseline, "Private ingress did not survive resume.")
        timings["resumed_private_preview_request_seconds"] = elapsed

        started = monotonic()
        results = parent.fork(count=2, timeout=TIMEOUT)
        timings["fork_call_seconds"] = monotonic() - started
        children = [result for result in results if isinstance(result, Sandbox)]
        for child in children:
            sandboxes[child.sandbox_id] = child
        require(
            len(children) == 2 and len(sandboxes) == 3,
            "Two unique forks are required.",
        )
        for child in children:
            require(child.files.read(MARKER) == "original", "Fork lost the workspace.")
            require(preview(child) == baseline, "Fork lost the preview process state.")
        timings["fork_to_both_previews_seconds"] = monotonic() - started

        for index, child in enumerate(children, 1):
            state, elapsed = private_preview(
                child, rejected_token=parent.traffic_access_token
            )
            require(state == baseline, "Fork private ingress returned the wrong state.")
            timings[f"fork_{index}_private_preview_request_seconds"] = elapsed

        first, second = children
        first.files.write(MARKER, "first fork")
        expected = {**baseline, "count": baseline["count"] + 1}
        require(
            preview(first, increment=True) == expected, "First fork did not advance."
        )
        for sandbox in (parent, second):
            require(
                sandbox.files.read(MARKER) == "original",
                "A fork shared workspace writes.",
            )
            require(
                preview(sandbox) == baseline, "A fork shared process memory writes."
            )
    finally:
        cleanup_failures = 0
        sandbox_ids = set(sandboxes)
        try:
            paginator = Sandbox.list(
                query=SandboxQuery(metadata=metadata), request_timeout=8
            )
            while paginator.has_next:
                sandbox_ids.update(
                    sandbox.sandbox_id for sandbox in paginator.next_items()
                )
        except Exception:  # noqa: BLE001 - inspect all cleanup results
            cleanup_failures += 1
        for sandbox_id in sandbox_ids:
            try:
                Sandbox.kill(sandbox_id, request_timeout=8)
                try:
                    Sandbox.get_info(sandbox_id, request_timeout=8)
                except SandboxNotFoundException:
                    continue
                cleanup_failures += 1
            except Exception:  # noqa: BLE001 - inspect all cleanup results
                cleanup_failures += 1
        if cleanup_failures:
            raise RuntimeError(
                f"Provider cleanup could not be verified ({cleanup_failures} failures)."
            )
        print(f"Verified terminated sandboxes: {len(sandbox_ids)}", file=stderr)

    return {name: round(seconds, 3) for name, seconds in timings.items()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--template", required=True, help="Dedicated synthetic E2B template ID"
    )
    args = parser.parse_args()
    print(json.dumps(run_probe(args.template), indent=2))
