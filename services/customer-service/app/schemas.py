from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_serializer

CUSTOMER_ID_PATTERN = r"^C\d{3,}$"


class CustomerTier(str, Enum):
    STANDARD = "STANDARD"
    GOLD = "GOLD"
    PLATINUM = "PLATINUM"


class CustomerStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class CustomerCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "first_name": "Malee",
                "last_name": "Srisuk",
                "email": "malee.s@example.com",
                "phone": "+66-81-555-0199",
                "tier": "STANDARD",
            }
        },
    )

    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    email: EmailStr
    phone: str | None = Field(default=None, pattern=r"^\+?[0-9\- ]{7,20}$")
    tier: CustomerTier = CustomerTier.STANDARD


class Customer(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    customer_id: str = Field(examples=["C001"])
    first_name: str
    last_name: str
    email: str
    phone: str | None
    tier: CustomerTier
    status: CustomerStatus
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def _utc(self, value: datetime) -> str:
        # SQLite returns naive datetimes; everything is stored as UTC.
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class CustomerPage(BaseModel):
    items: list[Customer]
    total: int
    limit: int
    offset: int
