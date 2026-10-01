from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_serializer

CUSTOMER_ID_PATTERN = r"^C\d{3,}$"
ORDER_ID_PATTERN = r"^O\d{4,}$"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    PROCESSING = "PROCESSING"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"


# Allowed lifecycle transitions; anything else is rejected with 409.
ALLOWED_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.PENDING: {OrderStatus.PAID, OrderStatus.CANCELLED},
    OrderStatus.PAID: {OrderStatus.PROCESSING, OrderStatus.CANCELLED},
    OrderStatus.PROCESSING: {OrderStatus.SHIPPED, OrderStatus.CANCELLED},
    OrderStatus.SHIPPED: {OrderStatus.DELIVERED},
    OrderStatus.DELIVERED: set(),
    OrderStatus.CANCELLED: set(),
}


class OrderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str = Field(min_length=1, max_length=64, examples=["SKU-ROUTER-AX3000"])
    name: str = Field(min_length=1, max_length=200)
    quantity: int = Field(ge=1, le=1000)
    unit_price: Decimal = Field(ge=0, max_digits=12, decimal_places=2)


class OrderCreate(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "customer_id": "C002",
                "currency": "THB",
                "items": [
                    {"sku": "SKU-SIM-5G", "name": "5G SIM starter pack", "quantity": 2, "unit_price": "199.00"}
                ],
            }
        },
    )

    customer_id: str = Field(pattern=CUSTOMER_ID_PATTERN)
    items: list[OrderItem] = Field(min_length=1, max_length=50)
    currency: str = Field(default="THB", pattern=r"^[A-Z]{3}$")


class OrderStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: OrderStatus


class Order(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    order_id: str = Field(examples=["O1001"])
    customer_id: str
    status: OrderStatus
    items: list[OrderItem]
    total_amount: Decimal
    currency: str
    created_at: datetime
    updated_at: datetime

    @field_serializer("total_amount")
    def _money(self, value: Decimal) -> str:
        return f"{Decimal(value):.2f}"

    @field_serializer("created_at", "updated_at")
    def _utc(self, value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class OrderPage(BaseModel):
    items: list[Order]
    total: int
    limit: int
    offset: int
