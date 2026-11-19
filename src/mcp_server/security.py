"""Who may call which tool, how often, and the audit trail.

Every tool declares the scope it needs with @guarded. The decorator checks the caller's OAuth
token, applies the caller's rate limit, publishes one audit record per call (audit.py: who,
which tool, which records, the outcome) and records its usage (usage.py: which client and app,
which tool, how long, how it went; no person in it). Argument values are
never recorded (they may hold PHI); record ids are.

If the record can't be published the call fails: a read returns no data, and a write asks the
caller to retry with the same idempotency key (which returns the first result and records again).
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import logging
import threading
import time
from collections.abc import Callable
from typing import Any, ParamSpec, Protocol, TypeVar

import anyio
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver.exceptions import ToolError

from . import usage
from .audit import Actor, Auditor, AuditUnavailable

log = logging.getLogger("mcp_server.security")

P = ParamSpec("P")
R = TypeVar("R")

# Arguments and result fields whose values identify records rather than describe people: safe
# to audit. Maps the field to the record type.
RECORD_IDS = {"patient_id": "patient", "task_id": "task"}


def _record_ids(values: dict[str, Any], result: Any) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}

    def take(field: str, value: Any) -> None:
        if field in RECORD_IDS and value:
            found.setdefault(RECORD_IDS[field], set()).add(str(value))

    for k, v in values.items():
        take(k, v)
    items = getattr(result, "tasks", None) or ([result] if result is not None else [])
    for item in items:
        for k in RECORD_IDS:
            take(k, getattr(item, k, None))
    return found


class JwtVerifier:
    """Verifies the IdP's JWT access tokens: signature (JWKS), issuer, audience = this server."""

    def __init__(self, *, issuer: str, jwks_url: str, audience: str) -> None:
        import jwt

        if not (issuer and jwks_url):
            raise RuntimeError("Set MCP_OIDC_ISSUER and MCP_OIDC_JWKS_URL")
        self._jwt = jwt
        self.issuer, self.audience = issuer, audience
        self.jwks = jwt.PyJWKClient(jwks_url, cache_keys=True)

    def _verify(self, token: str) -> AccessToken | None:
        try:
            key = self.jwks.get_signing_key_from_jwt(token)
            claims: dict[str, Any] = self._jwt.decode(
                token, key.key, algorithms=["RS256", "ES256"], audience=self.audience, issuer=self.issuer
            )
        except self._jwt.PyJWTError:
            return None
        scope = claims.get("scope") or claims.get("scp") or ""
        return AccessToken(
            token=token,
            client_id=str(claims.get("azp") or claims.get("client_id") or ""),
            scopes=scope.split() if isinstance(scope, str) else list(scope),
            expires_at=claims.get("exp"),
            resource=self.audience,
            subject=claims.get("sub"),
            claims=claims,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        return await anyio.to_thread.run_sync(self._verify, token)


def tenants(claims: dict[str, Any], claim: str) -> list[str]:
    """The caller's tenants from the token: a name, a list of names, or Keycloak's organization
    claim (a map of organization alias to details)."""
    raw = claims.get(claim)
    if not raw:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, dict):
        return sorted(str(k) for k in raw)
    return sorted(str(v) for v in raw)


class Limiter(Protocol):
    def allow(self, key: str) -> bool: ...


class RateLimiter:
    """A token bucket per caller, in this replica: local work, and the fallback while the shared
    store can't be reached. Callers idle long enough to have a full bucket are forgotten, so memory
    stays bounded."""

    MAX_CALLERS = 10_000

    def __init__(self, per_minute: int, burst: int) -> None:
        self.rate, self.burst = per_minute / 60.0, float(burst)
        self.buckets: dict[str, tuple[float, float]] = {}
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            tokens, last = self.buckets.pop(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            allowed = tokens >= 1
            self.buckets[key] = (tokens - 1 if allowed else tokens, now)
            if len(self.buckets) > self.MAX_CALLERS:
                self._forget(now)
            return allowed

    def _forget(self, now: float) -> None:
        full = self.burst / self.rate if self.rate else float("inf")  # seconds to refill
        for k, (_, last) in list(self.buckets.items()):
            if now - last >= full:
                del self.buckets[k]
        while len(self.buckets) > self.MAX_CALLERS:  # oldest first (insertion order)
            del self.buckets[next(iter(self.buckets))]


# One caller's bucket, read and updated in one step on the store (Valkey runs a script atomically),
# on the store's clock so every replica agrees. KEYS[1] bucket; ARGV rate per ms, burst.
_BUCKET = """
local rate, burst = tonumber(ARGV[1]), tonumber(ARGV[2])
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local b = redis.call('HMGET', KEYS[1], 'tokens', 'at')
local tokens, at = tonumber(b[1]) or burst, tonumber(b[2]) or now
tokens = math.min(burst, tokens + math.max(0, now - at) * rate)
local allowed = 0
if tokens >= 1 then tokens = tokens - 1; allowed = 1 end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'at', tostring(now))
redis.call('PEXPIRE', KEYS[1], math.ceil(burst / rate) + 1000)
return allowed
"""


class SharedRateLimiter:
    """A token bucket per caller shared by every replica, in the product's Valkey (or Redis): the
    limit is the same however many replicas run. Buckets expire once full, so the store keeps
    only active callers. If the store can't be reached, each replica falls back to its own bucket
    (still limited, never unlimited) and says so in the log, at most once a minute."""

    def __init__(
        self,
        url: str,
        per_minute: int,
        burst: int,
        *,
        prefix: str,
        client: Any = None,
        fallback: RateLimiter | None = None,
    ) -> None:
        import redis

        self.rate_ms, self.burst = per_minute / 60_000.0, burst
        self.prefix = prefix
        self.client = client or redis.Redis.from_url(url, socket_timeout=0.25, socket_connect_timeout=0.25)
        self.script = self.client.register_script(_BUCKET)
        self.fallback = fallback or RateLimiter(per_minute, burst)
        self.warned_at = 0.0

    def allow(self, key: str) -> bool:
        digest = hashlib.sha256(key.encode()).hexdigest()[:32]  # no caller ids in the store's keys
        try:
            return bool(self.script(keys=[f"{self.prefix}:{digest}"], args=[self.rate_ms, self.burst]))
        except Exception as exc:  # noqa: BLE001 - the store down or slow: limit here instead
            if time.monotonic() - self.warned_at > 60:
                self.warned_at = time.monotonic()
                log.warning("rate limit store unreachable (%s): limiting per replica", type(exc).__name__)
            return self.fallback.allow(key)


def make_limiter(settings: Any) -> Limiter:
    """Shared in Valkey when MCP_RATE_LIMIT_URL is set (required outside local work and CI), else
    per replica."""
    url = settings.rate_limit_url
    if not url:
        return RateLimiter(settings.rate_per_minute, settings.rate_burst)
    if settings.environment not in ("", "local", "ci") and not url.startswith("rediss://"):
        raise RuntimeError("MCP_RATE_LIMIT_URL must be rediss:// (TLS) in deployed environments")
    return SharedRateLimiter(
        url,
        settings.rate_per_minute,
        settings.rate_burst,
        prefix=f"mcp-rate:{settings.product or 'product'}:{settings.name}",
    )


def caller() -> AccessToken:
    token = get_access_token()
    if token is None:
        raise ToolError("Not authenticated.")
    return token


def guarded(scope: str, limiter: Limiter, auditor: Auditor) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def wrap(fn: Callable[P, R]) -> Callable[P, R]:
        sig = inspect.signature(fn)
        writes = scope.endswith(":write")

        @functools.wraps(fn)
        def inner(*args: P.args, **kwargs: P.kwargs) -> R:
            token = caller()
            who = token.subject or token.client_id
            actor = Actor(
                id=who, type="service" if who == token.client_id else "user", client=token.client_id
            )
            bound = sig.bind_partial(*args, **kwargs)
            outcome, reason, result = "failure", "", None
            started = time.monotonic()
            try:
                if scope not in token.scopes:
                    raise ToolError(f"This tool needs the {scope} scope.")
                if not limiter.allow(who):
                    raise ToolError("Rate limit reached; try again shortly.")
                result = fn(*args, **kwargs)
                outcome = "success"
                return result
            except ToolError as exc:
                outcome, reason = "denied", str(exc)
                raise
            except Exception as exc:
                reason = type(exc).__name__
                raise
            finally:
                # Which client called which tool, how long it took and how it went (usage.py).
                usage.record_call(fn.__name__, token.client_id, outcome, time.monotonic() - started)
                ids = _record_ids(dict(bound.arguments), result)
                rtype = next(iter(sorted(ids)), "")
                try:
                    auditor.record(
                        f"tool.{fn.__name__}",
                        actor=actor,
                        resource=(rtype, ids.get(rtype, ())),
                        outcome=outcome,  # type: ignore[arg-type]
                        reason=reason,
                        details={
                            "records": {k: sorted(v) for k, v in ids.items()},
                            "args": sorted(bound.arguments),
                        },
                    )
                except AuditUnavailable as exc:
                    raise ToolError(
                        "This call couldn't be recorded in the audit trail, so its result is withheld."
                        + (" Retry with the same idempotency_key." if writes else " Try again shortly.")
                    ) from exc

        return inner

    return wrap
