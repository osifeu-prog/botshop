# bot/handlers/admin_commands.py

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, Application

from bot.config import Config
from SLH.admin_tools import (
    get_admin_wallet_snapshot,
    get_admin_user_snapshot,
)


def _is_admin(update: Update) -> bool:
    """בודק אם המשתמש הנוכחי נמצא ברשימת האדמינים (ADMIN_OWNER_IDS)."""
    user = update.effective_user
    if not user:
        return False
    try:
        return int(user.id) in Config.ADMIN_OWNER_IDS
    except Exception:
        return False


def _format_decimal(value: Decimal, ndigits: int = 4) -> str:
    q = Decimal(10) ** -ndigits
    return str(value.quantize(q))


async def adminwallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """פקודת /adminwallet – תקציר כספי של המערכת + ארנקים חם/קר."""
    if not _is_admin(update):
        await update.effective_chat.send_message(
            "⛔ הפקודה /adminwallet זמינה רק לאדמינים מורשים."
        )
        return

    snap = get_admin_wallet_snapshot()

    lines = []
    lines.append("🧮 *תמונת מצב מערכתית – ארנקי SLHNET*")
    lines.append("")
    lines.append("💳 *תשלומים מצטברים*")
    lines.append(f" - מספר תשלומים: {snap.payments_count}")
    lines.append(f" - סכום ברוטו (NIS): ~{_format_decimal(snap.total_amount_nis, 2)} ₪")
    lines.append(f" - סכום נטו (NIS): ~{_format_decimal(snap.total_net_nis, 2)} ₪")
    lines.append(
        f" - סכום רזרבה מצטבר (NIS): ~{_format_decimal(snap.total_reserve_nis, 2)} ₪"
    )

    lines.append("")
    lines.append("💠 *SLH במערכת*")
    lines.append(
        f" - סה\"כ SLH שחולקו (mint_entry): ~{_format_decimal(snap.total_distributed_slh)} SLH"
    )
    lines.append(
        f" - סה\"כ SLH בסטייקינג פעיל: ~{_format_decimal(snap.total_staked_slh)} SLH"
    )

    lines.append("")
    lines.append("💎 *פרמטרים פיננסיים*")
    if snap.slh_price_nis is not None:
        lines.append(
            f" - מחיר נוכחי ל־SLH 1: ~{_format_decimal(snap.slh_price_nis, 2)} ₪"
        )
    else:
        lines.append(" - מחיר SLH בש\"ח: לא מוגדר (SLH_NIS_PRICE)")

    if snap.entry_amount_nis is not None:
        lines.append(
            f" - סכום כניסה (NISENTRYAMOUNT): ~{_format_decimal(snap.entry_amount_nis, 2)} ₪"
        )
    else:
        lines.append(" - סכום כניסה: לא מוגדר (NISENTRYAMOUNT)")

    lines.append("")
    lines.append("🏦 *ארנקים מערכתיים*")
    lines.append(
        f" - ארנק חם (HOTWALLETADDRESS): {snap.hot_wallet_address or 'לא מוגדר'}"
    )
    lines.append(
        f" - ארנק קר / כספת קהילה (COLDWALLETADDRESS): {snap.cold_wallet_address or 'לא מוגדר'}"
    )

    text = "\n".join(lines)
    await update.effective_chat.send_message(
        text, parse_mode="Markdown"
    )


async def adminuser_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """פקודת /adminuser <user_id> – תמונת מצב על משתמש בודד."""
    if not _is_admin(update):
        await update.effective_chat.send_message(
            "⛔ הפקודה /adminuser זמינה רק לאדמינים מורשים."
        )
        return

    chat = update.effective_chat
    user = update.effective_user

    if not context.args:
        await chat.send_message("שימוש: /adminuser <user_id>\nלדוגמה: /adminuser 224223270")
        return

    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await chat.send_message("❗ user_id חייב להיות מספרי.\nלדוגמה: /adminuser 224223270")
        return

    snap = get_admin_user_snapshot(target_user_id)
    if snap is None:
        await chat.send_message(
            f"לא נמצאו נתונים למשתמש עם user_id={target_user_id} ב־DB."
        )
        return

    lines = []
    lines.append("🧑‍💼 *תמונת משתמש – SLHNET*")
    lines.append(f"🆔 user_id: `{snap.user_id}`")
    if snap.username:
        lines.append(f"👤 username: @{snap.username}")
    else:
        lines.append("👤 username: לא ידוע")

    lines.append("")
    lines.append("💼 *ארנק פנימי*")
    if snap.wallet_id is not None:
        lines.append(f" - ID ארנק פנימי: {snap.wallet_id}")
        lines.append(f" - יתרה זמינה: ~{_format_decimal(snap.balance_slh)} SLH")
    else:
        lines.append(" - טרם נוצר ארנק פנימי למשתמש זה.")

    lines.append("")
    lines.append("📊 *סטייקינג*")
    lines.append(f" - מספר עמדות סטייקינג פעילות: {snap.active_stakes_count}")
    lines.append(
        f" - סה\"כ SLH נעולים: ~{_format_decimal(snap.active_staked_slh)} SLH"
    )

    lines.append("")
    lines.append("👥 *הפניות*")
    if snap.referrals_count is not None:
        lines.append(f" - מספר הפניות משויך: {snap.referrals_count}")
    else:
        lines.append(" - נתוני הפניות עדיין לא מחוברים לדוח זה.")

    text = "\n".join(lines)
    await chat.send_message(text, parse_mode="Markdown")


def register_admin_commands(app: Application) -> None:
    """
    פונקציה נוחה: לקרוא לה אחרי שיצרת את ה-Application,
    כדי לרשום את שתי הפקודות.
    """
    app.add_handler(CommandHandler("adminwallet", adminwallet_command))
    app.add_handler(CommandHandler("adminuser", adminuser_command))
