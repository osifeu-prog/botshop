# tools/test_onchain.py
"""
סקריפט דיבוג קטן לבדוק חיבור ל-BSC / TON דרך SLH/slh_onchain.py
בלי לגעת בבוט וב־FastAPI.
להרצה מקומית בלבד (python tools/test_onchain.py).
"""

from __future__ import annotations

import asyncio
import os
from pprint import pprint

from SLH.slh_onchain import (
    get_onchain_balances,
    set_onchain_wallet,
)


async def main() -> None:
    user_id = int(os.getenv("DEBUG_USER_ID", "224223270"))

    print("=== בדיקת כתובות On-chain למשתמש ===")
    rec = set_onchain_wallet(
        user_id=user_id,
        bsc_address=os.getenv("DEBUG_BSC_ADDRESS"),
        ton_address=os.getenv("DEBUG_TON_ADDRESS"),
    )
    pprint(rec)

    print("\n=== בדיקת יתרות On-chain ===")
    balances = await get_onchain_balances(user_id=user_id)
    pprint(balances)

    print("\nסיום test_onchain ✅")


if __name__ == "__main__":
    asyncio.run(main())
