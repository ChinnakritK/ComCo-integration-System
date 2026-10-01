"""Phase 3 - A2A tests: specialist agents (official a2a-sdk servers) and the
coordinator delegating to them. Everything runs in-process: the specialists are
real ASGI apps reached through an httpx transport, the Kong gateway behind them
is the FakeGateway from test_coordinator."""

import httpx
import pytest
from fastapi.testclient import TestClient

from a2a.client import ClientConfig, create_client
from a2a.helpers import get_data_parts, get_message_text, new_data_message, new_text_message
from a2a.types import Role, SendMessageRequest, TaskState

from app.config import Settings
from app.main import create_app
from comco_agentkit.specialist import create_specialist_app
from test_coordinator import FakeGateway

CUSTOMER_URL = "http://customer-agent:8000"
ORDER_URL = "http://order-agent:8000"


class AgentNetwork(httpx.AsyncBaseTransport):
    """Routes requests by host to in-process agent apps; hosts in `down` refuse connections."""

    def __init__(self, apps: dict[str, object]):
        self.transports = {host: httpx.ASGITransport(app=app) for host, app in apps.items()}
        self.down: set[str] = set()
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        if host in self.down or host not in self.transports:
            raise httpx.ConnectError(f"connection refused: {host}", request=request)
        return await self.transports[host].handle_async_request(request)


@pytest.fixture()
def gateway():
    return FakeGateway()


@pytest.fixture()
def network(gateway, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "agent-key")
    monkeypatch.setenv("GATEWAY_URL", "http://kong:8000/api/v1")
    monkeypatch.setenv("FAULT_INJECTION_ENABLED", "true")
    apps = {}
    for domain, url in (("customer", CUSTOMER_URL), ("order", ORDER_URL)):
        monkeypatch.setenv("AGENT_PUBLIC_URL", url)
        monkeypatch.setenv("SERVICE_NAME", f"{domain}-agent")
        apps[httpx.URL(url).host] = create_specialist_app(domain, gateway_transport=httpx.MockTransport(gateway))
    return AgentNetwork(apps)


@pytest.fixture()
def coordinator(network):
    settings = Settings(
        execution_mode="a2a",
        a2a_agent_urls=(CUSTOMER_URL, ORDER_URL),
        fault_injection_enabled=True,
    )
    with TestClient(create_app(settings, a2a_transport=network)) as c:
        yield c


def ask(client, query, headers=None):
    r = client.post("/agent/query", json={"query": query}, headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ specialist agents
async def _send(network, url, message):
    http = httpx.AsyncClient(transport=network)
    client = await create_client(url, ClientConfig(streaming=False, httpx_client=http))
    task = None
    async for event in client.send_message(SendMessageRequest(message=message)):
        task = event.task
    await http.aclose()
    return task


def test_agent_cards_advertise_skills(network):
    async def run():
        async with httpx.AsyncClient(transport=network) as http:
            customer = (await http.get(f"{CUSTOMER_URL}/.well-known/agent-card.json")).json()
            order = (await http.get(f"{ORDER_URL}/.well-known/agent-card.json")).json()
        return customer, order

    import asyncio
    customer, order = asyncio.run(run())
    assert customer["name"] == "Customer Agent"
    assert [s["id"] for s in customer["skills"]] == ["get_customer"]
    assert {s["id"] for s in order["skills"]} == {"get_latest_order", "list_customer_orders", "get_order"}
    assert order["supportedInterfaces"][0]["url"] == f"{ORDER_URL}/a2a"


@pytest.mark.anyio
async def test_specialist_executes_structured_task(network):
    msg = new_data_message({"skill": "get_order", "args": {"order_id": "O1001"}}, role=Role.ROLE_USER)
    task = await _send(network, ORDER_URL, msg)
    assert TaskState.Name(task.status.state) == "TASK_STATE_COMPLETED"
    result = get_data_parts(task.artifacts[0].parts)[0]
    assert result["outcome"] == "ok" and result["data"]["status"] == "SHIPPED"


@pytest.mark.anyio
async def test_specialist_accepts_free_text(network):
    task = await _send(network, ORDER_URL, new_text_message("latest order of customer C001", role=Role.ROLE_USER))
    result = get_data_parts(task.artifacts[0].parts)[0]
    assert result["tool"] == "get_latest_order" and result["data"]["order_id"] == "O1003"


@pytest.mark.anyio
async def test_specialist_input_required_without_id(network):
    task = await _send(network, CUSTOMER_URL, new_text_message("who is my customer?", role=Role.ROLE_USER))
    assert TaskState.Name(task.status.state) == "TASK_STATE_INPUT_REQUIRED"
    assert "customer ID" in get_message_text(task.status.message)


@pytest.mark.anyio
async def test_specialist_rejects_foreign_skill(network):
    msg = new_data_message({"skill": "get_order", "args": {"order_id": "O1001"}}, role=Role.ROLE_USER)
    task = await _send(network, CUSTOMER_URL, msg)
    assert TaskState.Name(task.status.state) == "TASK_STATE_REJECTED"


@pytest.mark.anyio
async def test_specialist_task_fails_when_backend_down(network, gateway):
    gateway.down.add("order")
    msg = new_data_message({"skill": "get_order", "args": {"order_id": "O1001"}}, role=Role.ROLE_USER)
    task = await _send(network, ORDER_URL, msg)
    assert TaskState.Name(task.status.state) == "TASK_STATE_FAILED"
    assert get_data_parts(task.artifacts[0].parts)[0]["outcome"] == "unavailable"


# ------------------------------------------------------------------ coordinator over A2A
def test_order_via_a2a_delegation(coordinator):
    body = ask(coordinator, "What is the status of order O1001?")
    assert body["executor"] == "a2a"
    step = body["steps"][0]
    assert step["via"] == "a2a Order Agent"
    assert step["a2a"]["state"] == "TASK_STATE_COMPLETED"
    assert step["a2a"]["task_id"]
    assert "SHIPPED" in body["answer"]


def test_multi_agent_coordination_key_scenario(coordinator):
    body = ask(coordinator, "Find customer C001 and tell me their latest order status.")
    assert body["status"] == "completed"
    assert [s["a2a"]["agent"] for s in body["steps"]] == ["Customer Agent", "Order Agent"]
    assert "Somchai" in body["answer"] and "PROCESSING" in body["answer"]
    assert "1 x Mesh" in body["answer"]  # ints survive the protobuf Struct round-trip
    assert body["data"]["latest_orders"]["C001"]["items"][0]["quantity"] == 1


def test_unknown_customer_over_a2a(coordinator):
    body = ask(coordinator, "Show customer C999 and their latest order")
    assert body["status"] == "not_found"
    assert body["steps"][0]["a2a"]["state"] == "TASK_STATE_COMPLETED"  # a definitive "does not exist"
    assert body["steps"][1]["outcome"] == "skipped"
    assert body["steps"][1]["a2a"] is None  # never delegated
    assert body["data"] == {}


def test_order_agent_unavailable(coordinator, network):
    network.down.add("order-agent")
    body = ask(coordinator, "Show customer C001 and their latest order")
    assert body["status"] == "partial"
    assert body["steps"][0]["outcome"] == "ok"
    assert body["steps"][1]["error_code"] == "AGENT_UNAVAILABLE"
    assert "Order Agent is not reachable" in body["answer"]
    assert "latest_orders" not in body["data"]


def test_agent_comes_back_after_outage(coordinator, network):
    network.down.add("order-agent")
    assert ask(coordinator, "status of order O1001")["status"] == "failed"
    network.down.clear()
    assert ask(coordinator, "status of order O1001")["status"] == "completed"


def test_backend_down_behind_agent(coordinator, gateway):
    gateway.down.add("order")
    body = ask(coordinator, "Show customer C001 and their latest order")
    assert body["status"] == "partial"
    assert body["steps"][1]["a2a"]["state"] == "TASK_STATE_FAILED"
    assert body["steps"][1]["http_status"] == 503
    assert "Order service is currently unavailable" in body["answer"]


def test_fault_injection_travels_over_a2a(coordinator):
    body = ask(coordinator, "Show customer C001 and their latest order",
               headers={"X-Simulate-Downstream-Target": "order", "X-Simulate-Downstream-Status": "500"})
    assert body["status"] == "partial"
    assert body["steps"][1]["error_code"] == "SIMULATED_FAILURE"


def test_correlation_id_flows_coordinator_to_agent_to_gateway(coordinator, network, gateway):
    body = ask(coordinator, "Show customer C001", headers={"X-Correlation-ID": "corr-a2a-1"})
    assert body["correlation_id"] == "corr-a2a-1"
    a2a_calls = [r for r in network.requests if r.url.path == "/a2a"]
    assert a2a_calls and all(r.headers["X-Correlation-ID"] == "corr-a2a-1" for r in a2a_calls)
    assert gateway.calls[-1].headers["X-Correlation-ID"] == "corr-a2a-1"


def test_discovery_endpoint(coordinator, network):
    agents = coordinator.get("/agent/agents").json()["agents"]
    assert {a["name"] for a in agents} == {"Customer Agent", "Order Agent"}
    assert all(a["reachable"] for a in agents)
    network.down.add("customer-agent")
    agents = coordinator.get("/agent/agents").json()["agents"]
    assert [a["reachable"] for a in agents] == [False, True]


def test_task_status_endpoint(coordinator):
    body = ask(coordinator, "What is the status of order O1001?")
    a2a = body["steps"][0]["a2a"]
    r = coordinator.get(f"/agent/a2a/tasks/order-agent/{a2a['task_id']}")
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task["id"] == a2a["task_id"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert coordinator.get("/agent/a2a/tasks/nobody/123").status_code == 404


# ------------------------------------------------------------------ artifact validation
from app.a2a_executor import validate_artifact  # noqa: E402
from app.planner import PlannedStep  # noqa: E402
from comco_agentkit import ContractViolation  # noqa: E402
from test_coordinator import CUSTOMERS  # noqa: E402

STEP = PlannedStep("customer_C001", "get_customer", {"customer_id": "C001"}, "test")


def test_artifact_for_another_step_is_rejected():
    with pytest.raises(ContractViolation, match="expected 'get_customer'"):
        validate_artifact(STEP, {"tool": "get_order", "args": {"order_id": "O1001"}, "outcome": "ok"})
    with pytest.raises(ContractViolation, match="args"):
        validate_artifact(STEP, {"tool": "get_customer", "args": {"customer_id": "C002"}, "outcome": "ok",
                                 "data": {**CUSTOMERS["C001"], "customer_id": "C002"}})


def test_artifact_with_invalid_data_or_outcome_is_rejected():
    bad = {**CUSTOMERS["C001"], "tier": "DIAMOND"}
    with pytest.raises(ContractViolation, match="tier"):
        validate_artifact(STEP, {"tool": "get_customer", "args": STEP.args, "outcome": "ok", "data": bad})
    with pytest.raises(ContractViolation, match="unknown outcome"):
        validate_artifact(STEP, {"tool": "get_customer", "args": STEP.args, "outcome": "great"})


def test_valid_artifact_keeps_only_known_fields():
    art = {"tool": "get_customer", "args": STEP.args, "outcome": "ok", "http_status": 200,
           "data": CUSTOMERS["C001"], "injected": "x", "via": "somewhere-else"}
    clean = validate_artifact(STEP, art)
    assert set(clean) == {"outcome", "http_status", "data", "error_code", "detail"}
    assert clean["data"]["customer_id"] == "C001"


def test_specialist_validates_before_answering(coordinator, gateway):
    gateway.tamper = lambda path, body: {**body, "customer_id": "C002"} if path.startswith("/customers/") else body
    body = ask(coordinator, "Show customer C001")
    step = body["steps"][0]
    assert step["a2a"]["agent"] == "Customer Agent"
    assert step["error_code"] == "INVALID_UPSTREAM_RESPONSE"  # caught inside the Customer Agent
    assert body["data"] == {}
