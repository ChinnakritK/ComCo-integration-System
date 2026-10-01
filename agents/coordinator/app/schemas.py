from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Status = Literal[
    "completed", "completed_with_gaps", "not_found", "partial", "failed", "needs_clarification", "unsupported"
]


class AgentQuery(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"example": {"query": "Find customer C001 and tell me their latest order status."}},
    )
    query: str = Field(min_length=1, max_length=500)


class StepReport(BaseModel):
    id: str
    tool: str
    args: dict[str, Any]
    reason: str
    depends_on: list[str]
    outcome: str = Field(description="ok | not_found | skipped | unavailable | rate_limited | unauthorized | invalid | error")
    http_status: int | None = None
    error_code: str | None = None
    detail: str | None = None
    latency_ms: float | None = None
    via: str | None = None
    a2a: dict[str, Any] | None = Field(
        default=None, description="A2A delegation details: agent, task_id, context_id, final task state"
    )


class AgentResponse(BaseModel):
    query: str
    status: Status = Field(
        description=(
            "completed: everything answered · completed_with_gaps: answered, some items do not exist · "
            "not_found: requested entity does not exist · partial: some data could not be retrieved · "
            "failed: nothing could be retrieved · needs_clarification / unsupported: no tool applicable"
        )
    )
    answer: str = Field(description="Human-readable answer composed ONLY from retrieved data")
    intent: str
    planner: str
    executor: str
    steps: list[StepReport]
    graph_path: list[str] = Field(description="LangGraph nodes visited, in order")
    data: dict[str, Any] = Field(description="Raw business data returned by the tools (verbatim)")
    correlation_id: str | None = None
    trace_id: str | None = None
