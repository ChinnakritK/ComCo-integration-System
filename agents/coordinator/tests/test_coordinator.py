"""Coordinator Agent tests - the Kong gateway is replaced by an httpx MockTransport
serving the same seed data, so agent behaviour is tested without Docker."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

CUSTOMERS = {
    "C001": {"customer_id": "C001", "first_name": "Somchai", "last_name": "Jaidee", "email": "somchai.j@example.com",
             "phone": "+66-81-234-5678", "tier": "GOLD", "status": "ACTIVE",
             "created_at": "2024-03-12T09:30:00Z", "updated_at": "2024-03-12T09:30:00Z"},
    "C003": {"customer_id": "C003", "first_name": "Anan", "last_name": "Wongsa", "email": "anan.w@example.com",
             "phone": None, "tier": "PLATINUM", "status": "ACTIVE",
             "created_at": "2025-06-20T08:15:00Z", "updated_at": "2025-06-20T08:15:00Z"},
}
ORDERS = {
    "O1001": {"order_id": "O1001", "customer_id": "C001", "status": "SHIPPED", "currency": "THB",
              "items": [{"sku": "SKU-ROUTER-AX3000", "name": "Wi-Fi 6 Router AX3000", "quantity": 1, "unit_price": "2490.00"}],
              "total_amount": "2490.00", "created_at": "2026-09-10T03:20:00Z", "updated_at": "2026-09-12T07:45:00Z"},
    "O1003": {"order_id": "O1003", "customer_id": "C001", "status": "PROCESSING", "currency": "THB",
              "items": [{"sku": "SKU-MESH-3PK", "name": "Mesh Wi-Fi system (3-pack)", "quantity": 1, "unit_price": "5990.00"}],
              "total_amount": "5990.00", "created_at": "2026-09-27T06:10:00Z", "updated_at": "2026-09-28T02:00:00Z"},
}


def problem(status, code, detail):
    return httpx.Response(status, json={"status": status, "code": code, "detail": detail},
                          headers={"content-type": "application/problem+json"})


class FakeGateway:
    """Mimics Kong + both backends. `down` = set of services returning 503."""

    def __init__(self):
        self.down: set[str] = set()
        self.calls: list[httpx.Request] = []
        self.exception: Exception | None = None
        self.rate_limited = False
        self.tamper = None  # callable(path, json) -> json, to simulate a misbehaving backend

    def __call__(self, request: httpx.Request) -> httpx.Response:
        response = self._route(request)
        if self.tamper and response.status_code == 200:
            path = request.url.path.removeprefix("/api/v1")
            return httpx.Response(200, json=self.tamper(path, response.json()))
        return response

    def _route(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.exception:
            raise self.exception
        if request.headers.get("apikey") != "agent-key":
            return httpx.Response(401, json={"detail": "No API key found in request", "source": "kong-gateway"})
        if self.rate_limited:
            return httpx.Response(429, json={"detail": "Rate limit exceeded", "source": "kong-gateway"})
        path = request.url.path.removeprefix("/api/v1")
        service = "customer" if path.startswith("/customers") else "order"
        if service in self.down:
            return httpx.Response(503, json={"detail": "failure to get a peer from the ring-balancer",
                                             "source": "kong-gateway"})
        if sim := request.headers.get("X-Simulate-Status"):
            return problem(int(sim), "SIMULATED_FAILURE", "Simulated failure")
        if path.startswith("/customers/"):
            cid = path.split("/")[-1]
            return httpx.Response(200, json=CUSTOMERS[cid]) if cid in CUSTOMERS else problem(
                404, "CUSTOMER_NOT_FOUND", f"Customer {cid} does not exist.")
        if path == "/orders/latest":
            cid = request.url.params["customer_id"]
            mine = sorted((o for o in ORDERS.values() if o["customer_id"] == cid), key=lambda o: o["created_at"])
            return httpx.Response(200, json=mine[-1]) if mine else problem(
                404, "NO_ORDERS_FOR_CUSTOMER", f"No orders found for customer {cid}.")
        if path == "/orders":
            cid = request.url.params["customer_id"]
            mine = sorted((o for o in ORDERS.values() if o["customer_id"] == cid),
                          key=lambda o: o["created_at"], reverse=True)
            return httpx.Response(200, json={"items": mine, "total": len(mine), "limit": 10, "offset": 0})
        if path.startswith("/orders/"):
            oid = path.split("/")[-1]
            return httpx.Response(200, json=ORDERS[oid]) if oid in ORDERS else problem(
                404, "ORDER_NOT_FOUND", f"Order {oid} does not exist.")
        return httpx.Response(404, json={"detail": "no Route matched"})


@pytest.fixture()
def gateway():
    return FakeGateway()


def make_client(gateway, llm_transport=None, **overrides):
    settings = Settings(gateway_url="http://kong:8000/api/v1", agent_api_key="agent-key", **overrides)
    app = create_app(settings, gateway_transport=httpx.MockTransport(gateway), llm_transport=llm_transport)
    return TestClient(app)


@pytest.fixture()
def client(gateway):
    with make_client(gateway) as c:
        yield c


def ask(client, query, headers=None):
    r = client.post("/agent/query", json={"query": query}, headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ assessment scenarios
def test_single_tool_selection(client, gateway):
    body = ask(client, "Show customer C001")
    assert body["status"] == "completed"
    assert [s["tool"] for s in body["steps"]] == ["get_customer"]
    assert "Somchai Jaidee" in body["answer"]
    assert body["data"]["customers"]["C001"]["tier"] == "GOLD"
    assert len(gateway.calls) == 1


def test_multi_tool_orchestration(client):
    body = ask(client, "Show customer C001 and their latest order")
    assert body["status"] == "completed"
    assert [s["tool"] for s in body["steps"]] == ["get_customer", "get_latest_order"]
    assert body["steps"][1]["depends_on"] == ["customer_C001"]
    assert "O1003" in body["answer"] and "PROCESSING" in body["answer"]
    assert body["graph_path"] == [
        "plan_request", "execute_step:get_customer", "execute_step:get_latest_order", "compose_answer"]


def test_key_scenario_latest_order_status(client):
    body = ask(client, "Find customer C001 and tell me their latest order status.")
    assert body["status"] == "completed"
    assert body["data"]["latest_orders"]["C001"]["status"] == "PROCESSING"


def test_unknown_customer_is_not_fabricated(client, gateway):
    body = ask(client, "Show customer C999 and their latest order")
    assert body["status"] == "not_found"
    assert "C999 was not found" in body["answer"]
    assert body["steps"][1]["outcome"] == "skipped"  # no order lookup for a non-existent customer
    assert body["data"] == {}
    assert len(gateway.calls) == 1


def test_order_status_lookup(client):
    body = ask(client, "What is the status of order O1001?")
    assert [s["tool"] for s in body["steps"]] == ["get_order"]
    assert "SHIPPED" in body["answer"]


def test_unknown_order(client):
    body = ask(client, "status of order O9999")
    assert body["status"] == "not_found"
    assert "O9999 was not found" in body["answer"]


def test_customer_without_orders(client):
    body = ask(client, "Show customer C003 and their latest order")
    assert body["status"] == "completed_with_gaps"
    assert "C003 has no orders" in body["answer"]
    assert "latest_orders" not in body["data"]


def test_order_history(client):
    body = ask(client, "Show all orders of customer C001")
    assert [s["tool"] for s in body["steps"]] == ["get_customer", "list_customer_orders"]
    assert "O1003 PROCESSING" in body["answer"]


# ------------------------------------------------------------------ failure handling
def test_backend_unavailable_gives_partial_answer_without_fabrication(client, gateway):
    gateway.down.add("order")
    body = ask(client, "Show customer C001 and their latest order")
    assert body["status"] == "partial"
    assert "Somchai" in body["answer"]
    assert "could not retrieve the latest order" in body["answer"]
    assert "Order service is currently unavailable" in body["answer"]
    assert "O1003" not in body["answer"] and "latest_orders" not in body["data"]
    order_calls = [c for c in gateway.calls if "/orders" in c.url.path]
    assert len(order_calls) == 2  # one retry on 503


def test_everything_unavailable(client, gateway):
    gateway.down.update({"customer", "order"})
    body = ask(client, "Show customer C001 and their latest order")
    assert body["status"] == "failed"
    assert body["steps"][1]["outcome"] == "skipped"
    assert "was not attempted" in body["answer"]


def test_gateway_unreachable(client, gateway):
    gateway.exception = httpx.ConnectError("connection refused")
    body = ask(client, "Show customer C001")
    assert body["status"] == "failed"
    assert body["steps"][0]["error_code"] == "GATEWAY_UNREACHABLE"


def test_timeout(client, gateway):
    gateway.exception = httpx.ReadTimeout("timed out")
    body = ask(client, "What is the status of order O1001?")
    assert body["status"] == "failed"
    assert body["steps"][0]["error_code"] == "TIMEOUT"


def test_rate_limited(client, gateway):
    gateway.rate_limited = True
    body = ask(client, "Show customer C001")
    assert body["steps"][0]["outcome"] == "rate_limited"
    assert "quota" in body["answer"]


def test_downstream_fault_injection_header(gateway):
    with make_client(gateway, fault_injection_enabled=True) as c:
        body = ask(c, "Show customer C001 and their latest order",
                   headers={"X-Simulate-Downstream-Target": "order", "X-Simulate-Downstream-Status": "500"})
    assert body["status"] == "partial"
    assert body["steps"][1]["error_code"] == "SIMULATED_FAILURE"


# ------------------------------------------------------------------ managed-API usage
def test_agent_uses_its_own_api_key_and_forwards_correlation_id(client, gateway):
    body = ask(client, "Show customer C001", headers={"X-Correlation-ID": "corr-42"})
    assert body["correlation_id"] == "corr-42"
    call = gateway.calls[0]
    assert call.headers["apikey"] == "agent-key"
    assert call.headers["X-Correlation-ID"] == "corr-42"
    assert str(call.url).startswith("http://kong:8000/api/v1/customers/C001")


# ------------------------------------------------------------------ no applicable tool
def test_needs_clarification(client, gateway):
    body = ask(client, "What is the status of my order?")
    assert body["status"] == "needs_clarification"
    assert body["steps"] == [] and gateway.calls == []


def test_unsupported(client):
    body = ask(client, "What's the weather in Bangkok?")
    assert body["status"] == "unsupported"


def test_validation(client):
    assert client.post("/agent/query", json={"query": ""}).status_code == 422
    assert client.post("/agent/query", json={}).status_code == 422


def test_capabilities_and_graph(client):
    caps = client.get("/agent/capabilities").json()
    assert {t["name"] for t in caps["tools"]} == {"get_customer", "get_latest_order", "list_customer_orders", "get_order"}
    diagram = client.get("/agent/graph").json()["diagram"]
    assert "plan_request" in diagram and "execute_step" in diagram


# ------------------------------------------------------------------ optional LLM planner
def _ollama(reply: dict | None = None, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, json={"error": "model not found"})
        return httpx.Response(200, json={"message": {"role": "assistant", "content": json.dumps(reply)}})
    return httpx.MockTransport(handler)


def test_llm_planner_used_when_valid(gateway):
    reply = {"steps": [{"tool": "get_customer", "args": {"customer_id": "C001"}, "reason": "profile"},
                       {"tool": "get_latest_order", "args": {"customer_id": "C001"}, "reason": "latest"}]}
    with make_client(gateway, use_llm=True, llm_transport=_ollama(reply)) as c:
        body = ask(c, "Who is C001 and what did they order most recently?")
    assert body["planner"].startswith("llm:")
    assert body["steps"][1]["depends_on"] == [body["steps"][0]["id"]]
    assert "O1003" in body["answer"]


def test_llm_cannot_invent_ids(gateway):
    reply = {"steps": [{"tool": "get_customer", "args": {"customer_id": "C002"}}]}
    with make_client(gateway, use_llm=True, llm_transport=_ollama(reply)) as c:
        body = ask(c, "Show customer C001")
    assert body["planner"].startswith("rules (fallback")
    assert "IDs not present" in body["planner"]
    assert body["steps"][0]["args"] == {"customer_id": "C001"}


def test_llm_unavailable_falls_back_to_rules(gateway):
    with make_client(gateway, use_llm=True, llm_transport=_ollama(status=500)) as c:
        body = ask(c, "Show customer C001")
    assert body["planner"].startswith("rules (fallback")
    assert body["status"] == "completed"


# ------------------------------------------------------------------ response validation
def test_wrong_customer_returned_is_rejected(client, gateway):
    gateway.tamper = lambda path, body: {**body, "customer_id": "C002"} if path.startswith("/customers/") else body
    body = ask(client, "Show customer C001 and their latest order")
    step = body["steps"][0]
    assert step["outcome"] == "error"
    assert step["error_code"] == "INVALID_UPSTREAM_RESPONSE"
    assert "asked for customer C001 but received C002" in step["detail"]
    assert body["steps"][1]["outcome"] == "skipped"  # dependent step not run on unverified data
    assert "Somchai" not in body["answer"] and "did not pass validation" in body["answer"]
    assert body["data"] == {}


def test_malformed_order_is_rejected(client, gateway):
    def drop_status(path, body):
        return {k: v for k, v in body.items() if k != "status"} if path.startswith("/orders/") else body
    gateway.tamper = drop_status
    body = ask(client, "What is the status of order O1001?")
    assert body["status"] == "failed"
    assert body["steps"][0]["error_code"] == "INVALID_UPSTREAM_RESPONSE"
    assert "status" in body["steps"][0]["detail"]
    assert "SHIPPED" not in body["answer"]


def test_latest_order_of_another_customer_is_rejected(client, gateway):
    gateway.tamper = lambda path, body: {**body, "customer_id": "C004"} if path == "/orders/latest" else body
    body = ask(client, "Show customer C001 and their latest order")
    assert body["status"] == "partial"
    assert "belongs to C004, not C001" in body["steps"][1]["detail"]
    assert "O1003" not in body["answer"]


def test_unexpected_fields_are_dropped(client, gateway):
    gateway.tamper = lambda path, body: {**body, "internal_note": "secret"} if path.startswith("/customers/") else body
    body = ask(client, "Show customer C001")
    assert body["status"] == "completed"
    assert "internal_note" not in body["data"]["customers"]["C001"]


def test_non_json_success_is_rejected(client, gateway):
    gateway.tamper = lambda path, body: ["not", "an", "object"]
    body = ask(client, "Show customer C001")
    assert body["steps"][0]["error_code"] == "INVALID_UPSTREAM_RESPONSE"
