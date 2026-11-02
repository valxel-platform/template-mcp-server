"""Who may call which tool, how often, and the audit trail.

Every tool declares the scope it needs with @guarded. The decorator checks the caller's OAuth
token, applies the caller's rate limit and writes one audit record per call: who, which tool,
which records, the outcome. Argument values are not logged (they may hold PHI); record ids are.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar

import anyio
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver.exceptions import ToolError

audit_log = logging.getLogger("audit")
P = ParamSpec("P")
R = TypeVar("R")

# Arguments whose values identify records rather than describe people: safe to audit.
RECORD_ID_ARGS = {"patient_id", "task_id"}


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


class RateLimiter:
    """A token bucket per caller."""

    def __init__(self, per_minute: int, burst: int) -> None:
        self.rate, self.burst = per_minute / 60.0, float(burst)
        self.buckets: dict[str, tuple[float, float]] = {}
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            tokens, last = self.buckets.get(key, (self.burst, now))
            tokens = min(self.burst, tokens + (now - last) * self.rate)
            if tokens < 1:
                self.buckets[key] = (tokens, now)
                return False
            self.buckets[key] = (tokens - 1, now)
            return True


def caller() -> AccessToken:
    token = get_access_token()
    if token is None:
        raise ToolError("Not authenticated.")
    return token


def guarded(scope: str, limiter: RateLimiter) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def wrap(fn: Callable[P, R]) -> Callable[P, R]:
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def inner(*args: P.args, **kwargs: P.kwargs) -> R:
            token = caller()
            who = token.subject or token.client_id
            bound = sig.bind_partial(*args, **kwargs)
            record: dict[str, Any] = {
                "at": time.time(),
                "tool": fn.__name__,
                "subject": who,
                "client_id": token.client_id,
                "records": {k: v for k, v in bound.arguments.items() if k in RECORD_ID_ARGS},
                "args": sorted(bound.arguments),
            }
            try:
                if scope not in token.scopes:
                    raise ToolError(f"This tool needs the {scope} scope.")
                if not limiter.allow(who):
                    raise ToolError("Rate limit reached; try again shortly.")
                result = fn(*args, **kwargs)
                record["outcome"] = "ok"
                return result
            except ToolError as exc:
                record["outcome"], record["error"] = "refused", str(exc)
                raise
            except Exception as exc:
                record["outcome"], record["error"] = "error", type(exc).__name__
                raise
            finally:
                audit_log.info(json.dumps(record, default=str))

        return inner

    return wrap
