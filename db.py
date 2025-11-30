# db.py
import os
import logging
from contextlib import contextmanager
from typing import Optional, Any, List, Dict

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    logger.warning("DATABASE_URL is not set. DB functions will be no-op.")


def get_conn():
    """מחזיר חיבור ל-Postgres או None אם אין DATABASE_URL"""
    if not DATABASE_URL:
        return None
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.DictCursor)
    return conn


@contextmanager
def db_cursor():
    conn = get_conn()
    if conn is None:
        yield None, None
        return
    try:
        cur = conn.cursor()
        yield conn, cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def init_schema() -> None:
    """
    מריץ CREATE TABLE IF NOT EXISTS לכל הטבלאות.
    לא מוחק ולא שובר כלום, רק מוסיף אם חסר.
    """
    if not DATABASE_URL:
        logger.warning("init_schema called but DATABASE_URL not set.")
        return

    with db_cursor() as (conn, cur):
        if cur is None:
            logger.warning("No DB cursor available in init_schema.")
            return

        # payments – כבר קיימת אצלך, כאן רק לוודא
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                username TEXT,
                pay_method TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                reason TEXT,
                amount NUMERIC(12,2),
                reserve_ratio NUMERIC(5,4),
                reserve_amount NUMERIC(12,2),
                net_amount NUMERIC(12,2),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        # עדכון טבלה קיימת עם עמודות רזרבה, למקרה שהיא כבר קיימת בלי השדות החדשים
        cur.execute(
            """
            ALTER TABLE payments
                ADD COLUMN IF NOT EXISTS amount NUMERIC(12,2),
                ADD COLUMN IF NOT EXISTS reserve_ratio NUMERIC(5,4),
                ADD COLUMN IF NOT EXISTS reserve_amount NUMERIC(12,2),
                ADD COLUMN IF NOT EXISTS net_amount NUMERIC(12,2);
            """
        )

        # users – רשימת משתמשים
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id BIGINT PRIMARY KEY,      -- Telegram user id
                username TEXT,
                first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        # referrals – הפניות
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
                id SERIAL PRIMARY KEY,
                referrer_id BIGINT NOT NULL,
                referred_id BIGINT NOT NULL,
                source TEXT,
                points INT NOT NULL DEFAULT 1,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        # rewards – פרסים/נקודות (SLH, NFT, SHARE וכו')
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS rewards (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                reward_type TEXT NOT NULL,      -- "SLH", "NFT", "SHARE", ...
                reason TEXT,
                points INT NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',   -- pending/sent/failed
                tx_hash TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        # metrics – מונים גלובליים (למשל start_image_views)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS metrics (
                key TEXT PRIMARY KEY,
                value BIGINT NOT NULL DEFAULT 0
            );
            """
        )

        logger.info("DB schema ensured (payments, users, referrals, rewards, metrics).")


# =========================
# payments
# =========================

def log_payment(user_id: int, username: Optional[str], pay_method: str) -> None:
    """
    רושם תשלום במצב 'pending' (כשהמשתמש שולח צילום אישור).
    """
    with db_cursor() as (conn, cur):
        if cur is None:
            logger.warning("log_payment called without DB.")
            return
        cur.execute(
            """
            INSERT INTO payments (
                user_id,
                username,
                pay_method,
                status,
                amount,
                reserve_ratio,
                reserve_amount,
                net_amount,
                created_at,
                updated_at
            )
            VALUES (
                %s,
                %s,
                %s,
                'pending',
                39.00,
                0.49,
                39.00 * 0.49,
                39.00 - (39.00 * 0.49),
                NOW(),
                NOW()
            );
            """,
            (user_id, username, pay_method),
        )


def update_payment_status(user_id: int, status: str, reason: Optional[str]) -> None:
    """
    מעדכן את הסטטוס של התשלום האחרון של משתמש מסוים.
    status: 'approved' / 'rejected' / 'pending'
    """
    with db_cursor() as (conn, cur):
        if cur is None:
            logger.warning("update_payment_status called without DB.")
            return
        cur.execute(
            """
            UPDATE payments
            SET status = %s,
                reason = %s,
                updated_at = NOW()
            WHERE id = (
                SELECT id
                FROM payments
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT 1
            );
            """,
            (status, reason, user_id),
        )


# =========================
# users / referrals – למערכת ניקוד ו-Leaderboard
# =========================

def store_user(user_id: int, username: Optional[str]) -> None:
    """
    שומר/מעדכן משתמש בטבלת users.
    אם קיים – מעדכן username.
    """
    with db_cursor() as (conn, cur):
        if cur is None:
            return
        cur.execute(
            """
            INSERT INTO users (id, username, first_seen_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (id) DO UPDATE
              SET username = EXCLUDED.username;
            """,
            (user_id, username),
        )


def add_referral(referrer_id: int, referred_id: int, source: str) -> None:
    """
    מוסיף רשומת הפנייה.
    """
    with db_cursor() as (conn, cur):
        if cur is None:
            return
        cur.execute(
            """
            INSERT INTO referrals (referrer_id, referred_id, source, points)
            VALUES (%s, %s, %s, 1)
            ON CONFLICT DO NOTHING;
            """,
            (referrer_id, referred_id, source),
        )


def get_top_referrers(limit: int = 10) -> List[Dict[str, Any]]:
    """
    מחזיר את המפנים הטופ לפי סך נקודות / מספר הפניות.
    """
    with db_cursor() as (conn, cur):
        if cur is None:
            return []
        cur.execute(
            """
            SELECT r.referrer_id,
                   u.username,
                   COUNT(*) AS total_referrals,
                   SUM(r.points) AS total_points
            FROM referrals r
            LEFT JOIN users u ON u.id = r.referrer_id
            GROUP BY r.referrer_id, u.username
            ORDER BY total_points DESC, total_referrals DESC
            LIMIT %s;
            """,
            (limit,),
        )
        rows = cur.fetchall()
        return [dict(row) for row in rows]


# =========================
# דוחות על תשלומים
# =========================

def get_monthly_payments(year: int, month: int) -> List[Dict[str, Any]]:
    """
    מחזיר פילוח לפי שיטת תשלום וסטטוס לחודש נתון.
    """
    with db_cursor() as (conn, cur):
        if cur is None:
            return []
        cur.execute(
            """
            SELECT pay_method,
                   status,
                   COUNT(*) AS count
            FROM payments
            WHERE EXTRACT(YEAR FROM created_at) = %s
              AND EXTRACT(MONTH FROM created_at) = %s
            GROUP BY pay_method, status
            ORDER BY pay_method, status;
            """,
            (year, month),
        )
        rows = cur.fetchall()
        return [dict(row) for row in rows]

def get_reserve_stats() -> Optional[Dict[str, Any]]:
    """מחזיר סטטיסטיקה כספית על תשלומים ורזרבות (49%)."""
    with db_cursor() as (conn, cur):
        if cur is None:
            return None
        cur.execute(
            """
            SELECT
                COALESCE(SUM(amount), 0)           AS total_amount,
                COALESCE(SUM(reserve_amount), 0)   AS total_reserve,
                COALESCE(SUM(net_amount), 0)       AS total_net,
                COUNT(*)                           AS total_payments,
                COUNT(*) FILTER (WHERE status = 'approved') AS approved_count,
                COUNT(*) FILTER (WHERE status = 'pending')  AS pending_count,
                COUNT(*) FILTER (WHERE status = 'rejected') AS rejected_count
            FROM payments;
            """
        )
        row = cur.fetchone()
        if not row:
            return None
        return dict(row)




def get_approval_stats() -> Optional[Dict[str, Any]]:
    """
    מחזיר סטטיסטיקה כללית על statuses מהמכלול.
    """
    with db_cursor() as (conn, cur):
        if cur is None:
            return None
        cur.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE status = 'pending') AS pending,
              COUNT(*) FILTER (WHERE status = 'approved') AS approved,
              COUNT(*) FILTER (WHERE status = 'rejected') AS rejected,
              COUNT(*) AS total
            FROM payments;
            """
        )
        row = cur.fetchone()
        if not row:
            return None
        return dict(row)


# =========================
# rewards – בסיס ל-NFT / SLH / SHARE
# =========================

def create_reward(user_id: int, reward_type: str, reason: str, points: int = 0) -> None:
    """
    יוצר רשומת Reward במצב 'pending'.
    """
    with db_cursor() as (conn, cur):
        if cur is None:
            return
        cur.execute(
            """
            INSERT INTO rewards (user_id, reward_type, reason, points, status, created_at, updated_at)
            VALUES (%s, %s, %s, %s, 'pending', NOW(), NOW());
            """,
            (user_id, reward_type, reason, points),
        )


def get_user_total_points(user_id: int, reward_type: Optional[str] = None) -> int:
    """
    מחזיר סך נקודות למשתמש מתוך rewards.
    אם reward_type לא None – מסנן לפי סוג (למשל 'SHARE').
    """
    with db_cursor() as (conn, cur):
        if cur is None:
            return 0

        if reward_type:
            cur.execute(
                """
                SELECT COALESCE(SUM(points), 0) AS total_points
                FROM rewards
                WHERE user_id = %s
                  AND reward_type = %s;
                """,
                (user_id, reward_type),
            )
        else:
            cur.execute(
                """
                SELECT COALESCE(SUM(points), 0) AS total_points
                FROM rewards
                WHERE user_id = %s;
                """,
                (user_id,),
            )

        row = cur.fetchone()
        return int(row["total_points"]) if row else 0


# =========================
# metrics – מונים גלובליים
# =========================

def increment_metric(key: str, amount: int = 1) -> int:
    """
    מעלה מונה גלובלי ומחזיר את הערך החדש.
    """
    with db_cursor() as (conn, cur):
        if cur is None:
            return 0
        cur.execute(
            """
            INSERT INTO metrics (key, value)
            VALUES (%s, %s)
            ON CONFLICT (key)
            DO UPDATE SET value = metrics.value + EXCLUDED.value
            RETURNING value;
            """,
            (key, amount),
        )
        row = cur.fetchone()
        return int(row["value"]) if row else 0


def get_metric(key: str) -> int:
    """
    מחזיר את ערך המונה או 0 אם לא קיים.
    """
    with db_cursor() as (conn, cur):
        if cur is None:
            return 0
        cur.execute(
            "SELECT value FROM metrics WHERE key = %s;",
            (key,),
        )
        row = cur.fetchone()
        return int(row["value"]) if row else 0

def get_users_stats() -> Dict[str, int]:
    """Aggregate basic user/referral stats for admin dashboard."""
    with db_cursor() as (conn, cur):
        if cur is None:
            return {
                "total_users": 0,
                "total_referrals": 0,
                "total_referred_users": 0,
                "total_referrers": 0,
            }

        # Total registered users
        cur.execute("SELECT COUNT(*) FROM users;")
        total_users = cur.fetchone()[0] or 0

        # Total referral rows
        cur.execute("SELECT COUNT(*) FROM referrals;")
        total_referrals = cur.fetchone()[0] or 0

        # Distinct referred users (joined through any referral link)
        cur.execute("SELECT COUNT(DISTINCT referred_id) FROM referrals;")
        total_referred_users = cur.fetchone()[0] or 0

        # Distinct referrers (users who brought at least one friend)
        cur.execute("SELECT COUNT(DISTINCT referrer_id) FROM referrals;")
        total_referrers = cur.fetchone()[0] or 0

        return {
            "total_users": int(total_users),
            "total_referrals": int(total_referrals),
            "total_referred_users": int(total_referred_users),
            "total_referrers": int(total_referrers),
        }
# === SLHNET EXTENSION: wallets, token_sales, posts ===
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger("db_slhnet_ext")

def _slhnet_get_conn():
    import os
    import psycopg2
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg2.connect(url)

try:
    get_conn  # type: ignore[name-defined]
except NameError:  # pragma: no cover
    get_conn = _slhnet_get_conn  # type: ignore[assignment]


def _init_schema_slhnet():
    conn = get_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS wallets (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    telegram_username TEXT,
                    chain_id INTEGER NOT NULL,
                    address TEXT NOT NULL,
                    is_primary BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (chain_id, address)
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS token_sales (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    wallet_address TEXT NOT NULL,
                    chain_id INTEGER NOT NULL,
                    amount_slh NUMERIC(36, 18) NOT NULL,
                    tx_hash TEXT NOT NULL,
                    tx_status TEXT NOT NULL DEFAULT 'verified',
                    tx_error TEXT,
                    block_number BIGINT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS posts (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    username TEXT,
                    title TEXT,
                    content TEXT,
                    image_url TEXT,
                    link_url TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    status TEXT NOT NULL DEFAULT 'published'
                );
                """
            )
    logger.info("SLHNET extra tables ensured (wallets, token_sales, posts)")


try:
    _orig_init_schema = init_schema  # type: ignore[name-defined]
    def init_schema():  # type: ignore[no-redef]
        _orig_init_schema()
        _init_schema_slhnet()
except NameError:
    def init_schema():  # type: ignore[no-redef]
        _init_schema_slhnet()


def add_wallet(
    user_id: int,
    username: Optional[str],
    chain_id: int,
    address: str,
    is_primary: bool = True,
) -> None:
    conn = get_conn()
    with conn:
        with conn.cursor() as cur:
            if is_primary:
                cur.execute(
                    "UPDATE wallets SET is_primary = FALSE WHERE user_id = %s AND chain_id = %s",
                    (user_id, chain_id),
                )
            cur.execute(
                """
                INSERT INTO wallets (user_id, telegram_username, chain_id, address, is_primary)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (chain_id, address) DO UPDATE
                SET telegram_username = EXCLUDED.telegram_username
                """,
                (user_id, username, chain_id, address, is_primary),
            )
    logger.info("Wallet added/updated: user_id=%s chain_id=%s address=%s", user_id, chain_id, address)


def get_user_wallets(user_id: int, chain_id: Optional[int] = None) -> List[Dict[str, Any]]:
    conn = get_conn()
    with conn:
        with conn.cursor() as cur:
            if chain_id is None:
                cur.execute(
                    """
                    SELECT id, chain_id, address, is_primary, created_at
                    FROM wallets
                    WHERE user_id = %s
                    ORDER BY is_primary DESC, created_at DESC
                    """,
                    (user_id,),
                )
            else:
                cur.execute(
                    """
                    SELECT id, chain_id, address, is_primary, created_at
                    FROM wallets
                    WHERE user_id = %s AND chain_id = %s
                    ORDER BY is_primary DESC, created_at DESC
                    """,
                    (user_id, chain_id),
                )
            rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "chain_id": r[1],
            "address": r[2],
            "is_primary": r[3],
            "created_at": r[4],
        }
        for r in rows
    ]


def get_primary_wallet(user_id: int, chain_id: int) -> Optional[Dict[str, Any]]:
    ws = get_user_wallets(user_id, chain_id=chain_id)
    for w in ws:
        if w["is_primary"]:
            return w
    return ws[0] if ws else None


def create_token_sale(
    user_id: int,
    wallet_address: str,
    chain_id: int,
    amount_slh: float,
    tx_hash: str,
    status: str,
    error: Optional[str],
    block_number: Optional[int],
) -> int:
    conn = get_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO token_sales (
                    user_id, wallet_address, chain_id, amount_slh, tx_hash, tx_status, tx_error, block_number
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (user_id, wallet_address, chain_id, amount_slh, tx_hash, status, error, block_number),
            )
            sale_id = cur.fetchone()[0]
    return sale_id


def list_token_sales(limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    conn = get_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, user_id, wallet_address, chain_id, amount_slh,
                       tx_hash, tx_status, tx_error, block_number, created_at
                FROM token_sales
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "user_id": r[1],
            "wallet_address": r[2],
            "chain_id": r[3],
            "amount_slh": float(r[4]),
            "tx_hash": r[5],
            "tx_status": r[6],
            "tx_error": r[7],
            "block_number": r[8],
            "created_at": r[9],
        }
        for r in rows
    ]


def get_user_token_sales(user_id: int) -> List[Dict[str, Any]]:
    conn = get_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, wallet_address, chain_id, amount_slh,
                       tx_hash, tx_status, tx_error, block_number, created_at
                FROM token_sales
                WHERE user_id = %s
                ORDER BY created_at DESC
                """,
                (user_id,),
            )
            rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "wallet_address": r[1],
            "chain_id": r[2],
            "amount_slh": float(r[3]),
            "tx_hash": r[4],
            "tx_status": r[5],
            "tx_error": r[6],
            "block_number": r[7],
            "created_at": r[8],
        }
        for r in rows
    ]


def create_post(
    user_id: int,
    username: Optional[str],
    title: str,
    content: str,
    image_url: Optional[str] = None,
    link_url: Optional[str] = None,
) -> int:
    conn = get_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO posts (user_id, username, title, content, image_url, link_url)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (user_id, username, title, content, image_url, link_url),
            )
            pid = cur.fetchone()[0]
    return pid


def list_recent_posts(limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
    conn = get_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, user_id, username, title, content, image_url, link_url, created_at, status
                FROM posts
                WHERE status = 'published'
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            rows = cur.fetchall()
    return [
        {
            "id": r[0],
            "user_id": r[1],
            "username": r[2],
            "title": r[3],
            "content": r[4],
            "image_url": r[5],
            "link_url": r[6],
            "created_at": r[7],
            "status": r[8],
        }
        for r in rows
    ]
try:
    _init_schema_slhnet()
except Exception as e:
    try:
        logger.error("SLHNET: failed to ensure extra tables: %s", e)
    except Exception:
        pass

# ================================
# SLHNET extra tables & helpers
from typing import List, Dict, Any, Optional

def ensure_extra_tables(conn):
    """Create SLHNET extra tables if they don't exist"""
    with conn.cursor() as cur:
        # פוסטים מהרשת החברתית
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS slh_posts (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                username TEXT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                share_url TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                is_published BOOLEAN DEFAULT TRUE
            );
            """
        )
        # מכירות SLH שתועדו דרך המערכת
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS slh_token_sales (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                username TEXT,
                wallet_address TEXT,
                amount_slh NUMERIC(36, 18),
                price_nis NUMERIC(18, 2),
                status TEXT DEFAULT 'pending',
                tx_hash TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """
        )
    conn.commit()


def fetch_posts(limit: int = 20) -> List[Dict[str, Any]]:
    """Get recent published posts for SLHNET Social"""
    conn = get_conn()
    ensure_extra_tables(conn)
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, user_id, username, title, content, share_url,
                       created_at, is_published
                FROM slh_posts
                WHERE is_published = TRUE
                ORDER BY created_at DESC
                LIMIT %s;
                """,
                (limit,)
            )
            rows = cur.fetchall()
    posts: List[Dict[str, Any]] = []
    for r in rows:
        posts.append(
            {
                "id": r[0],
                "user_id": r[1],
                "username": r[2],
                "title": r[3],
                "content": r[4],
                "share_url": r[5],
                "created_at": r[6].isoformat() if r[6] else None,
                "is_published": bool(r[7]),
            }
        )
    return posts


def add_post(user_id: int, username: str, title: str, content: str,
             share_url: Optional[str] = None) -> int:
    """Insert a new social post and return its id"""
    conn = get_conn()
    ensure_extra_tables(conn)
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO slh_posts (user_id, username, title, content, share_url)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id;
                """,
                (user_id, username, title, content, share_url),
            )
            post_id = cur.fetchone()[0]
    return post_id


def fetch_token_sales(limit: int = 50) -> List[Dict[str, Any]]:
    """Get recent SLH token sales for the public board"""
    conn = get_conn()
    ensure_extra_tables(conn)
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, user_id, username, wallet_address,
                       amount_slh, price_nis, status, tx_hash, created_at
                FROM slh_token_sales
                ORDER BY created_at DESC
                LIMIT %s;
                """,
                (limit,),
            )
            rows = cur.fetchall()
    sales: List[Dict[str, Any]] = []
    for r in rows:
        sales.append(
            {
                "id": r[0],
                "user_id": r[1],
                "username": r[2],
                "wallet_address": r[3],
                "amount_slh": float(r[4]) if r[4] is not None else None,
                "price_nis": float(r[5]) if r[5] is not None else None,
                "status": r[6],
                "tx_hash": r[7],
                "created_at": r[8].isoformat() if r[8] else None,
            }
        )
    return sales

# =========================
# payment helpers
# =========================

def has_approved_payment(user_id: int) -> bool:
    """בודק אם למשתמש יש תשלום מאושר כלשהו."""
    with db_cursor() as (conn, cur):
        if cur is None:
            return False
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM payments
                WHERE user_id = %s
                  AND status = 'approved'
            )
            """,
            (user_id,),
        )
        row = cur.fetchone()
        return bool(row[0]) if row else False


def get_pending_payments(limit: int = 20) -> List[Dict[str, Any]]:
    """מחזיר רשימת תשלומים במצב 'pending' לתצוגה באדמין."""
    with db_cursor() as (conn, cur):
        if cur is None:
            return []
        cur.execute(
            """
            SELECT id, user_id, username, pay_method, status, created_at
            FROM payments
            WHERE status = 'pending'
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        rows = cur.fetchall()
        return [
            {
                "id": r["id"],
                "user_id": r["user_id"],
                "username": r["username"],
                "pay_method": r["pay_method"],
                "status": r["status"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
