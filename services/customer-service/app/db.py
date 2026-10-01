from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from comco_common import get_logger

from .models import Base, Customer
from .seed import SEED_CUSTOMERS

log = get_logger(__name__)


def build_engine(database_url: str) -> AsyncEngine:
    kwargs = {"pool_pre_ping": True}
    if database_url.startswith("postgresql"):
        # Fail fast when the database is down instead of hanging the request.
        kwargs.update(pool_size=5, max_overflow=5, pool_timeout=3, connect_args={"timeout": 3})
    return create_async_engine(database_url, **kwargs)


async def init_db(engine: AsyncEngine, seed: bool) -> None:
    """Create schema and load demo data (prototype shortcut - see README: migrations)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    if not seed:
        return
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        existing = await session.scalar(select(Customer.customer_id).limit(1))
        if existing is None:
            session.add_all(Customer(**row, updated_at=row["created_at"]) for row in SEED_CUSTOMERS)
            await session.commit()
            log.info("seed_loaded", customers=len(SEED_CUSTOMERS))


async def ping(session: AsyncSession) -> None:
    await session.execute(text("SELECT 1"))
