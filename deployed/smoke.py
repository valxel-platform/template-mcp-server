"""Checks a deployed MCP server and writes JUnit for the post-deploy report.

Always: it is up, it refuses calls without a token, and it publishes its OAuth protected-resource
metadata. With SMOKE_TOKEN (a client-credentials token with the read scope): its tools are listed
and every tool declares whether it is read-only. Makes no writes, so it is safe in production.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import anyio
import httpx


def list_tools(url: str, token: str) -> list[Any]:
    from mcp.client.client import Client
    from mcp.client.streamable_http import (  # type: ignore[attr-defined]
        create_mcp_http_client,
        streamable_http_client,
    )

    async def go() -> list[Any]:
        http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
        async with Client(streamable_http_client(url, http_client=http)) as c:
            return list((await c.list_tools()).tools)

    return anyio.run(go)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True, help="e.g. https://care-mcp.dev.example.com")
    ap.add_argument("--junit", default="reports/deployed-smoke.xml")
    args = ap.parse_args(argv)
    base = args.base_url.rstrip("/")
    mcp_url = f"{base}/mcp"
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    accept = {"Accept": "application/json, text/event-stream"}
    checks: dict[str, Callable[[], bool]] = {
        "healthz": lambda: httpx.get(f"{base}/healthz", timeout=10).status_code == 200,
        "calls need a token": lambda: (
            httpx.post(mcp_url, json=rpc, headers=accept, timeout=10).status_code == 401
        ),
        "protected-resource metadata": lambda: (
            httpx.get(f"{base}/.well-known/oauth-protected-resource/mcp", timeout=10).status_code == 200
        ),
    }
    token = os.environ.get("SMOKE_TOKEN", "")
    if token:
        checks["tools listed and annotated"] = lambda: all(
            t.annotations is not None and t.annotations.read_only_hint is not None
            for t in list_tools(mcp_url, token)
        )
    suite = ET.Element("testsuite", name="smoke")
    failed = 0
    for name, check in checks.items():
        t0 = time.monotonic()
        try:
            ok = check()
        except Exception:  # noqa: BLE001  (an unreachable server is a failed check)
            ok = False
        tc = ET.SubElement(
            suite, "testcase", classname="smoke", name=name, time=f"{time.monotonic() - t0:.3f}"
        )
        if not ok:
            failed += 1
            ET.SubElement(tc, "failure", message=f"{name} failed at {base}")
        print(f"{'pass' if ok else 'FAIL'}  {name}")
    suite.set("tests", str(len(checks)))
    suite.set("failures", str(failed))
    Path(args.junit).parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(suite).write(args.junit, encoding="utf-8", xml_declaration=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
