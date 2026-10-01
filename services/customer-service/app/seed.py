"""Demo data. Chosen so every assessment scenario has a deterministic answer.

C001 - has several orders (latest = O1003, PROCESSING)  -> "customer + latest order"
C002 - has orders                                       -> general listing
C003 - exists but has NO orders                         -> "customer found, no order" path
C004 - INACTIVE customer                                -> status handling
C999 - intentionally absent                             -> "unknown customer" scenario
"""

from datetime import datetime, timezone

SEED_CUSTOMERS = [
    {
        "customer_id": "C001",
        "first_name": "Somchai",
        "last_name": "Jaidee",
        "email": "somchai.j@example.com",
        "phone": "+66-81-234-5678",
        "tier": "GOLD",
        "status": "ACTIVE",
        "created_at": datetime(2024, 3, 12, 9, 30, tzinfo=timezone.utc),
    },
    {
        "customer_id": "C002",
        "first_name": "Suda",
        "last_name": "Rakthai",
        "email": "suda.r@example.com",
        "phone": "+66-89-111-2233",
        "tier": "STANDARD",
        "status": "ACTIVE",
        "created_at": datetime(2025, 1, 5, 14, 0, tzinfo=timezone.utc),
    },
    {
        "customer_id": "C003",
        "first_name": "Anan",
        "last_name": "Wongsa",
        "email": "anan.w@example.com",
        "phone": None,
        "tier": "PLATINUM",
        "status": "ACTIVE",
        "created_at": datetime(2025, 6, 20, 8, 15, tzinfo=timezone.utc),
    },
    {
        "customer_id": "C004",
        "first_name": "Kanya",
        "last_name": "Boonmee",
        "email": "kanya.b@example.com",
        "phone": "+66-62-777-8899",
        "tier": "STANDARD",
        "status": "INACTIVE",
        "created_at": datetime(2023, 11, 1, 10, 0, tzinfo=timezone.utc),
    },
]
