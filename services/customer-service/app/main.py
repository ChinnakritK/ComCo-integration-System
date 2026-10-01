import asyncio
from contextlib import asynccontextmanager
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
from .models import Customer as CustomerRow
from .schemas import CUSTOMER_ID_PATTERN, Customer, CustomerCreate, CustomerPage, CustomerStatus, CustomerTier

log = get_logger(__name__)

PROBLEM_RESPONSES = problem_responses(
    r404="Customer not found",
    r409="Conflict (e.g. email already registered)",
    r422="Validation error",
    r503="Data store unavailable",
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.service_name)
    engine = db.build_engine(settings.database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Retry so the container survives the DB starting a few seconds late.
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
        title="ComCo Customer API",
        version="1.0.0",
        summary="Customer master data for the ComCo Customer Service Integration Platform.",
        description=(
            "Owned by the **Customer Service**, backed by its own PostgreSQL database.\n\n"
            "External consumers reach this API **only through the Kong API gateway** at "
            "`/api/v1/customers` using an `apikey` header. All errors use "
            "RFC 7807 Problem Details (`application/problem+json`)."
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
    CustomerId = Annotated[
        str, Path(pattern=CUSTOMER_ID_PATTERN, description="Customer identifier", examples=["C001"])
    ]

    # ---------------------------------------------------------------- health
    @app.get("/health", tags=["ops"], summary="Liveness probe", include_in_schema=False)
    async def health() -> dict:
        return {"status": "ok", "service": settings.service_name}

    @app.get("/ready", tags=["ops"], summary="Readiness probe (checks the database)", include_in_schema=False)
    async def ready(session: SessionDep) -> dict:
        await db.ping(session)
        return {"status": "ready", "service": settings.service_name}

    # ---------------------------------------------------------------- customers
    @app.get(
        "/customers",
        response_model=CustomerPage,
        tags=["customers"],
        summary="List customers",
        responses={422: PROBLEM_RESPONSES[422], 503: PROBLEM_RESPONSES[503]},
    )
    async def list_customers(
        session: SessionDep,
        tier: CustomerTier | None = None,
        status_: Annotated[CustomerStatus | None, Query(alias="status")] = None,
        email: str | None = Query(default=None, description="Exact email match"),
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> CustomerPage:
        query = select(CustomerRow)
        if tier:
            query = query.where(CustomerRow.tier == tier.value)
        if status_:
            query = query.where(CustomerRow.status == status_.value)
        if email:
            query = query.where(func.lower(CustomerRow.email) == email.lower())
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        rows = (
            await session.scalars(query.order_by(CustomerRow.customer_id).limit(limit).offset(offset))
        ).all()
        return CustomerPage(
            items=[Customer.model_validate(r) for r in rows], total=total or 0, limit=limit, offset=offset
        )

    @app.get(
        "/customers/{customer_id}",
        response_model=Customer,
        tags=["customers"],
        summary="Get a customer by ID",
        responses={404: PROBLEM_RESPONSES[404], 422: PROBLEM_RESPONSES[422], 503: PROBLEM_RESPONSES[503]},
    )
    async def get_customer(customer_id: CustomerId, session: SessionDep) -> Customer:
        row = await session.get(CustomerRow, customer_id)
        if row is None:
            raise ProblemException(
                404, f"Customer {customer_id} does not exist.", code="CUSTOMER_NOT_FOUND"
            )
        return Customer.model_validate(row)

    @app.post(
        "/customers",
        response_model=Customer,
        status_code=status.HTTP_201_CREATED,
        tags=["customers"],
        summary="Register a new customer",
        responses={409: PROBLEM_RESPONSES[409], 422: PROBLEM_RESPONSES[422], 503: PROBLEM_RESPONSES[503]},
    )
    async def create_customer(
        payload: CustomerCreate, session: SessionDep, response: Response
    ) -> Customer:
        duplicate = await session.scalar(
            select(CustomerRow.customer_id).where(func.lower(CustomerRow.email) == payload.email.lower())
        )
        if duplicate:
            raise ProblemException(
                409,
                f"Email {payload.email} is already registered.",
                code="EMAIL_ALREADY_REGISTERED",
            )
        row = CustomerRow(
            customer_id=await _next_customer_id(session),
            first_name=payload.first_name,
            last_name=payload.last_name,
            email=payload.email.lower(),
            phone=payload.phone,
            tier=payload.tier.value,
            status=CustomerStatus.ACTIVE.value,
        )
        session.add(row)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise ProblemException(409, "Customer could not be created due to a conflict.", code="CONFLICT")
        await session.refresh(row)
        response.headers["Location"] = f"{settings.public_base_url}/customers/{row.customer_id}"
        log.info("customer_created", customer_id=row.customer_id)
        return Customer.model_validate(row)

    install_openapi_extras(app)
    return app


async def _next_customer_id(session: AsyncSession) -> str:
    # Prototype ID generation (see README limitations - a DB sequence would be used in production).
    ids = (await session.scalars(select(CustomerRow.customer_id))).all()
    numbers = [int(i[1:]) for i in ids if i[1:].isdigit()]
    return f"C{(max(numbers, default=0) + 1):03d}"


app = create_app()
