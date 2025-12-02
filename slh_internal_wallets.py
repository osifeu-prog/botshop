from decimal import Decimal, InvalidOperation
from typing import Optional, Dict, Any, List, Tuple
import os
from datetime import datetime, timezone

from db import db_cursor
import logging

logger = logging.getLogger("slhnet.internal_wallets")


def _to_decimal(val: Any, default: str = "0") -> Decimal:
    try:
        if isinstance(val, Decimal):
            return val
        if val is None:
            return Decimal(default)
        return Decimal(str(val))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def _get_token_price_nis() -> Decimal:
    """
    מחיר מטבע SLH בש״ח – ניתן לשינוי דרך משתני סביבה.
    ברירת מחדל: 444 ש״ח ל-1 SLH.
    """
    try:
        return Decimal(os.getenv("SLH_TOKEN_PRICE_NIS", "444"))
    except (InvalidOperation, ValueError):
        return Decimal("444")


def _get_entry_price_nis() -> Decimal:
    """
    מחיר כניסה – תשלום בסיס בטלגרם (39 ש״ח כברירת מחדל).
    ניתן לעדכן דרך SLH_ENTRY_PRICE_NIS.
    """
    try:
        return Decimal(os.getenv("SLH_ENTRY_PRICE_NIS", "39"))
    except (InvalidOperation, ValueError):
        return Decimal("39")


def init_internal_wallet_schema() -> None:
    """
    יוצר טבלאות פנימיות לארנקים, ספר תנועות וסטייקינג.
    רץ פעם אחת באתחול (idempotent).
    """
    with db_cursor() as (conn, cur):
        if cur is None:
            logger.warning("init_internal_wallet_schema called without DB.")
            return

        # ארנקים פנימיים (יתרות למשתמשים בתוך המערכת)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS internal_wallets (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL UNIQUE,
                username TEXT,
                balance_slh NUMERIC(36,18) NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        # ספר תנועות ארנק
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS internal_wallet_ledger (
                id SERIAL PRIMARY KEY,
                wallet_id INTEGER NOT NULL REFERENCES internal_wallets(id) ON DELETE CASCADE,
                change_slh NUMERIC(36,18) NOT NULL,
                reason TEXT,
                ref_type TEXT,
                ref_id BIGINT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        # עמדות סטייקינג
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS staking_positions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                wallet_id INTEGER NOT NULL REFERENCES internal_wallets(id),
                amount_slh NUMERIC(36,18) NOT NULL,
                apy NUMERIC(10,2) NOT NULL,
                lock_days INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active', -- active / closed
                started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_reward_at TIMESTAMPTZ,
                closed_at TIMESTAMPTZ,
                total_rewards_slh NUMERIC(36,18) NOT NULL DEFAULT 0
            );
            """
        )

        conn.commit()
        logger.info("Internal wallet & staking schema ensured.")


def ensure_internal_wallet(user_id: int, username: Optional[str]) -> Dict[str, Any]:
    """
    יוצר (אם צריך) ומחזיר את ארנק המשתמש.
    """
    with db_cursor() as (conn, cur):
        if cur is None:
            raise RuntimeError("DB not available")

        cur.execute(
            """
            INSERT INTO internal_wallets (user_id, username)
            VALUES (%s, %s)
            ON CONFLICT (user_id) DO UPDATE
              SET username = EXCLUDED.username,
                  updated_at = NOW()
            RETURNING id, user_id, username, balance_slh, created_at, updated_at;
            """,
            (user_id, username),
        )
        row = cur.fetchone()
        conn.commit()

    return {
        "wallet_id": row[0],
        "user_id": row[1],
        "username": row[2],
        "balance_slh": _to_decimal(row[3]),
        "created_at": row[4],
        "updated_at": row[5],
    }


def get_wallet_overview(user_id: int) -> Optional[Dict[str, Any]]:
    with db_cursor() as (conn, cur):
        if cur is None:
            return None

        cur.execute(
            """
            SELECT id, user_id, username, balance_slh, created_at, updated_at
            FROM internal_wallets
            WHERE user_id = %s;
            """,
            (user_id,),
        )
        row = cur.fetchone()

        if not row:
            return None

        return {
            "wallet_id": row[0],
            "user_id": row[1],
            "username": row[2],
            "balance_slh": _to_decimal(row[3]),
            "created_at": row[4],
            "updated_at": row[5],
        }


def _add_ledger_entry(
    wallet_id: int,
    delta_slh: Decimal,
    reason: str,
    ref_type: Optional[str],
    ref_id: Optional[int],
) -> None:
    with db_cursor() as (conn, cur):
        if cur is None:
            raise RuntimeError("DB not available")

        cur.execute(
            """
            INSERT INTO internal_wallet_ledger (wallet_id, change_slh, reason, ref_type, ref_id)
            VALUES (%s, %s, %s, %s, %s);
            """,
            (wallet_id, str(delta_slh), reason, ref_type, ref_id),
        )
        conn.commit()


def credit_wallet(
    user_id: int,
    username: Optional[str],
    amount_slh: Decimal,
    reason: str,
    ref_type: Optional[str],
    ref_id: Optional[int],
) -> Dict[str, Any]:
    """
    מזכה ארנק של משתמש בכמות SLH נתונה.
    """
    if amount_slh <= 0:
        raise ValueError("amount_slh must be positive")

    with db_cursor() as (conn, cur):
        if cur is None:
            raise RuntimeError("DB not available")

        # ודא ארנק
        cur.execute(
            """
            INSERT INTO internal_wallets (user_id, username)
            VALUES (%s, %s)
            ON CONFLICT (user_id) DO NOTHING;
            """,
            (user_id, username),
        )

        # עדכון יתרה
        cur.execute(
            """
            UPDATE internal_wallets
            SET balance_slh = COALESCE(balance_slh, 0) + %s,
                username = COALESCE(%s, username),
                updated_at = NOW()
            WHERE user_id = %s
            RETURNING id, balance_slh;
            """,
            (str(amount_slh), username, user_id),
        )
        row = cur.fetchone()
        wallet_id = row[0]
        new_balance = _to_decimal(row[1])

        # ספר תנועות
        cur.execute(
            """
            INSERT INTO internal_wallet_ledger (wallet_id, change_slh, reason, ref_type, ref_id)
            VALUES (%s, %s, %s, %s, %s);
            """,
            (wallet_id, str(amount_slh), reason, ref_type, ref_id),
        )

        conn.commit()

    return {
        "wallet_id": wallet_id,
        "balance_slh": new_balance,
        "amount_slh": amount_slh,
    }


def transfer_between_users(from_user_id: int, to_user_id: int, amount_slh: Decimal) -> Tuple[bool, str]:
    """
    מעביר SLH פנימי בין שני משתמשים.
    """
    if amount_slh <= 0:
        return False, "הסכום חייב להיות גדול מאפס."

    with db_cursor() as (conn, cur):
        if cur is None:
            return False, "מסד הנתונים לא זמין כרגע."

        # ודא שני ארנקים
        cur.execute(
            "INSERT INTO internal_wallets (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING;",
            (from_user_id,),
        )
        cur.execute(
            "INSERT INTO internal_wallets (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING;",
            (to_user_id,),
        )

        # בדיקת יתרה
        cur.execute(
            "SELECT id, balance_slh FROM internal_wallets WHERE user_id = %s FOR UPDATE;",
            (from_user_id,),
        )
        row_from = cur.fetchone()
        if not row_from:
            return False, "לא נמצא ארנק שולח."

        from_wallet_id = row_from[0]
        from_balance = _to_decimal(row_from[1])

        if from_balance < amount_slh:
            return False, "אין מספיק יתרה בארנק."

        # נעילת ארנק מקבל
        cur.execute(
            "SELECT id, balance_slh FROM internal_wallets WHERE user_id = %s FOR UPDATE;",
            (to_user_id,),
        )
        row_to = cur.fetchone()
        if not row_to:
            return False, "לא נמצא ארנק מקבל."

        to_wallet_id = row_to[0]
        to_balance = _to_decimal(row_to[1])

        # עדכון יתרות
        new_from = from_balance - amount_slh
        new_to = to_balance + amount_slh

        cur.execute(
            "UPDATE internal_wallets SET balance_slh = %s, updated_at = NOW() WHERE id = %s;",
            (str(new_from), from_wallet_id),
        )
        cur.execute(
            "UPDATE internal_wallets SET balance_slh = %s, updated_at = NOW() WHERE id = %s;",
            (str(new_to), to_wallet_id),
        )

        # ספר תנועות
        cur.execute(
            """
            INSERT INTO internal_wallet_ledger (wallet_id, change_slh, reason, ref_type, ref_id)
            VALUES (%s, %s, %s, %s, %s);
            """,
            (from_wallet_id, str(-amount_slh), f"transfer to user {to_user_id}", "transfer", to_user_id),
        )
        cur.execute(
            """
            INSERT INTO internal_wallet_ledger (wallet_id, change_slh, reason, ref_type, ref_id)
            VALUES (%s, %s, %s, %s, %s);
            """,
            (to_wallet_id, str(amount_slh), f"transfer from user {from_user_id}", "transfer", from_user_id),
        )

        conn.commit()

    return True, "✅ ההעברה הושלמה בהצלחה."


def create_stake_position(user_id: int, amount_slh: Decimal, apy: Decimal, lock_days: int) -> Tuple[bool, str]:
    """
    יוצר עמדת סטייקינג בסיסית: מקפיא סכום מארנק פנימי.
    התגמולים יחושבו בעתיד – כרגע אנחנו רק מתעדים.
    """
    if amount_slh <= 0:
        return False, "הסכום חייב להיות גדול מאפס."
    if apy <= 0:
        return False, "שיעור APY חייב להיות חיובי."

    with db_cursor() as (conn, cur):
        if cur is None:
            return False, "מסד הנתונים לא זמין כרגע."

        # ודא ארנק
        cur.execute(
            "SELECT id, balance_slh FROM internal_wallets WHERE user_id = %s FOR UPDATE;",
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            return False, "אין לך עדיין ארנק פנימי."

        wallet_id = row[0]
        balance = _to_decimal(row[1])

        if balance < amount_slh:
            return False, "אין מספיק יתרה בארנק לצורך סטייקינג."

        new_balance = balance - amount_slh

        # עדכון יתרה
        cur.execute(
            "UPDATE internal_wallets SET balance_slh = %s, updated_at = NOW() WHERE id = %s;",
            (str(new_balance), wallet_id),
        )

        # יצירת עמדת סטייקינג
        cur.execute(
            """
            INSERT INTO staking_positions (
                user_id, wallet_id, amount_slh, apy, lock_days, status, started_at, last_reward_at
            )
            VALUES (%s, %s, %s, %s, %s, 'active', NOW(), NOW())
            RETURNING id;
            """,
            (user_id, wallet_id, str(amount_slh), str(apy), lock_days),
        )
        row_pos = cur.fetchone()
        position_id = row_pos[0]

        # ספר תנועות (חיוב)
        cur.execute(
            """
            INSERT INTO internal_wallet_ledger (wallet_id, change_slh, reason, ref_type, ref_id)
            VALUES (%s, %s, %s, %s, %s);
            """,
            (wallet_id, str(-amount_slh), f"stake position {position_id}", "stake", position_id),
        )

        conn.commit()

    return True, f"✅ נפתחה עבורך עמדת סטייקינג #{position_id} על {amount_slh} SLH."


def get_user_stakes(user_id: int) -> List[Dict[str, Any]]:
    with db_cursor() as (conn, cur):
        if cur is None:
            return []

        cur.execute(
            """
            SELECT id, amount_slh, apy, lock_days, status, started_at, last_reward_at, total_rewards_slh
            FROM staking_positions
            WHERE user_id = %s
            ORDER BY started_at DESC;
            """,
            (user_id,),
        )
        rows = cur.fetchall() or []

    stakes: List[Dict[str, Any]] = []
    for r in rows:
        stakes.append(
            {
                "id": r[0],
                "amount_slh": _to_decimal(r[1]),
                "apy": _to_decimal(r[2]),
                "lock_days": r[3],
                "status": r[4],
                "started_at": r[5],
                "last_reward_at": r[6],
                "total_rewards_slh": _to_decimal(r[7]),
            }
        )
    return stakes


def mint_slh_from_payment(amount_nis: Decimal) -> Decimal:
    """
    מחשב כמה SLH מונפקים על סכום תשלום בש״ח.
    כרגע: SLH = amount_nis / TOKEN_PRICE.
    """
    price = _get_token_price_nis()
    if price <= 0:
        return Decimal("0")
    return (amount_nis / price).quantize(Decimal("0.000000000000000001"))


def credit_wallet_from_payment(
    user_id: int,
    username: Optional[str],
    amount_nis: Decimal,
    ref_type: str = "payment",
    ref_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    מזכה ארנק פנימי על בסיס סכום בש״ח:
    - מחשב כמה SLH מגיעים לפי מחיר המטבע הנוכחי.
    - מזכה את הארנק ומוסיף רשומת לוג.
    """
    slh_amount = mint_slh_from_payment(amount_nis)
    if slh_amount <= 0:
        raise ValueError("לא ניתן להנפיק SLH – מחיר מטבע לא תקין או סכום נמוך מדי.")

    return credit_wallet(
        user_id=user_id,
        username=username,
        amount_slh=slh_amount,
        reason=f"mint from payment {amount_nis} NIS",
        ref_type=ref_type,
        ref_id=ref_id,
    )


def credit_wallet_from_entry_price(
    user_id: int,
    username: Optional[str],
    ref_type: str = "entry_payment",
    ref_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    מזכה ארנק פנימי לפי מחיר הכניסה המוגדר (SLH_ENTRY_PRICE_NIS).
    שימושי למקרה שבו כל כניסה לבוט היא, למשל, 39 ש״ח.
    """
    amount_nis = _get_entry_price_nis()
    return credit_wallet_from_payment(
        user_id=user_id,
        username=username,
        amount_nis=amount_nis,
        ref_type=ref_type,
        ref_id=ref_id,
    )
