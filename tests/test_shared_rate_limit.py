"""The rate limit shared by every replica in the product's Valkey (security.SharedRateLimiter).
Runs against MCP_TEST_REDIS_URL (CI starts a Valkey); the fallback runs anywhere."""

from __future__ import annotations

import os
import time
import uuid
from types import SimpleNamespace

import pytest

from mcp_server.security import RateLimiter, SharedRateLimiter, make_limiter

URL = os.environ.get("MCP_TEST_REDIS_URL", "")
needs_store = pytest.mark.skipif(not URL, reason="set MCP_TEST_REDIS_URL to test against Valkey")


def replica(prefix: str, per_minute: int = 60, burst: int = 3) -> SharedRateLimiter:
    return SharedRateLimiter(URL, per_minute, burst, prefix=prefix)


@needs_store
def test_replicas_share_one_bucket_per_caller() -> None:
    prefix = f"test:{uuid.uuid4().hex}"
    a, b = replica(prefix), replica(prefix)  # two replicas, their own clients
    assert [a.allow("nurse-7"), b.allow("nurse-7"), a.allow("nurse-7")] == [True, True, True]
    assert not b.allow("nurse-7") and not a.allow("nurse-7")  # the burst is spent, whichever replica
    assert a.allow("nurse-9")  # another caller has their own bucket
    keys = a.client.keys(f"{prefix}:*")
    assert len(keys) == 2 and not any(b"nurse" in k for k in keys)  # no caller ids in the store
    assert 0 < a.client.pttl(keys[0]) <= 4000  # full after 3 s: forgotten soon after


@needs_store
def test_the_bucket_refills() -> None:
    limiter = replica(f"test:{uuid.uuid4().hex}", per_minute=6000, burst=1)  # 100 a second
    assert limiter.allow("x") and not limiter.allow("x")
    time.sleep(0.03)
    assert limiter.allow("x")


class Down:
    def register_script(self, _script: str) -> object:
        def run(**_: object) -> int:
            raise ConnectionError("store unreachable")

        return run


def test_an_unreachable_store_falls_back_to_this_replica(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("mcp_server.security.time.monotonic", lambda: 5.0)  # a machine booted 5 s ago
    limiter = SharedRateLimiter("redis://unused", 60, 2, prefix="p", client=Down())
    with caplog.at_level("WARNING"):
        assert [limiter.allow("c") for _ in range(3)] == [True, True, False]  # limited, never unlimited
    assert sum("unreachable" in r.message for r in caplog.records) == 1  # said once, not per call


def test_which_limiter() -> None:
    def settings(**kw: object) -> SimpleNamespace:
        return SimpleNamespace(
            **{
                "rate_limit_url": "",
                "rate_per_minute": 60,
                "rate_burst": 5,
                "environment": "production",
                "product": "emr",
                "name": "care",
                **kw,
            }
        )

    assert isinstance(make_limiter(settings()), RateLimiter)
    with pytest.raises(RuntimeError, match="rediss://"):
        make_limiter(settings(rate_limit_url="redis://valkey:6379"))
    shared = make_limiter(settings(rate_limit_url="rediss://:pw@valkey:6379"))
    assert isinstance(shared, SharedRateLimiter) and shared.prefix == "mcp-rate:emr:care"
    assert isinstance(
        make_limiter(settings(environment="local", rate_limit_url="redis://localhost:6379")),
        SharedRateLimiter,
    )
