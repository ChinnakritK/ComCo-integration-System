"""Demo data. C001's latest order is O1003 (PROCESSING); C003 has no orders."""

from datetime import datetime, timezone


def _item(sku, name, qty, price):
    return {"sku": sku, "name": name, "quantity": qty, "unit_price": price}


SEED_ORDERS = [
    {
        "order_id": "O1001",
        "customer_id": "C001",
        "status": "SHIPPED",
        "items": [_item("SKU-ROUTER-AX3000", "Wi-Fi 6 Router AX3000", 1, "2490.00")],
        "total_amount": "2490.00",
        "created_at": datetime(2026, 9, 10, 3, 20, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 9, 12, 7, 45, tzinfo=timezone.utc),
    },
    {
        "order_id": "O1002",
        "customer_id": "C001",
        "status": "DELIVERED",
        "items": [
            _item("SKU-SIM-5G", "5G SIM starter pack", 2, "199.00"),
            _item("SKU-PLAN-UNL", "Unlimited plan - 1 month", 1, "599.00"),
        ],
        "total_amount": "997.00",
        "created_at": datetime(2026, 8, 15, 11, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 8, 18, 9, 30, tzinfo=timezone.utc),
    },
    {
        "order_id": "O1003",
        "customer_id": "C001",
        "status": "PROCESSING",
        "items": [_item("SKU-MESH-3PK", "Mesh Wi-Fi system (3-pack)", 1, "5990.00")],
        "total_amount": "5990.00",
        "created_at": datetime(2026, 9, 27, 6, 10, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc),
    },
    {
        "order_id": "O1004",
        "customer_id": "C002",
        "status": "PENDING",
        "items": [_item("SKU-PHONE-A55", "Smartphone A55", 1, "12900.00")],
        "total_amount": "12900.00",
        "created_at": datetime(2026, 9, 25, 13, 5, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 9, 25, 13, 5, tzinfo=timezone.utc),
    },
    {
        "order_id": "O1005",
        "customer_id": "C002",
        "status": "CANCELLED",
        "items": [_item("SKU-CASE-A55", "Protective case A55", 1, "390.00")],
        "total_amount": "390.00",
        "created_at": datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 7, 3, 8, 0, tzinfo=timezone.utc),
    },
    {
        "order_id": "O1006",
        "customer_id": "C004",
        "status": "DELIVERED",
        "items": [_item("SKU-SIM-4G", "4G SIM", 1, "99.00")],
        "total_amount": "99.00",
        "created_at": datetime(2024, 2, 1, 8, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2024, 2, 4, 8, 0, tzinfo=timezone.utc),
    },
]
