from __future__ import annotations

import time
from typing import Any

import anyio
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from mcp_server.security import JwtVerifier, RateLimiter


class FakeJwks:
    def __init__(self, key: Any) -> None:
        self.key = key

    def get_signing_key_from_jwt(self, token: str) -> Any:
        return type("K", (), {"key": self.key})()


@pytest.fixture
def keys() -> tuple[Any, JwtVerifier]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    v = JwtVerifier(issuer="https://idp.test", jwks_url="https://idp.test/jwks", audience="https://care/mcp")
    v.jwks = FakeJwks(private.public_key())  # type: ignore[assignment]
    return private, v


def token(private: Any, **claims: Any) -> str:
    base = {
        "iss": "https://idp.test",
        "aud": "https://care/mcp",
        "sub": "nurse-7",
        "azp": "intake",
        "scope": "care:read care:write",
        "exp": int(time.time()) + 60,
    }
    return jwt.encode({**base, **claims}, private, algorithm="RS256")


def test_valid_token(keys: tuple[Any, JwtVerifier]) -> None:
    private, v = keys
    at = anyio.run(v.verify_token, token(private))
    assert at is not None and at.subject == "nurse-7" and at.scopes == ["care:read", "care:write"]
    assert at.client_id == "intake" and at.resource == "https://care/mcp"


@pytest.mark.parametrize(
    "claims", [{"aud": "https://other/mcp"}, {"iss": "https://evil"}, {"exp": int(time.time()) - 10}]
)
def test_wrong_audience_issuer_or_expired(keys: tuple[Any, JwtVerifier], claims: dict[str, Any]) -> None:
    private, v = keys
    assert anyio.run(v.verify_token, token(private, **claims)) is None


def test_issuer_is_required() -> None:
    with pytest.raises(RuntimeError, match="OIDC"):
        JwtVerifier(issuer="", jwks_url="", audience="x")


def test_rate_limiter_refills() -> None:
    rl = RateLimiter(per_minute=6000, burst=1)
    assert rl.allow("a") and not rl.allow("a")
    assert rl.allow("b")
    time.sleep(0.02)
    assert rl.allow("a")


def test_the_rate_limiter_forgets_idle_callers() -> None:
    from mcp_server.security import RateLimiter

    limiter = RateLimiter(per_minute=60, burst=5)
    limiter.MAX_CALLERS = 100
    for n in range(250):
        limiter.allow(f"caller-{n}")
    assert len(limiter.buckets) <= 100


def test_phi_audit_records_never_travel_in_plain_text() -> None:
    from mcp_server.audit import make_sink

    with pytest.raises(RuntimeError, match="must use TLS"):
        make_sink("kafka", bootstrap="kafka:9092", topic="audit.v1", data_class="phi")
