# core/slh_onchain.py

import os
import logging
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict, Any

import asyncio

import httpx  # תלוי כבר ב-python-telegram-bot, אבל טוב לדעת שהוא זמין

logger = logging.getLogger("slhnet.onchain")


class OnchainConfig:
    """
    קונפיגורציית On-chain דינמית, מבוססת משתני סביבה.

    BSC:
      - BSC_RPC_URL         – כתובת RPC (למשל https://bsc-dataseed.binance.org/)
      - BSC_CHAIN_ID        – ברירת מחדל: 56
      - BSC_DECIMALS        – ברירת מחדל: 18

    TON:
      - TONCENTER_API_URL   – למשל https://toncenter.com/api/v2
      - TONCENTER_API_KEY   – מפתח API אם צריך (אופציונלי)

    הערה: המודול הזה כרגע קורא *יתרה בלבד* (read-only).
    בעתיד אפשר להרחיב אותו למשיכות / הפקדות אמיתיות.
    """

    BSC_RPC_URL: str = os.getenv("BSC_RPC_URL", "").strip()
    BSC_CHAIN_ID: int = int(os.getenv("BSC_CHAIN_ID", "56"))
    BSC_DECIMALS: int = int(os.getenv("BSC_DECIMALS", "18"))

    TONCENTER_API_URL: str = os.getenv("TONCENTER_API_URL", "").rstrip("/")
    TONCENTER_API_KEY: str = os.getenv("TONCENTER_API_KEY", "").strip()

    @classmethod
    def has_bsc(cls) -> bool:
        return bool(cls.BSC_RPC_URL)

    @classmethod
    def has_ton(cls) -> bool:
        return bool(cls.TONCENTER_API_URL)


# ============================================================
# Utilities
# ============================================================

def _wei_to_decimal(value_wei: int, decimals: int = 18) -> Decimal:
    """
    המרת Wei או נייטיבים אחרים ל-Decimal.
    """
    if value_wei <= 0:
        return Decimal("0")
    try:
        factor = Decimal(10) ** Decimal(decimals)
        return (Decimal(value_wei) / factor).quantize(Decimal("0.00000001"))
    except Exception as e:
        logger.error(f"Error converting wei to decimal: {e}")
        return Decimal("0")


# ============================================================
# BSC – Native balance (BNB) באמצעות JSON-RPC
# ============================================================

async def fetch_bsc_native_balance(address: str) -> Decimal:
    """
    קריאת יתרת BNB אמיתית עבור כתובת BSC, באמצעות קריאת RPC eth_getBalance.

    מחזיר Decimal של כמות BNB.
    במקרה של שגיאה / קונפיגורציה חסרה – מחזיר 0 ולא מפיל את השרת.
    """
    address = (address or "").strip()
    if not address:
        logger.warning("fetch_bsc_native_balance called with empty address")
        return Decimal("0")

    if not OnchainConfig.has_bsc():
        logger.warning("BSC_RPC_URL not configured – returning 0 balance")
        return Decimal("0")

    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getBalance",
        "params": [address, "latest"],
        "id": 1,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(OnchainConfig.BSC_RPC_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.error(f"BSC RPC request failed: {e}")
        return Decimal("0")

    try:
        result = data.get("result")
        if not result:
            logger.warning(f"BSC RPC response missing 'result': {data}")
            return Decimal("0")

        # result הוא hex string, לדוגמה "0x1234..."
        value_wei = int(result, 16)
        return _wei_to_decimal(value_wei, OnchainConfig.BSC_DECIMALS)
    except Exception as e:
        logger.error(f"Failed to parse BSC balance response: {e}")
        return Decimal("0")


# ============================================================
# TON – Native balance (TON) דרך Toncenter API
# ============================================================

async def fetch_ton_native_balance(address: str) -> Decimal:
    """
    קריאת יתרת TON אמיתית עבור כתובת TON.

    ברירת מחדל: שימוש ב-Toncenter API:
      GET {TONCENTER_API_URL}/getAddressBalance?address=...&api_key=...

    Toncenter מחזיר יתרה ב-nanotons (1 TON = 1e9 nano).
    אנחנו נמיר ל-TON כ-Decimal.

    במקרה של שגיאה / קונפיגורציה חסרה – מחזיר 0 ולא מפיל את השרת.
    """
    address = (address or "").strip()
    if not address:
        logger.warning("fetch_ton_native_balance called with empty address")
        return Decimal("0")

    if not OnchainConfig.has_ton():
        logger.warning("TONCENTER_API_URL not configured – returning 0 balance")
        return Decimal("0")

    params = {"address": address}
    if OnchainConfig.TONCENTER_API_KEY:
        params["api_key"] = OnchainConfig.TONCENTER_API_KEY

    url = f"{OnchainConfig.TONCENTER_API_URL}/getAddressBalance"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.error(f"TON request failed: {e}")
        return Decimal("0")

    try:
        # חלק מהגרסאות מחזירות {"ok": true, "result": "<int_as_string>"}
        if "result" not in data:
            logger.warning(f"TON API response missing 'result': {data}")
            return Decimal("0")

        raw = str(data["result"])
        # nanotons -> TON
        value_nano = int(raw)
        if value_nano <= 0:
            return Decimal("0")

        factor = Decimal(10) ** Decimal(9)  # 1 TON = 1e9 nano
        return (Decimal(value_nano) / factor).quantize(Decimal("0.00000001"))
    except Exception as e:
        logger.error(f"Failed to parse TON balance response: {e}")
        return Decimal("0")


# ============================================================
# High-level helpers – Overview לכל משתמש / כתובות
# ============================================================

async def get_onchain_overview(
    bsc_address: Optional[str],
    ton_address: Optional[str],
) -> Dict[str, Any]:
    """
    מחזיר אובייקט מידע מאוחד על יתרות On-chain לממשק פנימי:

    {
      "bsc": {
        "address": "<str|None>",
        "balance": Decimal,  # BNB
      },
      "ton": {
        "address": "<str|None>",
        "balance": Decimal,  # TON
      }
    }

    הערות:
      - אם אין כתובת / אין קונפיגורציה – balance=0.
      - הפונקציה לא מפילה חריגות – במקרי שגיאה מחזירה 0 ורושמת לוג.
    """
    bsc_addr = (bsc_address or "").strip() or None
    ton_addr = (ton_address or "").strip() or None

    bsc_balance = Decimal("0")
    ton_balance = Decimal("0")

    # נריץ את שתי הבקשות במקביל (אם רלוונטי)
    tasks = []

    if bsc_addr:
        tasks.append(fetch_bsc_native_balance(bsc_addr))
    else:
        tasks.append(asyncio.sleep(0, result=Decimal("0")))

    if ton_addr:
        tasks.append(fetch_ton_native_balance(ton_addr))
    else:
        tasks.append(asyncio.sleep(0, result=Decimal("0")))

    try:
        bsc_balance, ton_balance = await asyncio.gather(*tasks)
    except Exception as e:
        logger.error(f"Error in get_onchain_overview gather: {e}")

    return {
        "bsc": {
            "address": bsc_addr,
            "balance": bsc_balance,
        },
        "ton": {
            "address": ton_addr,
            "balance": ton_balance,
        },
    }


# ============================================================
# Sync wrappers – לשימוש עתידי ממקומות לא async (אם תרצה)
# ============================================================

def get_onchain_overview_sync(
    bsc_address: Optional[str],
    ton_address: Optional[str],
) -> Dict[str, Any]:
    """
    עטיפה סינכרונית סביב get_onchain_overview – למקומות שאינם async.
    נפוץ יותר להשתמש בגרסה האסינכרונית בתוך FastAPI / handlers של טלגרם.
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # במידה וקוראים מהקשר async שכבר רץ – זו רק רשת ביטחון.
        # עדיף במקרים כאלה להשתמש ישירות ב-get_onchain_overview.
        logger.warning(
            "get_onchain_overview_sync called inside running event loop – "
            "consider using the async version directly."
        )
        coro = get_onchain_overview(bsc_address, ton_address)
        return loop.run_until_complete(coro)  # type: ignore[func-returns-value]

    return asyncio.run(get_onchain_overview(bsc_address, ton_address))
