"""The product's capabilities as MCP tools.

Conventions every tool follows (agents rely on them):
- Annotations say what the tool does: readOnlyHint for reads (agents run them without asking),
  destructiveHint/idempotentHint for writes (agents ask a person first).
- Writes take an `idempotency_key`; the same key and input returns the first result.
- Results are typed (structured output) and minimal: ids and what the next step needs, never
  identifiers or contact details the caller did not ask for.
- Errors say what to do next, and never echo PHI.
"""

from __future__ import annotations

from typing import Literal

from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from .audit import Auditor, make_sink
from .security import RateLimiter, caller, guarded, tenants
from .settings import Settings
from .store import Conflict, NotFound, Store

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
CREATE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
UPDATE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)

IdempotencyKey = Field(
    default="",
    max_length=200,
    description="Repeat a call with the same key to get the first result instead of a new change.",
)


class PatientMatch(BaseModel):
    found: bool
    patient_id: str | None = None
    display_name: str | None = None


class TaskOut(BaseModel):
    task_id: str
    patient_id: str
    title: str
    priority: str
    status: str


class TaskList(BaseModel):
    tasks: list[TaskOut]


def _task(t: object) -> TaskOut:
    return TaskOut.model_validate(t, from_attributes=True)


def make_auditor(settings: Settings) -> Auditor:
    sink = make_sink(
        settings.audit_sink,
        bootstrap=settings.audit_kafka_bootstrap,
        topic=settings.audit_topic,
        config=dict(settings.audit_kafka_config),
        data_class=settings.data_class,
    )
    return Auditor(
        sink,
        service=settings.name,
        product=settings.product,
        environment=settings.environment,
        data_class=settings.data_class,
        required=settings.audit_required,
    )


def build_server(
    settings: Settings,
    *,
    verifier: TokenVerifier,
    store: Store | None = None,
    auditor: Auditor | None = None,
) -> MCPServer:
    store = store or Store.seeded()
    auditor = auditor or make_auditor(settings)
    limiter = RateLimiter(settings.rate_per_minute, settings.rate_burst)
    read = guarded(settings.read_scope, limiter, auditor)
    write = guarded(settings.write_scope, limiter, auditor)
    server = MCPServer(
        settings.name,
        instructions="Care-team data: find patients and manage their tasks.",
        token_verifier=verifier,
        auth=AuthSettings(
            issuer_url=settings.oidc_issuer or "https://issuer.invalid",  # type: ignore[arg-type]
            resource_server_url=settings.resource_url,  # type: ignore[arg-type]
            required_scopes=[settings.read_scope],
            validate_token_resource=True,
        ),
    )

    def who() -> str:
        t = caller()
        return t.subject or t.client_id

    def mine() -> set[str] | None:
        """The caller's tenants; every tool reads and writes only their records."""
        if not settings.tenant_claim:
            return None  # a single-tenant product
        found = set(tenants(getattr(caller(), "claims", None) or {}, settings.tenant_claim))
        if not found:
            raise ToolError("Your token names no organization, so no records are available to you.")
        return found

    @server.tool(annotations=READ)
    @read
    def find_patient(
        name: str = Field(max_length=200),
        date_of_birth: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="YYYY-MM-DD"),
    ) -> PatientMatch:
        """Find a patient by full name and date of birth. Returns the patient id to use with other tools."""
        p = store.find_patient(name, date_of_birth, mine())
        return (
            PatientMatch(found=False)
            if p is None
            else PatientMatch(found=True, patient_id=p.patient_id, display_name=p.name)
        )

    @server.tool(annotations=READ)
    @read
    def list_open_tasks(patient_id: str) -> TaskList:
        """Open care-team tasks for a patient."""
        try:
            return TaskList(tasks=[_task(t) for t in store.open_tasks(patient_id, mine())])
        except NotFound as exc:
            raise ToolError(f"{exc} Use find_patient to get a patient id.") from exc

    @server.tool(annotations=CREATE)
    @write
    def create_task(
        patient_id: str,
        title: str = Field(min_length=3, max_length=200),
        priority: Literal["routine", "urgent"] = "routine",
        idempotency_key: str = IdempotencyKey,
    ) -> TaskOut:
        """Create a care-team task for a patient. Changes data."""
        try:
            return _task(store.create_task(who(), idempotency_key, patient_id, title, priority, mine()))
        except NotFound as exc:
            raise ToolError(f"{exc} Use find_patient to get a patient id.") from exc
        except Conflict as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(annotations=UPDATE)
    @write
    def complete_task(task_id: str, idempotency_key: str = IdempotencyKey) -> TaskOut:
        """Mark a task done. Changes data."""
        try:
            return _task(store.complete_task(who(), idempotency_key, task_id, mine()))
        except (NotFound, Conflict) as exc:
            raise ToolError(str(exc)) from exc

    return server
