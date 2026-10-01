"""Customer Service unit/API tests (SQLite in place of PostgreSQL, no Docker needed)."""

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    with TestClient(create_app(settings)) as c:
        yield c


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/ready").json()["status"] == "ready"


def test_get_existing_customer(client):
    r = client.get("/customers/C001", headers={"X-Correlation-ID": "test-corr-1"})
    assert r.status_code == 200
    body = r.json()
    assert body["customer_id"] == "C001"
    assert body["first_name"] == "Somchai"
    assert body["tier"] == "GOLD"
    assert body["created_at"].endswith("Z")
    # Correlation ID set by the gateway is propagated back.
    assert r.headers["X-Correlation-ID"] == "test-corr-1"


def test_unknown_customer_returns_problem_details(client):
    r = client.get("/customers/C999")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["code"] == "CUSTOMER_NOT_FOUND"
    assert body["status"] == 404
    assert body["instance"] == "/customers/C999"
    assert body["correlation_id"]  # generated when not supplied


def test_malformed_customer_id_is_rejected(client):
    r = client.get("/customers/abc")
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "VALIDATION_FAILED"
    assert body["errors"][0]["field"] == "path.customer_id"


def test_list_and_filter(client):
    r = client.get("/customers", params={"status": "ACTIVE", "limit": 2})
    body = r.json()
    assert r.status_code == 200
    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert client.get("/customers", params={"limit": 0}).status_code == 422


def test_create_customer_and_duplicate_email(client):
    payload = {"first_name": "Malee", "last_name": "Srisuk", "email": "Malee.S@example.com"}
    r = client.post("/customers", json=payload)
    assert r.status_code == 201
    created = r.json()
    assert created["customer_id"] == "C005"
    assert created["email"] == "malee.s@example.com"
    assert r.headers["Location"].endswith("/customers/C005")

    dup = client.post("/customers", json=payload)
    assert dup.status_code == 409
    assert dup.json()["code"] == "EMAIL_ALREADY_REGISTERED"


def test_create_customer_validation(client):
    r = client.post("/customers", json={"first_name": "", "last_name": "X", "email": "not-an-email", "extra": 1})
    assert r.status_code == 422
    fields = {e["field"] for e in r.json()["errors"]}
    assert {"first_name", "email", "extra"} <= fields


def test_fault_injection(client, monkeypatch):
    monkeypatch.setenv("FAULT_INJECTION_ENABLED", "true")
    r = client.get("/customers/C001", headers={"X-Simulate-Status": "503"})
    assert r.status_code == 503
    assert r.json()["code"] == "SIMULATED_FAILURE"


def test_fault_injection_disabled_by_default(client):
    r = client.get("/customers/C001", headers={"X-Simulate-Status": "503"})
    assert r.status_code == 200


def test_openapi_document(client):
    spec = client.get("/openapi.json").json()
    assert "/customers/{customer_id}" in spec["paths"]
    assert "Problem" in spec["components"]["schemas"]
    assert spec["components"]["securitySchemes"]["ApiKeyAuth"]["name"] == "apikey"
