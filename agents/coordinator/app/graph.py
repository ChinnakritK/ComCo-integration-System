"""The Coordinator's reasoning loop as an explicit LangGraph state machine.

    START -> plan_request --(no tools needed)--------------------> compose_answer -> END
                      \\--(tools planned)--> execute_step --(more)--^ loop
                                                     \\--(done)---> compose_answer

* plan_request   - understand the request, select tools (RulePlanner / LlmPlanner)
* execute_step   - run ONE planned tool call; before calling, check its
                   dependencies (e.g. do not fetch orders for a customer that
                   was not found) -> conditional orchestration
* compose_answer - build a grounded answer from the tool results only

Tool execution goes through a `ToolExecutor`, so Phase 3 can swap direct API
calls for A2A delegation to specialist agents without touching the graph.
"""

from __future__ import annotations

from typing import Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from opentelemetry import trace

from comco_agentkit import GatewayClient, execute_tool
from comco_common import get_logger

from .answer import compose
from .planner import Plan, PlannedStep

log = get_logger(__name__)
tracer = trace.get_tracer("comco.coordinator")


class ToolExecutor(Protocol):
    name: str

    async def execute(self, step: PlannedStep, correlation_id: str | None) -> dict: ...


class DirectApiExecutor:
    """Phase 2: the coordinator calls the managed APIs itself (through Kong)."""

    name = "direct-api"

    def __init__(self, client: GatewayClient) -> None:
        self.client = client

    async def execute(self, step: PlannedStep, correlation_id: str | None) -> dict:
        result = await execute_tool(self.client, step.tool, step.args, correlation_id)
        out = result.to_dict()
        out["via"] = f"kong {self.client.base_url}"
        return out


class AgentState(TypedDict, total=False):
    query: str
    correlation_id: str | None
    plan: Plan
    pending: list[str]
    results: dict[str, dict]
    path: list[str]
    status: str
    answer: str
    data: dict[str, Any]


def build_graph(planner, executor: ToolExecutor):
    async def plan_request(state: AgentState) -> AgentState:
        with tracer.start_as_current_span("agent.plan_request") as span:
            plan: Plan = await planner.plan(state["query"])
            span.set_attribute("agent.planner", plan.planner)
            span.set_attribute("agent.intent", plan.intent)
            span.set_attribute("agent.tools", [s.tool for s in plan.steps])
            log.info("plan_created", planner=plan.planner, intent=plan.intent,
                     tools=[f"{s.tool}({','.join(s.args.values())})" for s in plan.steps])
            return {
                "plan": plan,
                "pending": [s.id for s in plan.steps],
                "results": {},
                "path": [*state.get("path", []), "plan_request"],
            }

    async def execute_step(state: AgentState) -> AgentState:
        plan = state["plan"]
        step_id, *rest = state["pending"]
        step = next(s for s in plan.steps if s.id == step_id)
        results = dict(state["results"])

        with tracer.start_as_current_span(f"agent.tool.{step.tool}") as span:
            span.set_attribute("agent.tool.args", str(step.args))
            blocked = [d for d in step.depends_on if results.get(d, {}).get("outcome") != "ok"]
            if blocked:
                dep = results.get(blocked[0], {})
                # Dependency "not found" is already explained to the user; stay silent.
                # Dependency failure: say explicitly that this part was not attempted.
                detail = None if dep.get("outcome") == "not_found" else (
                    f"{step.tool} was not attempted because a prerequisite step ({blocked[0]}) failed."
                )
                result = {"tool": step.tool, "args": step.args, "outcome": "skipped",
                          "detail": detail, "skipped_because": blocked}
                log.info("step_skipped", step=step.id, because=blocked)
            else:
                result = await executor.execute(step, state.get("correlation_id"))
                log.info("step_executed", step=step.id, tool=step.tool, outcome=result["outcome"],
                         http_status=result.get("http_status"), latency_ms=result.get("latency_ms"))
            span.set_attribute("agent.tool.outcome", result["outcome"])

        results[step.id] = result
        return {"pending": rest, "results": results, "path": [*state["path"], f"execute_step:{step.tool}"]}

    async def compose_answer(state: AgentState) -> AgentState:
        with tracer.start_as_current_span("agent.compose_answer") as span:
            status, answer, data = compose(state["plan"], state.get("results", {}))
            span.set_attribute("agent.status", status)
            return {"status": status, "answer": answer, "data": data,
                    "path": [*state["path"], "compose_answer"]}

    def after_plan(state: AgentState) -> str:
        return "execute_step" if state["pending"] else "compose_answer"

    def after_step(state: AgentState) -> str:
        return "execute_step" if state["pending"] else "compose_answer"

    graph = StateGraph(AgentState)
    graph.add_node("plan_request", plan_request)
    graph.add_node("execute_step", execute_step)
    graph.add_node("compose_answer", compose_answer)
    graph.add_edge(START, "plan_request")
    graph.add_conditional_edges("plan_request", after_plan, ["execute_step", "compose_answer"])
    graph.add_conditional_edges("execute_step", after_step, ["execute_step", "compose_answer"])
    graph.add_edge("compose_answer", END)
    return graph.compile()
