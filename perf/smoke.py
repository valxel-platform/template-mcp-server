"""Performance smoke test for pull requests: the server's tools under a steady load, in-process
over Streamable HTTP, with the rate limit raised and an in-memory audit trail (what's measured is
the server's own work). Writes the 95th and 99th percentile call times, error rate and calls per
second as JSON for perf/compare.py. The workflow runs it on main's code and on the PR's, the same way.

usage: uv run python perf/smoke.py out.json [--seconds 60] [--concurrency 8]
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import statistics
import threading
import time
from typing import Any

import anyio
import uvicorn
from mcp.server.auth.provider import AccessToken

from mcp_server.app import create_app
from mcp_server.audit import Auditor, MemorySink
from mcp_server.settings import Settings
from mcp_server.store import Store

RESOURCE = "http://127.0.0.1/mcp"
CLAIMS = {"organization": {"north-clinic": {}}}


class Verifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        kw: dict[str, Any] = {"claims": CLAIMS} if "claims" in AccessToken.model_fields else {}
        return AccessToken(
            token=token,
            client_id="perf",
            scopes=["care:read", "care:write"],
            subject="perf",
            resource=RESOURCE,
            **kw,
        )


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def serve() -> tuple[str, uvicorn.Server]:
    port = free_port()
    settings = Settings(
        resource_url=RESOURCE, oidc_issuer="https://idp.test", rate_per_minute=10**9, rate_burst=10**9
    )
    auditor = Auditor(MemorySink(), service="care", product="perf", environment="ci", data_class="phi")
    app = create_app(settings, verifier=Verifier(), store=Store.seeded(), auditor=auditor)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}/mcp", server


# The calls an agent makes most: find a patient, then list their tasks. Replace with your tools.
CALLS = [
    ("find_patient", {"name": "Ana Lopez", "date_of_birth": "1984-03-02"}),
    ("list_open_tasks", {"patient_id": "p-102"}),
]


async def worker(url: str, until: float, times: list[float], errors: list[int]) -> None:
    from mcp.client.client import Client
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client  # type: ignore[attr-defined]

    http = create_mcp_http_client(headers={"Authorization": "Bearer perf"})
    async with Client(streamable_http_client(url, http_client=http)) as c:
        i = 0
        while time.monotonic() < until:
            name, args = CALLS[i % len(CALLS)]
            i += 1
            start = time.perf_counter()
            try:
                res = await c.call_tool(name, args)
                if getattr(res, "is_error", False) or getattr(res, "isError", False):
                    errors.append(1)
            except Exception:  # noqa: BLE001 (any failure counts as an error)
                errors.append(1)
            times.append((time.perf_counter() - start) * 1000)


async def load(url: str, seconds: float, concurrency: int, warmup: float) -> dict:
    warm: list[float] = []
    async with anyio.create_task_group() as tg:
        for _ in range(concurrency):
            tg.start_soon(worker, url, time.monotonic() + warmup, warm, [])
    times: list[float] = []
    errors: list[int] = []
    started = time.monotonic()
    async with anyio.create_task_group() as tg:
        for _ in range(concurrency):
            tg.start_soon(worker, url, started + seconds, times, errors)
    elapsed = time.monotonic() - started
    q = statistics.quantiles(times, n=100) if len(times) >= 100 else [max(times or [0.0])] * 99
    return {
        "p95_ms": round(q[94], 2),
        "p99_ms": round(q[98], 2),
        "error_rate": round(len(errors) / max(1, len(times)), 4),
        "rps": round(len(times) / elapsed, 1),
        "calls": len(times),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--warmup", type=float, default=10)
    a = ap.parse_args()
    url, server = serve()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        result = anyio.run(load, url, a.seconds, a.concurrency, a.warmup)
    finally:
        server.should_exit = True
    with open(a.out, "w") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
