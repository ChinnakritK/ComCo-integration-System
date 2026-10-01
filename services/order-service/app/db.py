from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from comco_common import get_logger

from .models import Base, Order
from .seed import SEED_ORDERS

log = get_logger(__name__)


def build_engine(database_url: str) -> AsyncEngine:
    kwargs = {"pool_pre_ping": True}
    if database_url.startswith("postgresql"):
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
        existing = await session.scalar(select(Order.order_id).limit(1))
        if existing is None:
            session.add_all(
                Order(**{**row, "total_amount": Decimal(row["total_amount"])}) for row in SEED_ORDERS
            )
            await session.commit()
            log.info("seed_loaded", orders=len(SEED_ORDERS))


async def ping(session: AsyncSession) -> None:
    await session.execute(text("SELECT 1"))
