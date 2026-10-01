"""Phase 3: delegate tool steps to specialist agents over A2A.

    Coordinator                                   Customer Agent / Order Agent
    -----------                                   ----------------------------
    AgentDirectory.refresh()  --GET /.well-known/agent-card.json-->  Agent Card (skills)
    A2AExecutor.execute(step) --A2A SendMessage (DataPart {skill,args})-->  Task
                              <--Task: state + artifact (ToolResult)--

Discovery: the coordinator only knows the agents' base URLs. Which agent
handles which step is decided from the *skills advertised in the Agent Cards*,
not hard-coded. Cards are cached for CARD_TTL seconds and re-fetched when an
agent was unreachable or a skill is missing, so a restarted agent is picked up
again automatically.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx
from google.protobuf.json_format import MessageToDict

from a2a.client import A2ACardResolver, A2AClientError, A2AClientTimeoutError, ClientConfig, create_client
from a2a.helpers import get_data_parts, get_message_text, new_data_message
from a2a.types import AgentCard, GetTaskRequest, Role, SendMessageRequest, TaskState

from comco_agentkit import ContractViolation, downstream_fault_headers, validate_tool_data
from comco_common import get_correlation_id, get_logger

from .planner import PlannedStep

log = get_logger(__name__)

CARD_TTL = 30.0


@dataclass
class AgentEntry:
    base_url: str
    card: AgentCard | None = None
    error: str | None = None
    fetched_at: float = 0.0
    skills: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.card.name if self.card else self.base_url

    @property
    def reachable(self) -> bool:
        return self.card is not None and self.error is None

    def summary(self) -> dict:
        out = {"base_url": self.base_url, "reachable": self.reachable, "error": self.error}
        if self.card:
            card = MessageToDict(self.card)
            out.update(
                name=card.get("name"),
                description=card.get("description"),
                version=card.get("version"),
                a2a_endpoint=(card.get("supportedInterfaces") or [{}])[0].get("url"),
                skills=[{"id": s["id"], "description": s.get("description")} for s in card.get("skills", [])],
            )
        return out


def _correlation_hook(request: httpx.Request) -> None:
    cid = get_correlation_id()
    if cid and "X-Correlation-ID" not in request.headers:
        request.headers["X-Correlation-ID"] = cid


def build_a2a_http_client(timeout_seconds: float, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    async def hook(request: httpx.Request) -> None:
        _correlation_hook(request)

    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds, connect=2.0),
        transport=transport,
        event_hooks={"request": [hook]},
    )


class AgentDirectory:
    def __init__(self, base_urls: list[str], http: httpx.AsyncClient) -> None:
        self.entries = [AgentEntry(u.rstrip("/")) for u in base_urls if u.strip()]
        self.http = http

    async def refresh(self, force: bool = False) -> None:
        now = time.monotonic()
        for entry in self.entries:
            if not force and entry.reachable and now - entry.fetched_at < CARD_TTL:
                continue
            try:
                card = await A2ACardResolver(self.http, entry.base_url).get_agent_card()
                entry.card, entry.error = card, None
                entry.skills = [s.id for s in card.skills]
                log.info("agent_card_discovered", agent=card.name, url=entry.base_url, skills=entry.skills)
            except Exception as exc:  # noqa: BLE001 - any failure = agent unreachable
                entry.error = f"{type(exc).__name__}: {str(exc)[:120]}"
                log.warning("agent_card_unavailable", url=entry.base_url, error=entry.error)
            entry.fetched_at = now

    async def agent_for(self, skill: str) -> AgentEntry | None:
        entry = self._find(skill)
        if entry is None or not entry.reachable:
            await self.refresh()
            entry = self._find(skill)
        return entry if entry and entry.reachable else None

    def _find(self, skill: str) -> AgentEntry | None:
        return next((e for e in self.entries if skill in e.skills), None)

    def by_name(self, name: str) -> AgentEntry | None:
        key = name.lower().replace(" ", "-")
        for e in self.entries:
            if e.card and e.card.name.lower().replace(" ", "-") == key:
                return e
        for e in self.entries:
            if key in e.base_url:
                return e
        return None

    def last_error_for(self, skill: str) -> str:
        known = self._find(skill)
        if known:
            return f"{known.name} is not reachable ({known.error})"
        errors = [f"{e.base_url}: {e.error}" for e in self.entries if e.error]
        return "no agent advertises this skill" + (f" (unreachable: {'; '.join(errors)})" if errors else "")


_OWNER_NAME = {"customer": "Customer Agent", "order": "Order Agent"}


class A2AExecutor:
    """Executes each planned step by delegating it as an A2A task."""

    name = "a2a"

    def __init__(self, directory: AgentDirectory) -> None:
        self.directory = directory

    async def execute(self, step: PlannedStep, correlation_id: str | None) -> dict:
        base = {"tool": step.tool, "args": step.args, "http_status": None, "data": None}
        entry = await self.directory.agent_for(step.tool)
        if entry is None:
            reason = self.directory.last_error_for(step.tool)
            log.warning("a2a_no_agent", skill=step.tool, reason=reason)
            return {**base, "outcome": "unavailable", "error_code": "AGENT_UNAVAILABLE",
                    "detail": reason, "via": "a2a (no agent)"}

        started = time.perf_counter()
        message = new_data_message({"skill": step.tool, "args": step.args}, role=Role.ROLE_USER)
        if correlation_id:
            message.metadata.update({"correlation_id": correlation_id})
        faults = downstream_fault_headers().get()
        if faults:  # demo-only fault injection travels with the task to the specialist
            message.metadata.update({"simulate": faults})
        try:
            client = await create_client(entry.card, ClientConfig(streaming=False, httpx_client=self.directory.http))
            task = None
            async for event in client.send_message(SendMessageRequest(message=message)):
                if event.HasField("task"):
                    task = event.task
        except (A2AClientTimeoutError, httpx.TimeoutException) as exc:
            return self._agent_failure(base, entry, "AGENT_TIMEOUT", f"{entry.name} did not answer in time", started, exc)
        except (A2AClientError, httpx.TransportError) as exc:
            entry.error = f"{type(exc).__name__}: {str(exc)[:120]}"
            return self._agent_failure(base, entry, "AGENT_UNAVAILABLE", f"{entry.name} is not reachable", started, exc)

        latency = round((time.perf_counter() - started) * 1000, 1)
        if task is None:
            return {**base, "outcome": "error", "error_code": "A2A_NO_TASK",
                    "detail": f"{entry.name} returned no task", "via": f"a2a {entry.name}", "latency_ms": latency}

        state = TaskState.Name(task.status.state)
        status_text = get_message_text(task.status.message) if task.status.HasField("message") else None
        artifact = next((d for a in task.artifacts for d in get_data_parts(a.parts) if isinstance(d, dict)), None)
        artifact = _restore_ints(artifact)
        log.info("a2a_task_result", agent=entry.name, task_id=task.id, state=state, latency_ms=latency)

        if artifact and "outcome" in artifact:
            try:
                result = {**base, **validate_artifact(step, artifact)}
            except ContractViolation as exc:
                log.warning("a2a_artifact_rejected", agent=entry.name, task_id=task.id, reason=str(exc))
                result = {**base, "outcome": "error", "error_code": "INVALID_AGENT_RESPONSE",
                          "detail": f"{entry.name} returned a result that failed validation: {exc}"}
        elif state == "TASK_STATE_REJECTED":
            result = {**base, "outcome": "invalid", "error_code": "A2A_TASK_REJECTED", "detail": status_text}
        elif state == "TASK_STATE_INPUT_REQUIRED":
            result = {**base, "outcome": "invalid", "error_code": "A2A_INPUT_REQUIRED", "detail": status_text}
        else:
            result = {**base, "outcome": "error", "error_code": "A2A_TASK_FAILED", "detail": status_text}

        result.update(
            via=f"a2a {entry.name}",
            latency_ms=latency,
            a2a={"agent": entry.name, "task_id": task.id, "context_id": task.context_id,
                 "state": state, "status_message": status_text},
        )
        return result

    def _agent_failure(self, base, entry, code, detail, started, exc) -> dict:
        log.warning("a2a_delegation_failed", agent=entry.name, code=code, error=type(exc).__name__)
        return {**base, "outcome": "unavailable", "error_code": code, "detail": detail,
                "via": f"a2a {entry.name}", "latency_ms": round((time.perf_counter() - started) * 1000, 1)}

    async def get_task(self, agent_name: str, task_id: str) -> dict | None:
        entry = self.directory.by_name(agent_name)
        if entry is None or entry.card is None:
            await self.directory.refresh(force=True)
            entry = self.directory.by_name(agent_name)
        if entry is None or entry.card is None:
            return None
        client = await create_client(entry.card, ClientConfig(streaming=False, httpx_client=self.directory.http))
        task = await client.get_task(GetTaskRequest(id=task_id))
        return {"agent": entry.name, "task": MessageToDict(task)}


_OUTCOMES = {"ok", "not_found", "invalid", "unauthorized", "rate_limited", "unavailable", "error"}
_RESULT_FIELDS = ("outcome", "http_status", "data", "error_code", "detail")


def validate_artifact(step: PlannedStep, artifact: dict) -> dict:
    """Do not trust another agent blindly: the result must answer THIS step,
    use a known outcome, and (when ok) carry data that passes the same contract
    as a direct API call. Only known fields are taken over."""
    if artifact.get("tool") != step.tool:
        raise ContractViolation(f"result is for tool {artifact.get('tool')!r}, expected {step.tool!r}")
    if artifact.get("args") != step.args:
        raise ContractViolation(f"result is for args {artifact.get('args')}, expected {step.args}")
    outcome = artifact.get("outcome")
    if outcome not in _OUTCOMES:
        raise ContractViolation(f"unknown outcome {outcome!r}")
    clean = {k: artifact.get(k) for k in _RESULT_FIELDS}
    if outcome == "ok":
        clean["data"] = validate_tool_data(step.tool, step.args, artifact.get("data"))
    else:
        clean["data"] = None
    return clean


def _restore_ints(value):
    """A2A DataParts travel as protobuf Struct, where every number is a double.
    Turn integral floats back into ints so the data matches the REST APIs (1 not 1.0)."""
    if isinstance(value, dict):
        return {k: _restore_ints(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_restore_ints(v) for v in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def owner_display_name(owner: str) -> str:
    return _OWNER_NAME.get(owner, owner)
