"""Tool registry: the business capabilities an agent may use.

Each tool is a thin, typed wrapper over ONE managed API operation exposed by
Kong. The registry is also the source for capability discovery (the
coordinator's /agent/capabilities today, A2A Agent Cards in Phase 3).

Every call returns a ToolResult with a normalised `outcome`, so agents reason
about *what happened* (ok / not_found / unavailable / ...) instead of raw HTTP,
and can never mistake an error for data.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

import httpx

from .contracts import ContractViolation, validate_tool_data
from .gateway_client import GatewayClient

CUSTOMER_ID = re.compile(r"^C\d{3,}$")
ORDER_ID = re.compile(r"^O\d{4,}$")


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    owner: str  # which domain/agent owns it: "customer" | "order"
    parameters: dict[str, Any]
    path: Callable[[dict], str]
    query: Callable[[dict], dict] = field(default=lambda args: {})

#validate(args) ตรวจอาร์กิวเมนต์ก่อนเรียก API ถ้าผ่านคืน None ถ้าไม่ผ่านคืนข้อความอธิบายปัญหา
#อาร์กิวเมนต์ที่ required ต้องมีครบ
#ค่าต้องตรงกับ pattern ใน schema
#ห้ามมีอาร์กิวเมนต์ที่ไม่รู้จัก
#ข้อความ error เขียนให้ LLM อ่านเข้าใจและแก้ไขแล้วเรียกใหม่ได้

    def validate(self, args: dict) -> str | None:
        for pname, spec in self.parameters["properties"].items():
            if pname in self.parameters.get("required", []) and pname not in args:
                return f"missing argument '{pname}'"
            pattern = spec.get("pattern")
            if pname in args and pattern and not re.match(pattern, str(args[pname])):
                return f"argument '{pname}'={args[pname]!r} does not match {pattern}"
        unknown = set(args) - set(self.parameters["properties"])
        if unknown:
            return f"unknown arguments {sorted(unknown)}"
        return None

#4. _id_param: ตัวช่วยสร้าง schema สร้าง JSON Schema สำหรับ tool ที่รับ ID เพียงตัวเดียว พร้อม pattern และตัวอย่าง เพื่อลดโค้ดซ้ำ
def _id_param(name: str, pattern: str, example: str) -> dict:
    return {
        "type": "object",
        "properties": {name: {"type": "string", "pattern": pattern, "examples": [example]}},
        "required": [name],
    }


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool(
            name="get_customer",
            description="Get a customer's profile (name, tier, status, contact) by customer ID.",
            owner="customer",
            parameters=_id_param("customer_id", CUSTOMER_ID.pattern, "C001"),
            path=lambda a: f"/customers/{a['customer_id']}",
        ),
        Tool(
            name="get_latest_order",
            description="Get the most recent order of a customer, including its status.",
            owner="order",
            parameters=_id_param("customer_id", CUSTOMER_ID.pattern, "C001"),
            path=lambda a: "/orders/latest",
            query=lambda a: {"customer_id": a["customer_id"]},
        ),
        Tool(
            name="list_customer_orders",
            description="List a customer's orders, newest first (max 10).",
            owner="order",
            parameters=_id_param("customer_id", CUSTOMER_ID.pattern, "C001"),
            path=lambda a: "/orders",
            query=lambda a: {"customer_id": a["customer_id"], "limit": 10},
        ),
        Tool(
            name="get_order",
            description="Get one order (status, items, total) by order ID.",
            owner="order",
            parameters=_id_param("order_id", ORDER_ID.pattern, "O1001"),
            path=lambda a: f"/orders/{a['order_id']}",
        ),
    ]
}


@dataclass
class ToolResult:
    tool: str
    args: dict
    outcome: str  # ok | not_found | invalid | unauthorized | rate_limited | unavailable | error
    http_status: int | None = None
    data: Any = None
    error_code: str | None = None
    detail: str | None = None
    latency_ms: float | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"

    def to_dict(self) -> dict:
        return asdict(self)


def _outcome_for(status: int) -> str:
    if status < 300:
        return "ok"
    if status == 404:
        return "not_found"
    if status in (400, 422):
        return "invalid"
    if status in (401, 403):
        return "unauthorized"
    if status == 429:
        return "rate_limited"
    if status in (502, 503, 504):
        return "unavailable"
    return "error"


async def execute_tool(
    client: GatewayClient, tool_name: str, args: dict, correlation_id: str | None = None
) -> ToolResult:
    tool = TOOLS.get(tool_name)
    if tool is None:
        return ToolResult(tool_name, args, "invalid", detail=f"unknown tool '{tool_name}'")
    problem = tool.validate(args)
    if problem:
        return ToolResult(tool_name, args, "invalid", detail=problem)

    started = time.perf_counter()
    try:
        response = await client.get(
            tool.path(args), params=tool.query(args) or None, correlation_id=correlation_id, target=tool.owner
        )
    except httpx.TimeoutException:
        return ToolResult(
            tool_name, args, "unavailable", error_code="TIMEOUT",
            detail="The API did not respond in time.", latency_ms=_ms(started),
        )
    except httpx.TransportError as exc:
        return ToolResult(
            tool_name, args, "unavailable", error_code="GATEWAY_UNREACHABLE",
            detail=f"Could not reach the API gateway ({type(exc).__name__}).", latency_ms=_ms(started),
        )

    body: Any
    try:
        body = response.json()
    except ValueError:
        body = None
    outcome = _outcome_for(response.status_code)
    if outcome == "ok":
        # Never hand unverified data to the agent: schema + "is this what we asked for".
        try:
            data = validate_tool_data(tool_name, args, body)
        except ContractViolation as exc:
            return ToolResult(
                tool_name, args, "error", response.status_code,
                error_code="INVALID_UPSTREAM_RESPONSE",
                detail=f"The API response failed validation: {exc}", latency_ms=_ms(started),
            )
        return ToolResult(tool_name, args, "ok", response.status_code, data=data, latency_ms=_ms(started))

    body = body if isinstance(body, dict) else {}
    return ToolResult(
        tool_name,
        args,
        outcome,
        response.status_code,
        error_code=body.get("code") or ("GATEWAY_" + str(response.status_code) if body.get("source") else None),
        detail=body.get("detail") or body.get("message"),
        latency_ms=_ms(started),
    )


def tool_catalog(owner: str | None = None) -> list[dict]:
    return [
        {"name": t.name, "description": t.description, "owner": t.owner, "parameters": t.parameters}
        for t in TOOLS.values()
        if owner is None or t.owner == owner
    ]


def _ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)
