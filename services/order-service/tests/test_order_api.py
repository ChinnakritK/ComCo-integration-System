"""Order Service API tests (SQLite in place of PostgreSQL, no Docker needed)."""

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    with TestClient(create_app(settings)) as c:
        yield c


def test_get_order(client):
    r = client.get("/orders/O1001")
    assert r.status_code == 200
    body = r.json()
    assert body["customer_id"] == "C001"
    assert body["status"] == "SHIPPED"
    assert body["total_amount"] == "2490.00"


def test_unknown_order(client):
    r = client.get("/orders/O9999")
    assert r.status_code == 404
    assert r.json()["code"] == "ORDER_NOT_FOUND"


def test_latest_order_for_customer(client):
    r = client.get("/orders/latest", params={"customer_id": "C001"})
    assert r.status_code == 200
    assert r.json()["order_id"] == "O1003"
    assert r.json()["status"] == "PROCESSING"


def test_latest_order_customer_without_orders(client):
    r = client.get("/orders/latest", params={"customer_id": "C003"})
    assert r.status_code == 404
    assert r.json()["code"] == "NO_ORDERS_FOR_CUSTOMER"


def test_latest_order_requires_customer_id(client):
    r = client.get("/orders/latest")
    assert r.status_code == 422


def test_list_orders_newest_first(client):
    r = client.get("/orders", params={"customer_id": "C001"})
    body = r.json()
    assert body["total"] == 3
    assert [o["order_id"] for o in body["items"]] == ["O1003", "O1001", "O1002"]
    oldest = client.get("/orders", params={"customer_id": "C001", "sort": "created_at"}).json()
    assert oldest["items"][0]["order_id"] == "O1002"


def test_filter_by_status(client):
    body = client.get("/orders", params={"status": "PENDING"}).json()
    assert [o["order_id"] for o in body["items"]] == ["O1004"]


def test_create_order_computes_total(client):
    payload = {
        "customer_id": "C003",
        "items": [
            {"sku": "SKU-A", "name": "Item A", "quantity": 2, "unit_price": "100.50"},
            {"sku": "SKU-B", "name": "Item B", "quantity": 1, "unit_price": "49.00"},
        ],
    }
    r = client.post("/orders", json=payload)
    assert r.status_code == 201
    body = r.json()
    assert body["order_id"] == "O1007"
    assert body["total_amount"] == "250.00"
    assert body["status"] == "PENDING"
    assert client.get("/orders/latest", params={"customer_id": "C003"}).json()["order_id"] == "O1007"


def test_create_order_validation(client):
    r = client.post("/orders", json={"customer_id": "X1", "items": []})
    assert r.status_code == 422
    fields = {e["field"] for e in r.json()["errors"]}
    assert {"customer_id", "items"} <= fields


def test_status_transitions(client):
    ok = client.patch("/orders/O1004/status", json={"status": "PAID"})
    assert ok.status_code == 200
    assert ok.json()["status"] == "PAID"

    bad = client.patch("/orders/O1002/status", json={"status": "PENDING"})
    assert bad.status_code == 409
    body = bad.json()
    assert body["code"] == "INVALID_STATUS_TRANSITION"
    assert body["allowed_transitions"] == []
