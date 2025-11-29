from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes # <--- ייבוא נוסף לצורך שימוש ב-ContextTypes

from bot.config import Config
from core.logging import logger

# **********************************************
# 1. ייבוא פונקציית ה-DB
# **********************************************
# בהנחה שפונקציית בדיקת ה-DB שלך נמצאת ב-core.db (או מודול אחר שיצרת)
# עליך לשנות את ה-import בהתאם למיקום המדויק של פונקציית בדיקת התשלום שלך:
from core.db import is_user_premium 
# **********************************************


def safe_get_url(primary: Optional[str], fallback: str) -> str:
    if primary and primary.startswith("http"):
        return primary
    return fallback


# **********************************************
# 2. הפיכת הפונקציה לאסינכרונית (ASYNC)
# **********************************************
async def check_user_payment(user_id: Optional[int]) -> bool:
    """Check DB / API if the user has a valid and active payment."""
    if not user_id:
        return False

    logger.info("check_user_payment called", user_id=user_id)
    
    try:
        # **********************************************
        # 3. קריאה אסינכרונית לפונקציית ה-DB
        # **********************************************
        # הקריאה חייבת להכיל "await"
        has_paid = await is_user_premium(user_id) 
        return has_paid
    except Exception as e:
        # טיפול בשגיאות DB
        logger.error(f"DB check failed for user {user_id}: {e}")
        return False


# **********************************************
# 4. עדכון create_main_keyboard לקבלת has_paid כארגומנט
# **********************************************
# הפונקציה create_main_keyboard כבר לא יכולה לקרוא ל-check_user_payment
# בעצמה (כי היא לא אסינכרונית). נגדיר אותה לקבל את has_paid כפרמטר,
# ונקרא לבדיקה האסינכרונית מה-handler שמשתמש בה.

def create_main_keyboard(has_paid: bool) -> InlineKeyboardMarkup:
    """Creates the main keyboard based on the user's payment status."""

    buttons: list[list[InlineKeyboardButton]] = []

    if not has_paid:
        pay_url = safe_get_url(Config.PAYBOX_URL, Config.LANDING_URL + "#join39")
        buttons.append(
            [InlineKeyboardButton("💳 הצטרפות ב‑39 ₪ וגישה מלאה", url=pay_url)]
        )

    buttons.extend(
        [
            [
                InlineKeyboardButton(
                    "ℹ️ לפרטים נוספים על מודל החיסכון",
                    url=safe_get_url(Config.LANDING_URL, "https://slh-nft.com"),
                )
            ],
            [
                InlineKeyboardButton(
                    "👥 הצטרפות לקהילת העסקים",
                    url=safe_get_url(
                        Config.BUSINESS_GROUP_URL or Config.GROUP_STATIC_INVITE,
                        Config.LANDING_URL,
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "📈 מידע למשקיעים ומודל כלכלי", callback_data="open_investor"
                )
            ],
        ]
    )

    if has_paid:
        buttons.append(
            [InlineKeyboardButton("🚀 גישה לתוכן המלא", callback_data="premium_content")]
        )

    return InlineKeyboardMarkup(buttons)
