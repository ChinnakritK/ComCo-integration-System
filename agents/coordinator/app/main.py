"""Coordinator Agent - Agent API exposed through Kong at /api/v1/agent.
3.7 main.py — ประกอบทุกอย่างและเปิด API
create_app() ทำงานตามลำดับนี้:
อ่าน config
เลือก planner (rules หรือ LLM)
เลือก executor (direct หรือ a2a)
สร้าง graph
ตอนเริ่มระบบ ค้นหา agent ทั้งหมด (directory.refresh)"""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request

from comco_common import ProblemException

from comco_agentkit import GatewayClient, downstream_fault_headers, tool_catalog
from comco_common import (
    RequestContextMiddleware,
    configure_logging,
    get_correlation_id,
    get_logger,
    register_problem_handlers,
    setup_telemetry,
)
from comco_common.context import current_trace_id
from comco_common.openapi import install_openapi_extras, problem_responses
from comco_common.telemetry import init_tracing

from .a2a_executor import A2AExecutor, AgentDirectory, build_a2a_http_client
from .config import Settings
from .graph import DirectApiExecutor, build_graph
from .planner import LlmPlanner, RulePlanner
from .schemas import AgentQuery, AgentResponse, StepReport

log = get_logger(__name__)

EXAMPLES = [
    "Show customer C001",
    "Show customer C001 and their latest order",
    "Show customer C999",
    "What is the status of order O1001?",
    "Find customer C001 and tell me their latest order status.",
    "Show all orders of customer C002",
]


def create_app(
    settings: Settings | None = None, gateway_transport=None, llm_transport=None, a2a_transport=None
) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.service_name)
    init_tracing(settings.service_name)  # before creating httpx clients (traceparent propagation)

    client = GatewayClient(
        base_url=settings.gateway_url,
        api_key=settings.agent_api_key,
        timeout_seconds=settings.tool_timeout_seconds,
        transport=gateway_transport,
    )
    planner = (
        LlmPlanner(settings.ollama_url, settings.ollama_model, settings.llm_timeout_seconds, llm_transport)
        if settings.use_llm
        else RulePlanner()
    )
    directory: AgentDirectory | None = None
    a2a_http = None
    if settings.execution_mode == "a2a":
        a2a_http = build_a2a_http_client(settings.a2a_timeout_seconds, a2a_transport)
        directory = AgentDirectory(list(settings.a2a_agent_urls), a2a_http)
        executor = A2AExecutor(directory)
    else:
        executor = DirectApiExecutor(client)
    graph = build_graph(planner, executor)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if directory is not None:
            await directory.refresh(force=True)  # discovery; unreachable agents are retried lazily
        log.info("service_started", planner=planner.name, executor=executor.name, gateway=settings.gateway_url,
                 agents=[e.summary().get("name", e.base_url) for e in directory.entries] if directory else None)
        yield
        await client.aclose()
        if a2a_http is not None:
            await a2a_http.aclose()
        if isinstance(planner, LlmPlanner):
            await planner.aclose()

    app = FastAPI(
        title="ComCo Agent API",
        version="1.0.0",
        summary="Natural-language access to customer and order information via a Coordinator Agent.",
        description=(
            "The Coordinator Agent understands a request, plans which capabilities are needed and "
            "orchestrates them. In `a2a` mode (default) each step is delegated as an **A2A task** to the "
            "specialist agent whose Agent Card advertises the skill (Customer Agent / Order Agent); the "
            "specialists call the **managed** APIs through the Kong gateway. In `direct` mode the "
            "coordinator calls the managed APIs itself. Business data is never read from databases "
            "directly and never fabricated: answers are composed only from results, and "
            "missing/unavailable data is reported as such."
        ),
        servers=[{"url": "http://localhost:8000/api/v1", "description": "Via Kong API gateway"}],
        lifespan=lifespan,
        generate_unique_id_function=lambda route: route.name,
    )
    app.add_middleware(RequestContextMiddleware)
    register_problem_handlers(app)
    setup_telemetry(app, settings.service_name)

    @app.get("/health", include_in_schema=False)
    async def health() -> dict:
        return {"status": "ok", "service": settings.service_name}

    @app.get("/ready", include_in_schema=False)
    async def ready() -> dict:
        return {"status": "ready", "service": settings.service_name}

    @app.post(
        "/agent/query",
        response_model=AgentResponse,
        tags=["agent"],
        summary="Ask the Coordinator Agent",
        description=(
            "Always returns 200 for a handled request; the business outcome is in `status`. "
            "`steps` shows which tools were selected and why, and `graph_path` the LangGraph route taken."
        ),
        responses=problem_responses(r422="Invalid request body"),
    )
    async def query_agent(body: AgentQuery, request: Request) -> AgentResponse:
        token = downstream_fault_headers().set(
            _fault_injection(request) if settings.fault_injection_enabled else None
        )
        try:
            state = await graph.ainvoke({"query": body.query, "correlation_id": get_correlation_id(), "path": []})
        finally:
            downstream_fault_headers().reset(token)

        plan = state["plan"]
        results = state.get("results", {})
        steps = []
        for s in plan.steps:
            r = results.get(s.id, {})
            steps.append(
                StepReport(
                    id=s.id, tool=s.tool, args=s.args, reason=s.reason, depends_on=s.depends_on,
                    outcome=r.get("outcome", "skipped"), http_status=r.get("http_status"),
                    error_code=r.get("error_code"), detail=r.get("detail"),
                    latency_ms=r.get("latency_ms"), via=r.get("via"), a2a=r.get("a2a"),
                )
            )
        log.info("agent_answered", status=state["status"], intent=plan.intent, steps=len(steps))
        return AgentResponse(
            query=body.query,
            status=state["status"],
            answer=state["answer"],
            intent=plan.intent,
            planner=plan.planner,
            executor=executor.name,
            steps=steps,
            graph_path=state["path"],
            data=state.get("data", {}),
            correlation_id=get_correlation_id(),
            trace_id=current_trace_id(),
        )

    @app.get("/agent/capabilities", tags=["agent"], summary="What the agent can do (tools and examples)")
    async def capabilities() -> dict:
        return {
            "agent": settings.service_name,
            "planner": planner.name,
            "executor": executor.name,
            "agents": [e.summary() for e in directory.entries] if directory else None,
            "tools": tool_catalog(),
            "example_queries": EXAMPLES,
        }

    @app.get(
        "/agent/agents",
        tags=["agent"],
        summary="A2A discovery: specialist agents and the skills in their Agent Cards",
    )
    async def agents() -> dict:
        if directory is None:
            return {"execution_mode": settings.execution_mode, "agents": []}
        await directory.refresh(force=True)
        return {"execution_mode": "a2a", "agents": [e.summary() for e in directory.entries]}

    @app.get(
        "/agent/a2a/tasks/{agent}/{task_id}",
        tags=["agent"],
        summary="A2A task status: fetch a delegated task (state, messages, artifacts) from its agent",
        responses=problem_responses(r404="Unknown agent or task", r503="Agent unreachable"),
    )
    async def a2a_task(agent: str, task_id: str) -> dict:
        if not isinstance(executor, A2AExecutor):
            raise ProblemException(404, "A2A mode is not enabled (EXECUTION_MODE=direct).", code="A2A_DISABLED")
        try:
            found = await executor.get_task(agent, task_id)
        except Exception as exc:  # noqa: BLE001
            text = str(exc).lower()
            if "not found" in text:
                raise ProblemException(404, f"Task {task_id} not found at {agent}.", code="A2A_TASK_NOT_FOUND")
            raise ProblemException(503, f"Could not query {agent}: {type(exc).__name__}", code="AGENT_UNAVAILABLE")
        if found is None:
            raise ProblemException(404, f"Unknown agent '{agent}'.", code="AGENT_NOT_FOUND")
        return found

    @app.get("/agent/graph", tags=["agent"], summary="The agent's LangGraph workflow as a Mermaid diagram")
    async def graph_diagram() -> dict:
        return {"format": "mermaid", "diagram": graph.get_graph().draw_mermaid()}

    install_openapi_extras(app)
    return app


def _fault_injection(request: Request) -> dict[str, dict[str, str]] | None:
    """Demo only: X-Simulate-Downstream-Target: customer|order (+ -Status / -Delay-Ms)."""
    target = request.headers.get("X-Simulate-Downstream-Target")
    if not target:
        return None
    headers = {}
    if status := request.headers.get("X-Simulate-Downstream-Status"):
        headers["X-Simulate-Status"] = status
    if delay := request.headers.get("X-Simulate-Downstream-Delay-Ms"):
        headers["X-Simulate-Delay-Ms"] = delay
    log.warning("downstream_fault_injection", target=target, headers=headers)
    return {target: headers}


app = create_app()
