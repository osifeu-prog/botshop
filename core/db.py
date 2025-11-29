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


# --------------------------------------------------------------------
# * פונקציות חובה ללוגיקת הבוט החדשה *
# --------------------------------------------------------------------

async def is_user_premium(user_id: int) -> bool:
    """
    בדיקה האם למשתמש יש אישור תשלום פעיל (סטטוס 'approved').
    """
    if not user_id:
        return False
    
    try:
        async with db_conn() as conn:
            # בודק האם קיימת רשומה אחת לפחות עבור המשתמש עם סטטוס 'approved'
            result = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM payment_approvals
                    WHERE user_id = $1 AND status = 'approved'
                )
                """,
                user_id
            )
            # 💡 DEBUG: כדי לראות בלוגים שהפונקציה רצה בהצלחה
            logger.debug(f"DB check for user {user_id}: Premium status is {bool(result)}")
            return bool(result)
    except Exception as e:
        logger.error(f"DB check for premium status failed for user {user_id}: {e}")
        return False


async def update_user_payment_status(user_id: int, status: bool) -> bool:
    """
    מעדכן את הסטטוס של בקשת התשלום ה'pending' האחרונה של המשתמש.
    """
    new_status = 'approved' if status else 'rejected'
    
    try:
        async with db_conn() as conn:
            # 1. מציאת ה-ID של בקשת ה-'pending' האחרונה של המשתמש
            approval_id = await conn.fetchval(
                """
                SELECT id FROM payment_approvals
                WHERE user_id = $1 AND status = 'pending'
                ORDER BY id DESC 
                LIMIT 1
                """,
                user_id
            )

            if not approval_id:
                logger.warning(f"DB: No pending payment approval found for user {user_id} to update.")
                return True 
                
            # 2. עדכון הסטטוס של הבקשה שנמצאה
            await conn.execute(
                """
                UPDATE payment_approvals
                SET status = $1
                WHERE id = $2
                """,
                new_status,
                approval_id
            )
            logger.info(f"DB: Successfully set payment ID {approval_id} for user {user_id} to '{new_status}'.")
            return True

    except Exception as e:
        logger.error(f"DB update failed for user {user_id} to {new_status}: {e}")
        return False


# --------------------------------------------------------------------
# * פונקציה קיימת (שלך) *
# --------------------------------------------------------------------

async def get_approval_stats() -> Dict[str, Any]:
    """Fetch basic finance/approval stats."""
    try:
        async with db_conn() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status = 'pending')  AS pending,
                    COUNT(*) FILTER (WHERE status = 'approved') AS approved,
                    COUNT(*) FILTER (WHERE status = 'rejected') AS rejected,
                    COALESCE(SUM(amount), 0)                    AS total_amount
                FROM payment_approvals;
                """
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
