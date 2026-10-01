import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from enum import Enum
from typing import Annotated, AsyncIterator

from fastapi import Depends, FastAPI, Path, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from comco_common import (
    ProblemException,
    RequestContextMiddleware,
    configure_logging,
    get_logger,
    register_problem_handlers,
    setup_telemetry,
)
from comco_common.openapi import install_openapi_extras, problem_responses

from . import db
from .config import Settings
from .models import Order as OrderRow
from .schemas import (
    ALLOWED_TRANSITIONS,
    CUSTOMER_ID_PATTERN,
    ORDER_ID_PATTERN,
    Order,
    OrderCreate,
    OrderPage,
    OrderStatus,
    OrderStatusUpdate,
)

log = get_logger(__name__)


class SortOrder(str, Enum):
    newest = "-created_at"
    oldest = "created_at"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.service_name)
    engine = db.build_engine(settings.database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        for attempt in range(1, 11):
            try:
                await db.init_db(engine, settings.seed_data)
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("db_init_retry", attempt=attempt, error=type(exc).__name__)
                if attempt == 10:
                    raise
                await asyncio.sleep(2)
        log.info("service_started")
        yield
        await engine.dispose()

    app = FastAPI(
        title="ComCo Order API",
        version="1.0.0",
        summary="Customer orders for the ComCo Customer Service Integration Platform.",
        description=(
            "Owned by the **Order Service**, backed by its own PostgreSQL database. Orders reference "
            "customers by `customer_id` only (database-per-service - no cross-service joins).\n\n"
            "External consumers reach this API **only through the Kong API gateway** at "
            "`/api/v1/orders` using an `apikey` header. All errors use RFC 7807 Problem Details."
        ),
        servers=[{"url": settings.public_base_url, "description": "Via Kong API gateway"}],
        lifespan=lifespan,
        generate_unique_id_function=lambda route: route.name,  # operationId = handler name
    )
    app.add_middleware(RequestContextMiddleware)
    register_problem_handlers(app)
    setup_telemetry(app, settings.service_name, engine)

    async def get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    SessionDep = Annotated[AsyncSession, Depends(get_session)]
    OrderId = Annotated[str, Path(pattern=ORDER_ID_PATTERN, description="Order identifier", examples=["O1001"])]

    # ---------------------------------------------------------------- health
    @app.get("/health", tags=["ops"], summary="Liveness probe", include_in_schema=False)
    async def health() -> dict:
        return {"status": "ok", "service": settings.service_name}

    @app.get("/ready", tags=["ops"], summary="Readiness probe (checks the database)", include_in_schema=False)
    async def ready(session: SessionDep) -> dict:
        await db.ping(session)
        return {"status": "ready", "service": settings.service_name}

    # ---------------------------------------------------------------- orders
    @app.get(
        "/orders",
        response_model=OrderPage,
        tags=["orders"],
        summary="List / search orders",
        description="Filter by customer and status. Default sort is newest first.",
        responses=problem_responses(r422="Validation error", r503="Data store unavailable"),
    )
    async def list_orders(
        session: SessionDep,
        customer_id: Annotated[str | None, Query(pattern=CUSTOMER_ID_PATTERN)] = None,
        status_: Annotated[OrderStatus | None, Query(alias="status")] = None,
        sort: SortOrder = SortOrder.newest,
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> OrderPage:
        query = select(OrderRow)
        if customer_id:
            query = query.where(OrderRow.customer_id == customer_id)
        if status_:
            query = query.where(OrderRow.status == status_.value)
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        order_by = OrderRow.created_at.desc() if sort == SortOrder.newest else OrderRow.created_at.asc()
        rows = (await session.scalars(query.order_by(order_by, OrderRow.order_id).limit(limit).offset(offset))).all()
        return OrderPage(items=[Order.model_validate(r) for r in rows], total=total or 0, limit=limit, offset=offset)

    @app.get(
        "/orders/latest",
        response_model=Order,
        tags=["orders"],
        summary="Get a customer's most recent order",
        description=(
            "Purpose-built for the *customer + latest order* use case (and the agents' "
            "`get_latest_order` tool). Returns 404 `NO_ORDERS_FOR_CUSTOMER` when the customer "
            "has no orders. The Order Service does not verify that the customer exists - "
            "that is the Customer Service's responsibility."
        ),
        responses=problem_responses(r404="No orders for customer", r422="Validation error", r503="Data store unavailable"),
    )
    async def latest_order(
        session: SessionDep,
        customer_id: Annotated[str, Query(pattern=CUSTOMER_ID_PATTERN, examples=["C001"])],
    ) -> Order:
        row = await session.scalar(
            select(OrderRow)
            .where(OrderRow.customer_id == customer_id)
            .order_by(OrderRow.created_at.desc(), OrderRow.order_id.desc())
            .limit(1)
        )
        if row is None:
            raise ProblemException(
                404, f"No orders found for customer {customer_id}.", code="NO_ORDERS_FOR_CUSTOMER"
            )
        return Order.model_validate(row)

    @app.get(
        "/orders/{order_id}",
        response_model=Order,
        tags=["orders"],
        summary="Get an order by ID",
        responses=problem_responses(r404="Order not found", r422="Validation error", r503="Data store unavailable"),
    )
    async def get_order(order_id: OrderId, session: SessionDep) -> Order:
        row = await session.get(OrderRow, order_id)
        if row is None:
            raise ProblemException(404, f"Order {order_id} does not exist.", code="ORDER_NOT_FOUND")
        return Order.model_validate(row)

    @app.post(
        "/orders",
        response_model=Order,
        status_code=status.HTTP_201_CREATED,
        tags=["orders"],
        summary="Place an order",
        description="The total is always computed server-side from the line items.",
        responses=problem_responses(r409="Conflict", r422="Validation error", r503="Data store unavailable"),
    )
    async def create_order(payload: OrderCreate, session: SessionDep, response: Response) -> Order:
        total = sum((item.unit_price * item.quantity for item in payload.items), Decimal("0"))
        row = OrderRow(
            order_id=await _next_order_id(session),
            customer_id=payload.customer_id,
            status=OrderStatus.PENDING.value,
            items=[item.model_dump(mode="json") for item in payload.items],
            total_amount=total.quantize(Decimal("0.01")),
            currency=payload.currency,
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise ProblemException(409, "Order could not be created due to a conflict.", code="CONFLICT")
        await session.refresh(row)
        response.headers["Location"] = f"{settings.public_base_url}/orders/{row.order_id}"
        log.info("order_created", order_id=row.order_id, customer_id=row.customer_id)
        return Order.model_validate(row)

    @app.patch(
        "/orders/{order_id}/status",
        response_model=Order,
        tags=["orders"],
        summary="Change an order's status",
        description="Enforces the order lifecycle, e.g. PENDING -> PAID -> PROCESSING -> SHIPPED -> DELIVERED.",
        responses=problem_responses(
            r404="Order not found", r409="Invalid status transition", r422="Validation error", r503="Data store unavailable"
        ),
    )
    async def update_status(order_id: OrderId, payload: OrderStatusUpdate, session: SessionDep) -> Order:
        row = await session.get(OrderRow, order_id)
        if row is None:
            raise ProblemException(404, f"Order {order_id} does not exist.", code="ORDER_NOT_FOUND")
        current = OrderStatus(row.status)
        if payload.status not in ALLOWED_TRANSITIONS[current]:
            allowed = sorted(s.value for s in ALLOWED_TRANSITIONS[current])
            raise ProblemException(
                409,
                f"Cannot change order {order_id} from {current.value} to {payload.status.value}.",
                code="INVALID_STATUS_TRANSITION",
                extra={"allowed_transitions": allowed},
            )
        row.status = payload.status.value
        await session.commit()
        await session.refresh(row)
        log.info("order_status_changed", order_id=order_id, old=current.value, new=row.status)
        return Order.model_validate(row)

    install_openapi_extras(app)
    return app


async def _next_order_id(session: AsyncSession) -> str:
    # Prototype ID generation (see README limitations - a DB sequence would be used in production).
    ids = (await session.scalars(select(OrderRow.order_id))).all()
    numbers = [int(i[1:]) for i in ids if i[1:].isdigit()]
    return f"O{(max(numbers, default=1000) + 1):04d}"


app = create_app()
