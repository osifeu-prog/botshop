import os
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any

import asyncpg

from .logging import logger


class DatabaseManager:
    _pool: Optional[asyncpg.Pool] = None

    @classmethod
    async def get_pool(cls) -> asyncpg.Pool:
        if cls._pool is None:
            dsn = os.getenv("DATABASE_URL")
            if not dsn:
                raise RuntimeError("DATABASE_URL not configured")
            logger.info("Creating asyncpg pool", dsn=dsn)
            cls._pool = await asyncpg.create_pool(
                dsn,
                min_size=1,
                max_size=10,
                command_timeout=60,
            )
        return cls._pool

    @classmethod
    async def close(cls):
        if cls._pool is not None:
            await cls._pool.close()
            cls._pool = None


@asynccontextmanager
async def db_conn():
    pool = await DatabaseManager.get_pool()
    async with pool.acquire() as conn:
        yield conn


async def get_approval_stats() -> Dict[str, Any]:
    """Fetch basic finance/approval stats.

    This is intentionally defensive: if the table doesn't exist yet,
    we return zeros so the API continues to work.
    Expected schema (you can adapt on your DB):

        payment_approvals(
            id serial primary key,
            user_id bigint,
            amount numeric,
            status text check (status in ('pending','approved','rejected'))
        )
    """
    try:
        async with db_conn() as conn:
            row = await conn.fetchrow(
                '''
                SELECT
                    COUNT(*) FILTER (WHERE status = 'pending')  AS pending,
                    COUNT(*) FILTER (WHERE status = 'approved') AS approved,
                    COUNT(*) FILTER (WHERE status = 'rejected') AS rejected,
                    COALESCE(SUM(amount), 0)                    AS total_amount
                FROM payment_approvals;
                '''
            )
            data = dict(row)
    except Exception as e:
        logger.warning("DB metrics unavailable: %s", e)
        data = {
            "pending": 0,
            "approved": 0,
            "rejected": 0,
            "total_amount": 0,
        }

    total = data["pending"] + data["approved"] + data["rejected"]
    return {
        "approvals": {
            "pending": data["pending"],
            "approved": data["approved"],
            "rejected": data["rejected"],
            "total": total,
        },
        "reserve": {
            "total_amount": float(data["total_amount"]),
            "total_reserve": 0.0,
            "total_net": 0.0,
            "total_payments": total,
            "approved_count": data["approved"],
            "pending_count": data["pending"],
            "rejected_count": data["rejected"],
        },
    }
