"""End-to-end tests through the Kong gateway (requires `docker compose up`).

    make e2e        # or: GATEWAY_URL=http://localhost:8000 pytest tests/e2e -v

Covers the Phase 1 rows of the assessment's "Required Demonstration" table:
single API, unknown customer, unauthorized request, access control,
excessive requests and correlation/tracing headers.
"""

import os
import time
import uuid

import httpx
import pytest

GATEWAY = os.getenv("GATEWAY_URL", "http://localhost:8000")
API = f"{GATEWAY}/api/v1"

DEMO_KEY = {"apikey": "demo-client-key"}
TRIAL_KEY = {"apikey": "trial-client-key"}
PARTNER_KEY = {"apikey": "orders-partner-key"}


def _gateway_up() -> bool:
    try:
        httpx.get(GATEWAY, timeout=2)
        return True
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(not _gateway_up(), reason=f"gateway not reachable at {GATEWAY}")


@pytest.fixture(scope="module")
def http():
    with httpx.Client(timeout=20) as client:
        yield client


# ------------------------------------------------------------------ security
def test_request_without_api_key_is_rejected(http):
    r = http.get(f"{API}/customers/C001")
    assert r.status_code == 401
    assert r.json()["source"] == "kong-gateway"


def test_request_with_invalid_api_key_is_rejected(http):
    r = http.get(f"{API}/customers/C001", headers={"apikey": "not-a-real-key"})
    assert r.status_code == 401


def test_acl_blocks_consumer_without_permission(http):
    # orders-partner may read orders ...
    assert http.get(f"{API}/orders/O1001", headers=PARTNER_KEY).status_code == 200
    # ... but not customer data.
    r = http.get(f"{API}/customers/C001", headers=PARTNER_KEY)
    assert r.status_code == 403


# ------------------------------------------------------------------ routing + API behaviour
def test_get_customer_through_gateway(http):
    r = http.get(f"{API}/customers/C001", headers=DEMO_KEY)
    assert r.status_code == 200
    assert r.json()["customer_id"] == "C001"


def test_unknown_customer_returns_problem_details(http):
    r = http.get(f"{API}/customers/C999", headers=DEMO_KEY)
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == "CUSTOMER_NOT_FOUND"


def test_latest_order_through_gateway(http):
    r = http.get(f"{API}/orders/latest", params={"customer_id": "C001"}, headers=DEMO_KEY)
    assert r.status_code == 200
    assert r.json()["order_id"] == "O1003"


def test_backend_is_not_reachable_on_unrouted_path(http):
    r = http.get(f"{GATEWAY}/customers/C001", headers=DEMO_KEY)
    assert r.status_code == 404  # no route - internal paths are not exposed


# ------------------------------------------------------------------ observability
def test_correlation_id_is_propagated_end_to_end(http):
    cid = f"e2e-{uuid.uuid4()}"
    r = http.get(f"{API}/customers/C001", headers={**DEMO_KEY, "X-Correlation-ID": cid})
    assert r.headers["X-Correlation-ID"] == cid

    r = http.get(f"{API}/customers/C999", headers={**DEMO_KEY, "X-Correlation-ID": cid})
    assert r.json()["correlation_id"] == cid  # the backend saw the same ID
    assert r.json().get("trace_id")  # and continued the gateway's trace


def test_gateway_generates_correlation_id_when_missing(http):
    r = http.get(f"{API}/customers/C001", headers=DEMO_KEY)
    assert r.headers.get("X-Correlation-ID")


def test_prometheus_metrics_exposed(http):
    r = http.get(GATEWAY.replace(":8000", ":8100") + "/metrics")
    assert r.status_code == 200
    assert "kong_http_requests_total" in r.text


# ------------------------------------------------------------------ failure handling
def test_upstream_timeout_returns_504(http):
    # Requires FAULT_INJECTION_ENABLED=true (default in docker-compose.yml).
    r = http.get(f"{API}/orders/O1001", headers={**DEMO_KEY, "X-Simulate-Delay-Ms": "6000"})
    assert r.status_code == 504


def test_downstream_error_is_problem_details(http):
    r = http.get(f"{API}/orders/O1001", headers={**DEMO_KEY, "X-Simulate-Status": "500"})
    assert r.status_code == 500
    assert r.json()["code"] == "SIMULATED_FAILURE"


# ------------------------------------------------------------------ traffic control (keep last: consumes quota)
def test_rate_limit_returns_429_for_trial_plan(http):
    time.sleep(max(0, 61 - time.localtime().tm_sec))  # start of a fresh minute window
    statuses = [http.get(f"{API}/customers/C001", headers=TRIAL_KEY).status_code for _ in range(7)]
    assert statuses[:5] == [200] * 5
    assert statuses[5:] == [429, 429]
    r = http.get(f"{API}/customers/C001", headers=TRIAL_KEY)
    assert "RateLimit-Reset" in r.headers or "Retry-After" in r.headers
