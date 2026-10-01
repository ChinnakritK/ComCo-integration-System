"""Response contracts: validate business data BEFORE an agent uses it.

The agents must never present information that is wrong or unverified. So every
successful API response - and every result another agent sends back over A2A -
is checked here before it reaches the answer:

1. Schema    - required fields present, correct types and formats
               (IDs, enums, money as decimals, quantities as integers).
2. Identity  - the data is about what was asked for: get_customer(C001) must
               return C001, a latest order must belong to the requested
               customer, every listed order must belong to that customer.
3. Normalise - the validated model is re-serialised, so only known fields in a
               known shape flow on (unexpected fields are dropped).

Anything that fails becomes outcome "error" with code INVALID_UPSTREAM_RESPONSE:
the agent then says it could not retrieve the information instead of passing
on suspicious data.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

CUSTOMER_ID = r"^C\d{3,}$"
ORDER_ID = r"^O\d{4,}$"


class _Contract(BaseModel):
    # Ignore (and drop) unknown fields: providers may add fields without breaking
    # consumers, but nothing unvalidated is passed on.
    model_config = ConfigDict(extra="ignore", strict=False)


class CustomerData(_Contract):
    customer_id: str = Field(pattern=CUSTOMER_ID)
    first_name: str = Field(min_length=1)
    last_name: str = Field(min_length=1)
    email: str = Field(min_length=3)
    phone: str | None = None
    tier: Literal["STANDARD", "GOLD", "PLATINUM"]
    status: Literal["ACTIVE", "INACTIVE"]
    created_at: datetime
    updated_at: datetime


class OrderItemData(_Contract):
    sku: str = Field(min_length=1)
    name: str = Field(min_length=1)
    quantity: int = Field(ge=1)
    unit_price: Decimal = Field(ge=0)


class OrderData(_Contract):
    order_id: str = Field(pattern=ORDER_ID)
    customer_id: str = Field(pattern=CUSTOMER_ID)
    status: Literal["PENDING", "PAID", "PROCESSING", "SHIPPED", "DELIVERED", "CANCELLED"]
    items: list[OrderItemData] = Field(min_length=1)
    total_amount: Decimal = Field(ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    created_at: datetime
    updated_at: datetime


class OrderPageData(_Contract):
    items: list[OrderData]
    total: int = Field(ge=0)
    limit: int
    offset: int


class ContractViolation(Exception):
    """The provider's response does not match the contract or the request."""


def _dump(model: BaseModel) -> dict:
    data = model.model_dump(mode="json")
    return _money_as_strings(data)


def _money_as_strings(value):
    # Keep money formatted exactly like the REST APIs ("2490.00").
    if isinstance(value, dict):
        return {
            k: (f"{Decimal(v):.2f}" if k in ("total_amount", "unit_price") and v is not None else _money_as_strings(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_money_as_strings(v) for v in value]
    return value


def validate_tool_data(tool: str, args: dict, data) -> dict:
    """Validate and normalise the data returned for `tool(args)`. Raises ContractViolation."""
    if not isinstance(data, dict):
        raise ContractViolation(f"expected a JSON object, got {type(data).__name__}")
    try:
        if tool == "get_customer":
            customer = CustomerData.model_validate(data)
            if customer.customer_id != args["customer_id"]:
                raise ContractViolation(
                    f"asked for customer {args['customer_id']} but received {customer.customer_id}"
                )
            return _dump(customer)

        if tool == "get_order":
            order = OrderData.model_validate(data)
            if order.order_id != args["order_id"]:
                raise ContractViolation(f"asked for order {args['order_id']} but received {order.order_id}")
            return _dump(order)

        if tool == "get_latest_order":
            order = OrderData.model_validate(data)
            if order.customer_id != args["customer_id"]:
                raise ContractViolation(
                    f"latest order {order.order_id} belongs to {order.customer_id}, not {args['customer_id']}"
                )
            return _dump(order)

        if tool == "list_customer_orders":
            page = OrderPageData.model_validate(data)
            foreign = sorted({o.order_id for o in page.items if o.customer_id != args["customer_id"]})
            if foreign:
                raise ContractViolation(f"orders {foreign} do not belong to customer {args['customer_id']}")
            if page.total < len(page.items):
                raise ContractViolation(f"total={page.total} is smaller than the {len(page.items)} items returned")
            return _dump(page)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ())) or "body"
        raise ContractViolation(
            f"{exc.error_count()} schema error(s); first: {where}: {first.get('msg')}"
        ) from exc

    raise ContractViolation(f"no contract defined for tool '{tool}'")
