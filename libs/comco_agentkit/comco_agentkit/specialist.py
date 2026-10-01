"""A2A specialist agent (Customer Agent / Order Agent) built on the official a2a-sdk.

A specialist owns one business domain. It publishes an A2A **Agent Card**
(`/.well-known/agent-card.json`) listing its skills - one skill per tool it
owns - and executes delegated **tasks** over A2A JSON-RPC (`/a2a`):

    Coordinator --A2A message(DataPart {"skill", "args"})--> Specialist
    Specialist: SUBMITTED -> WORKING -> calls the managed API through Kong
                -> artifact (normalised ToolResult) + final state:
                   COMPLETED       ok / not_found (a definitive business answer)
                   FAILED          API unavailable, timeout, rate limited, ...
                   REJECTED        skill not offered by this agent / invalid args
                   INPUT_REQUIRED  free-text request without the needed ID

Specialists call the business APIs through the gateway with their OWN consumer
(least privilege: the Customer Agent's key cannot read orders and vice versa).
Free-text requests are also accepted, so any A2A client can talk to them.
"""

from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

from fastapi import FastAPI
from google.protobuf.json_format import MessageToDict
from opentelemetry import trace

from a2a.helpers import get_data_parts, get_message_text, new_data_part, new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import add_a2a_routes_to_fastapi, create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentProvider, AgentSkill
from a2a.utils.constants import PROTOCOL_VERSION_1_0

from .gateway_client import GatewayClient, downstream_fault_headers
from .tools import TOOLS, execute_tool

A2A_RPC_PATH = "/a2a"

_tracer = trace.get_tracer("comco.specialist")


@dataclass(frozen=True)
class Domain:
    key: str
    name: str
    description: str
    examples: dict[str, list[str]]


DOMAINS = {
    "customer": Domain(
        key="customer",
        name="Customer Agent",
        description="Answers questions about ComCo customers (profile, tier, status, contact) "
        "using the managed Customer API.",
        examples={"get_customer": ["Get customer C001", '{"skill": "get_customer", "args": {"customer_id": "C001"}}']},
    ),
    "order": Domain(
        key="order",
        name="Order Agent",
        description="Answers questions about ComCo orders (status, latest order, order history) "
        "using the managed Order API.",
        examples={
            "get_order": ["What is the status of order O1001?"],
            "get_latest_order": ["Latest order of customer C001"],
            "list_customer_orders": ["Orders of customer C002"],
        },
    ),
}


def skills_for(domain: str) -> list[str]:
    return [t.name for t in TOOLS.values() if t.owner == domain]


def build_agent_card(domain: str, public_url: str) -> AgentCard:
    d = DOMAINS[domain]
    return AgentCard(
        name=d.name,
        description=d.description,
        version="1.0.0",
        provider=AgentProvider(organization="ComCo", url="https://comco.example"),
        supported_interfaces=[
            AgentInterface(
                url=public_url.rstrip("/") + A2A_RPC_PATH,
                protocol_binding="JSONRPC",
                protocol_version=PROTOCOL_VERSION_1_0,
            )
        ],
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["application/json", "text/plain"],
        default_output_modes=["application/json", "text/plain"],
        skills=[
            AgentSkill(
                id=name,
                name=name.replace("_", " ").capitalize(),
                description=TOOLS[name].description,
                tags=[domain, "comco"],
                examples=d.examples.get(name, []),
                input_modes=["application/json", "text/plain"],
                output_modes=["application/json"],
            )
            for name in skills_for(domain)
        ],
    )


# ------------------------------------------------------------------ request parsing
_CUSTOMER_RE = re.compile(r"\b(c\d{3,})\b", re.IGNORECASE)
_ORDER_RE = re.compile(r"\b(o\d{4,})\b", re.IGNORECASE)
_LATEST_RE = re.compile(r"\b(latest|last|most recent|recent|newest)\b", re.IGNORECASE)


def parse_request(domain: str, message) -> tuple[str | None, dict, str | None]:
    """Return (skill, args, problem). Structured DataPart wins; free text is a fallback."""
    for data in get_data_parts(message.parts):
        if isinstance(data, dict) and "skill" in data:
            return str(data["skill"]), dict(data.get("args") or {}), None

    text = get_message_text(message) or ""
    customer = next(iter(m.upper() for m in _CUSTOMER_RE.findall(text)), None)
    order = next(iter(m.upper() for m in _ORDER_RE.findall(text)), None)
    if domain == "customer":
        if customer:
            return "get_customer", {"customer_id": customer}, None
        return None, {}, "Please provide a customer ID, e.g. C001."
    if order:
        return "get_order", {"order_id": order}, None
    if customer:
        skill = "get_latest_order" if _LATEST_RE.search(text) else "list_customer_orders"
        return skill, {"customer_id": customer}, None
    return None, {}, "Please provide an order ID (e.g. O1001) or a customer ID (e.g. C001)."


# ------------------------------------------------------------------ executor
class SpecialistExecutor(AgentExecutor):
    def __init__(self, domain: str, client: GatewayClient, logger) -> None:
        self.domain = domain
        self.client = client
        self.skills = set(skills_for(domain))
        self.log = logger

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        # One readable span per A2A task (the SDK's own fine-grained spans are switched
        # off via OTEL_INSTRUMENTATION_A2A_SDK_ENABLED=false - see docker-compose.yml).
        with _tracer.start_as_current_span(f"a2a.task {self.domain}-agent") as span:
            span.set_attribute("a2a.agent", DOMAINS[self.domain].name)
            await self._run(context, event_queue, span)

    async def _run(self, context: RequestContext, event_queue: EventQueue, span) -> None:
        from comco_common import get_correlation_id  # local import keeps agentkit importable alone

        task = context.current_task or new_task_from_user_message(context.message)
        if not context.current_task:
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)

        def say(text: str):
            return updater.new_agent_message([new_text_part(text)])

        skill, args, problem = parse_request(self.domain, context.message)
        metadata = MessageToDict(context.message.metadata) if context.message.metadata else {}
        correlation_id = get_correlation_id() or metadata.get("correlation_id")
        log = self.log.bind(a2a_task_id=task.id, skill=skill)
        span.set_attribute("a2a.task_id", task.id)
        span.set_attribute("a2a.skill", str(skill))
        if correlation_id:
            span.set_attribute("comco.correlation_id", correlation_id)

        if problem:
            log.info("a2a_task_input_required", reason=problem)
            span.set_attribute("a2a.state", "INPUT_REQUIRED")
            await updater.requires_input(say(problem))
            return
        if skill not in self.skills:
            log.warning("a2a_task_rejected", reason="skill not offered")
            span.set_attribute("a2a.state", "REJECTED")
            await updater.reject(say(f"{DOMAINS[self.domain].name} does not offer skill '{skill}'. "
                                     f"Available: {sorted(self.skills)}."))
            return

        await updater.start_work(say(f"Calling {skill} {args} via the API gateway."))
        log.info("a2a_task_working", args=args)
        simulate = metadata.get("simulate") if os.getenv("FAULT_INJECTION_ENABLED", "false") == "true" else None
        token = downstream_fault_headers().set(simulate or None)
        try:
            result = await execute_tool(self.client, skill, args, correlation_id)
        finally:
            downstream_fault_headers().reset(token)
        await updater.add_artifact([new_data_part(result.to_dict())], name=f"{skill}-result")

        summary = f"{skill} -> {result.outcome}" + (f" (HTTP {result.http_status})" if result.http_status else "")
        log.info("a2a_task_finished", outcome=result.outcome, http_status=result.http_status,
                 latency_ms=result.latency_ms)
        span.set_attribute("a2a.tool_outcome", result.outcome)
        if result.outcome in ("ok", "not_found"):
            span.set_attribute("a2a.state", "COMPLETED")
            await updater.complete(say(summary))
        elif result.outcome == "invalid":
            span.set_attribute("a2a.state", "REJECTED")
            await updater.reject(say(f"{summary}: {result.detail}"))
        else:
            # A real failure: mark the span as an error so it shows red in Jaeger.
            span.set_attribute("a2a.state", "FAILED")
            span.set_status(trace.Status(trace.StatusCode.ERROR, result.error_code or result.outcome))
            await updater.failed(say(f"{summary}: {result.detail or result.error_code}"))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task:
            await TaskUpdater(event_queue, task.id, task.context_id).cancel()


# ------------------------------------------------------------------ app factory
def create_specialist_app(domain: str, gateway_transport=None) -> FastAPI:
    """FastAPI app = Agent Card + A2A JSON-RPC endpoint + health, with the platform's
    correlation/logging/tracing middleware."""
    from comco_common import RequestContextMiddleware, configure_logging, get_logger, register_problem_handlers
    from comco_common.telemetry import init_tracing, setup_telemetry

    if domain not in DOMAINS:
        raise ValueError(f"unknown agent domain {domain!r}")
    service_name = os.getenv("SERVICE_NAME", f"{domain}-agent")
    public_url = os.getenv("AGENT_PUBLIC_URL", f"http://{domain}-agent:8000")
    configure_logging(service_name)
    init_tracing(service_name)
    log = get_logger("a2a")

    client = GatewayClient(
        base_url=os.getenv("GATEWAY_URL", "http://kong:8000/api/v1"),
        api_key=os.getenv("AGENT_API_KEY", ""),
        transport=gateway_transport,
    )
    card = build_agent_card(domain, public_url)
    handler = DefaultRequestHandler(
        agent_executor=SpecialistExecutor(domain, client, log),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        log.info("service_started", agent=card.name, skills=skills_for(domain), a2a_url=public_url + A2A_RPC_PATH)
        yield
        await client.aclose()

    app = FastAPI(
        title=f"ComCo {card.name}",
        version="1.0.0",
        description=f"{card.description} A2A endpoint: `{A2A_RPC_PATH}` (JSON-RPC, protocol 1.0).",
        lifespan=lifespan,
    )
    app.add_middleware(RequestContextMiddleware)
    register_problem_handlers(app)
    setup_telemetry(app, service_name)
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url=A2A_RPC_PATH),
    )

    @app.get("/health", include_in_schema=False)
    async def health() -> dict:
        return {"status": "ok", "service": service_name}

    @app.get("/ready", include_in_schema=False)
    async def ready() -> dict:
        return {"status": "ready", "service": service_name}

    return app

'''รับ task
  ├─ ไม่มี ID         → INPUT_REQUIRED  ("กรุณาระบุ customer ID")
  ├─ skill ไม่ใช่ของฉัน → REJECTED
  └─ ปกติ:
       WORKING  → execute_tool() ผ่าน Kong ด้วย key ของตัวเอง
                → แนบผลเป็น artifact
                → ok/not_found       = COMPLETED
                  invalid            = REJECTED
                  ล่ม/timeout/429/ผิด = FAILED  (span สีแดงใน Jaeger)'''