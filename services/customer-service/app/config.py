from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    service_name: str = "customer-service"
    database_url: str = "sqlite+aiosqlite:///./customer.db"
    seed_data: bool = True
    public_base_url: str = "http://localhost:8000/api/v1"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            service_name=os.getenv("SERVICE_NAME", cls.service_name),
            database_url=os.getenv("DATABASE_URL", cls.database_url),
            seed_data=os.getenv("SEED_DATA", "true").lower() == "true",
            public_base_url=os.getenv("PUBLIC_BASE_URL", cls.public_base_url),
        )
