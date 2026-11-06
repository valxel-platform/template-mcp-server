# template-mcp-server

The Vrixel template for an **MCP server**: it exposes a product's capabilities as tools that the
product's agents (and other MCP clients) can use, over Streamable HTTP at `/mcp`.

The example server is a care-team service: `find_patient`, `list_open_tasks`, `create_task`,
`complete_task`. Replace the store and the tools with your own and keep the conventions; the
Vrixel agent template depends on them.

## Conventions every tool follows

| Convention | Why | Where |
| --- | --- | --- |
| **Annotated.** `readOnlyHint` on reads; `destructiveHint` and `idempotentHint` on writes | Agents run read-only tools at once and ask a person before anything else. An unannotated tool counts as a write. | `server.py` |
| **Scoped.** Every tool declares a scope with `@read` or `@write` (`care:read`, `care:write`) | A reviewer's app can read; only callers allowed to change data can write. | `security.py` |
| **Idempotent writes.** Writes take `idempotency_key`; the same key and input return the first result; the same key with different input is refused | Agents and networks retry. A retry must never create a second task. Keys are per caller. | `store.py` |
| **Typed, minimal results.** Pydantic output models (structured output) with ids and what the next step needs | PHI minimization: `find_patient` returns the id and a display name, never the MRN or phone. | `server.py` |
| **Validated input.** Lengths, patterns and enums in the signature | The model sees the constraints in the schema; bad input is refused before your code runs. | `server.py` |
| **Helpful errors.** Errors say what to do next and never echo PHI | The model can correct itself ("Use find_patient to get a patient id"). | `server.py` |
| **Audited.** One audit record per call (CloudEvents, `vrixel.audit.v1`) on the product's Kafka audit topic: caller, client, tool, record ids from the arguments and the result, outcome, trace id. If the record can't be published the call returns no data | Who saw and changed which records, without argument values that may hold PHI. The platform's audit sink keeps the topic in an append-only, hash-chained table and archives it to object storage. | `security.py`, `audit.py` |
| **Traced.** OpenTelemetry: each request joins the caller's trace (`traceparent`); logs are JSON with `trace.id` | One request can be followed from the web app through agents to this server. | `telemetry.py` |
| **Rate-limited.** A token bucket per caller | One runaway agent can't starve the others. | `security.py` |
| **Flagged.** A feature's new behavior checks its flag with `flags.enabled("<key>")`; a new tool answers that it isn't available while the flag is off | The SDLC tool creates each feature's flag, off in production until release; on-call can turn it off. | `flags.py` |

## Auth

The server is an OAuth 2.0 resource server (MCP authorization spec):

- Tokens are JWTs from the product's IdP, checked for signature (JWKS), issuer, expiry and
  audience = this server's URL (`MCP_RESOURCE_URL`, RFC 8707). Tokens for another resource are refused.
- Calls without a token get 401 with a pointer to `/.well-known/oauth-protected-resource/mcp`,
  so MCP clients can discover the IdP. Tokens without the read scope get 403.
- Agents use client credentials; people's apps use authorization code with PKCE.

## Run it

```
uv sync
uv run pytest                        # starts the server on a free port and calls it over HTTP
uv run ruff check . && uv run ruff format --check . && uv run mypy
MCP_OIDC_ISSUER=... MCP_OIDC_JWKS_URL=... MCP_RESOURCE_URL=http://localhost:8080/mcp uv run mcp-server
```

| Variable | Default | |
| --- | --- | --- |
| `MCP_NAME` | `care` | Server name; agents see tools as `<name>__<tool>` |
| `MCP_RESOURCE_URL` | `http://localhost:8080/mcp` | This server's public URL, the token audience |
| `MCP_OIDC_ISSUER`, `MCP_OIDC_JWKS_URL` | | Required |
| `MCP_READ_SCOPE`, `MCP_WRITE_SCOPE` | `care:read`, `care:write` | |
| `MCP_RATE_PER_MINUTE`, `MCP_RATE_BURST` | `120`, `30` | Per caller |
| `MCP_ALLOWED_HOSTS` | `[]` | Host headers to accept (DNS rebinding protection); set in production |
| `MCP_FLAGS_URL` | | The product's Flipt, read through OpenFeature (OFREP); unset, every feature flag reads as off |
| `MCP_FLAGS_ENVIRONMENT`, `MCP_FLAGS_NAMESPACE` | | Flipt environment (e.g. `production`) and the product's namespace (e.g. `emr`) |
| `MCP_FLAGS_TOKEN` | | A Flipt client token, if the product's Flipt requires one |
| `MCP_PRODUCT`, `MCP_ENVIRONMENT`, `MCP_DATA_CLASS` | | Stamped on every audit record (`data_class`: `general`, `phi` or `payments`) |
| `MCP_AUDIT_SINK` | `log` | `kafka` in deployed environments; `log` writes JSON lines locally |
| `MCP_AUDIT_KAFKA_BOOTSTRAP`, `MCP_AUDIT_TOPIC` | `audit.v1` | The product's Kafka and its audit topic |
| `MCP_AUDIT_KAFKA_CONFIG` | `{}` | JSON of extra client settings, e.g. `{"security.protocol": "SASL_SSL", "sasl.mechanism": "SCRAM-SHA-512", ...}` |
| `MCP_AUDIT_REQUIRED` | `true` | A call whose audit record isn't acknowledged by Kafka fails |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES` | | Standard OpenTelemetry settings; point the endpoint at the data plane's collector |

The server is stateless (`stateless_http`), so any replica can serve any request. Keep it that
way: put state in the store, not in sessions.

## Errors and traces

Every response carries a W3C `traceresponse` header with its trace id (`errors.py`), which the web
and mobile apps put on the errors they report, so the SDLC tool sees a failure people hit and an
error here as one incident. With `SENTRY_DSN` set, unhandled exceptions and `ERROR` logs go to
Sentry, grouped into issues, with the exception type, stack, endpoint path and a `trace_id` tag
only: never exception messages, formatted messages, local variables, queries, headers, bodies,
users or breadcrumbs. Also `ENVIRONMENT` and `SENTRY_RELEASE`. Unset, Sentry is off; logs, traces
and metrics stay in OpenChoreo. For PHI products use a Sentry plan under a BAA, or self-hosted Sentry.

## Delivery

CI (`ci.yml`) runs `unit` (lint, format, types, tests with coverage), dependency review and an
image build; CodeQL comes from the repo's default setup. These are the required checks the
Vrixel SDLC sets on repos with the `mcp-server` role. After each deployment the SDLC sends
`vrixel-deployed`, which runs `post-deploy.yml` (`deployed/smoke.py`) against the new release.

[![M8ven Score](https://m8ven.ai/badge/mcp/vrixel-tools-template-mcp-server-14a98a?v=3128e991519e9f83b743ab7fb727b7ef)](https://m8ven.ai/mcp/vrixel-tools-template-mcp-server-14a98a?s=readme)
