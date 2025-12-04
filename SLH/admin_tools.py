# SLH/admin_tools.py

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict, Any

from db import db_cursor, get_payments_stats
from slh_internal_wallets import get_user_stakes


def _to_decimal(value: Any) -> Decimal:
    """Helper: converst any DB/str/None value to Decimal safely."""
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _env_decimal(*keys: str) -> Optional[Decimal]:
    """Try multiple env var names, return Decimal or None."""
    for key in keys:
        raw = os.getenv(key)
        if not raw:
            continue
        try:
            return Decimal(str(raw))
        except InvalidOperation:
            continue
    return None


@dataclass
class AdminWalletSnapshot:
    payments_count: int
    total_amount_nis: Decimal
    total_net_nis: Decimal
    total_reserve_nis: Decimal

    total_distributed_slh: Decimal
    total_staked_slh: Decimal

    slh_price_nis: Optional[Decimal]
    entry_amount_nis: Optional[Decimal]

    hot_wallet_address: Optional[str]
    cold_wallet_address: Optional[str]


@dataclass
class AdminUserSnapshot:
    user_id: int
    username: Optional[str]

    wallet_id: Optional[int]
    balance_slh: Decimal

    active_stakes_count: int
    active_staked_slh: Decimal

    # לעתיד – כשנרצה להעמיק בהפניות:
    referrals_count: Optional[int] = None


def get_admin_wallet_snapshot() -> AdminWalletSnapshot:
    """
    מחזיר תמונת מצב מערכתית לאדמין:
    - סה״כ תשלומים (count + סכומים בש״ח)
    - סה״כ SLH שמוזרמים (ledger_type='mint_entry')
    - סה״כ SLH נעולים בסטייקינג
    - מחיר SLH בש״ח + סכום כניסה בש״ח
    - כתובות ארנק חם/קר כפי שהוגדרו ב־ENV
    """
    stats: Dict[str, Any] = get_payments_stats() or {}

    payments_count = int(stats.get("payments_count") or 0)
    total_amount_nis = _to_decimal(stats.get("total_amount"))
    total_net_nis = _to_decimal(stats.get("total_net"))
    total_reserve_nis = _to_decimal(stats.get("total_reserve"))

    total_distributed_slh = Decimal("0")
    total_staked_slh = Decimal("0")

    with db_cursor() as cur:
        # סה״כ SLH שחולקו (מינט ראשוני על כניסות)
        cur.execute(
            """
            SELECT COALESCE(SUM(amount_slh), 0)
            FROM internal_wallet_ledger
            WHERE ledger_type = %s
            """,
            ("mint_entry",),
        )
        row = cur.fetchone()
        total_distributed_slh = _to_decimal(row[0] if row else 0)

        # סה״כ SLH בסטייקינג פעיל
        cur.execute(
            """
            SELECT COALESCE(SUM(principal_slh), 0)
            FROM staking_positions
            WHERE status = %s
            """,
            ("active",),
        )
        row = cur.fetchone()
        total_staked_slh = _to_decimal(row[0] if row else 0)

    slh_price_nis = _env_decimal("SLH_NIS_PRICE", "SLH_PRICE_NIS")
    entry_amount_nis = _env_decimal("NISENTRYAMOUNT", "ENTRY_AMOUNT_NIS")

    hot_wallet_address = os.getenv("HOTWALLETADDRESS") or None
    cold_wallet_address = os.getenv("COLDWALLETADDRESS") or None

    return AdminWalletSnapshot(
        payments_count=payments_count,
        total_amount_nis=total_amount_nis,
        total_net_nis=total_net_nis,
        total_reserve_nis=total_reserve_nis,
        total_distributed_slh=total_distributed_slh,
        total_staked_slh=total_staked_slh,
        slh_price_nis=slh_price_nis,
        entry_amount_nis=entry_amount_nis,
        hot_wallet_address=hot_wallet_address,
        cold_wallet_address=cold_wallet_address,
    )


def get_admin_user_snapshot(user_id: int) -> Optional[AdminUserSnapshot]:
    """
    מחזיר תמונת מצב על משתמש ספציפי:
    - ארנק פנימי (id + יתרה)
    - סטייקינג פעיל (כמות + סכום)
    - הפניות (כרגע לא נשלף מה־DB, שמור ל־None)
    """
    user_id = int(user_id)

    wallet_id: Optional[int] = None
    username: Optional[str] = None
    balance_slh: Decimal = Decimal("0")

    with db_cursor() as cur:
        # ארנק פנימי
        cur.execute(
            """
            SELECT id, username, balance_slh
            FROM internal_wallets
            WHERE user_id = %s
            ORDER BY id ASC
            LIMIT 1
            """,
            (user_id,),
        )
        row = cur.fetchone()
        if row:
            wallet_id = int(row[0])
            username = row[1]
            balance_slh = _to_decimal(row[2])

    # סטייקינג פעיל – משתמש בפונקציה קיימת
    stakes = get_user_stakes(user_id)
    active_stakes = [s for s in stakes if s.get("status") == "active"]
    active_stakes_count = len(active_stakes)
    active_staked_slh = sum(
        (_to_decimal(s.get("principal_slh")) for s in active_stakes),
        start=Decimal("0"),
    )

    # לעתיד – כשנחבר טבלת הפניות, נוסיף כאן SELECT ל-referrals
    referrals_count: Optional[int] = None

    if wallet_id is None and active_stakes_count == 0:
        # אין לנו שום נתונים על המשתמש ב־DB – נחזיר None במקום אובייקט ריק
        return None

    return AdminUserSnapshot(
        user_id=user_id,
        username=username,
        wallet_id=wallet_id,
        balance_slh=balance_slh,
        active_stakes_count=active_stakes_count,
        active_staked_slh=active_staked_slh,
        referrals_count=referrals_count,
    )
