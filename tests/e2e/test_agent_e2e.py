"""End-to-end tests for the Agent API (Phase 2) and A2A (Phase 3) through Kong (requires `docker compose up`).

    python -m pytest tests/e2e -v
"""

import os
import uuid

import httpx
import pytest

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8000")
AGENT = f"{GATEWAY}/api/v1/agent"
DEMO_KEY = {"apikey": "demo-client-key"}


def _gateway_up() -> bool:
    try:
        httpx.get(GATEWAY, timeout=2)
        return True
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(not _gateway_up(), reason=f"gateway not reachable at {GATEWAY}")


@pytest.fixture(scope="module")
def http():
    with httpx.Client(timeout=40) as client:
        yield client


def ask(http, query, headers=None):
    r = http.post(f"{AGENT}/query", json={"query": query}, headers={**DEMO_KEY, **(headers or {})})
    assert r.status_code == 200, r.text
    return r.json()


def test_agent_api_requires_api_key(http):
    assert http.post(f"{AGENT}/query", json={"query": "Show customer C001"}).status_code == 401


def test_agent_api_acl(http):
    r = http.post(f"{AGENT}/query", json={"query": "Show customer C001"}, headers={"apikey": "orders-partner-key"})
    assert r.status_code == 403


def test_single_tool(http):
    body = ask(http, "Show customer C001")
    assert body["status"] == "completed"
    assert [s["tool"] for s in body["steps"]] == ["get_customer"]
    assert "Somchai" in body["answer"]


def test_multi_tool_orchestration(http):
    body = ask(http, "Show customer C001 and their latest order")
    assert [s["tool"] for s in body["steps"]] == ["get_customer", "get_latest_order"]
    assert "O1003" in body["answer"]


def test_key_scenario(http):
    body = ask(http, "Find customer C001 and tell me their latest order status.")
    assert body["status"] == "completed"
    assert body["data"]["latest_orders"]["C001"]["status"] == "PROCESSING"


def test_unknown_customer(http):
    body = ask(http, "Show customer C999")
    assert body["status"] == "not_found"
    assert body["data"] == {}


def test_downstream_failure_is_reported_not_fabricated(http):
    body = ask(
        http,
        "Show customer C001 and their latest order",
        headers={"X-Simulate-Downstream-Target": "order", "X-Simulate-Downstream-Status": "500"},
    )
    assert body["status"] == "partial"
    assert "could not retrieve the latest order" in body["answer"]
    assert "latest_orders" not in body["data"]


def test_one_correlation_id_across_agent_and_backend_calls(http):
    cid = f"agent-e2e-{uuid.uuid4()}"
    body = ask(http, "Show customer C001", headers={"X-Correlation-ID": cid})
    assert body["correlation_id"] == cid
    assert body["trace_id"]


def test_capabilities(http):
    r = http.get(f"{AGENT}/capabilities", headers=DEMO_KEY)
    assert r.status_code == 200
    assert "get_latest_order" in {t["name"] for t in r.json()["tools"]}


# ------------------------------------------------------------------ Phase 3: A2A
def test_a2a_discovery_lists_specialist_agents(http):
    body = http.get(f"{AGENT}/agents", headers=DEMO_KEY).json()
    assert body["execution_mode"] == "a2a"
    agents = {a["name"]: a for a in body["agents"]}
    assert agents["Customer Agent"]["reachable"] and agents["Order Agent"]["reachable"]
    assert {s["id"] for s in agents["Order Agent"]["skills"]} >= {"get_order", "get_latest_order"}


def test_a2a_delegation_and_task_status(http):
    body = ask(http, "What is the status of order O1001?")
    step = body["steps"][0]
    assert body["executor"] == "a2a"
    assert step["a2a"]["agent"] == "Order Agent"
    assert step["a2a"]["state"] == "TASK_STATE_COMPLETED"
    task = http.get(f"{AGENT}/a2a/tasks/order-agent/{step['a2a']['task_id']}", headers=DEMO_KEY).json()["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["artifacts"]


def test_a2a_multi_agent_coordination(http):
    body = ask(http, "Find customer C001 and tell me their latest order status.")
    assert [s["a2a"]["agent"] for s in body["steps"]] == ["Customer Agent", "Order Agent"]
    assert body["status"] == "completed"


def test_specialist_agent_keys_are_least_privilege(http):
    api = f"{GATEWAY}/api/v1"
    assert http.get(f"{api}/customers/C001", headers={"apikey": "customer-agent-key"}).status_code == 200
    assert http.get(f"{api}/orders/O1001", headers={"apikey": "customer-agent-key"}).status_code == 403
    assert http.get(f"{api}/orders/O1001", headers={"apikey": "order-agent-key"}).status_code == 200
    assert http.get(f"{api}/customers/C001", headers={"apikey": "order-agent-key"}).status_code == 403
