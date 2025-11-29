from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import Config
from core.logging import logger
# ייבוא חובה של הפונקציה האסינכרונית
from core.db import is_user_premium 


def safe_get_url(primary: Optional[str], fallback: str) -> str:
    if primary and primary.startswith("http"):
        return primary
    return fallback


async def check_user_payment(user_id: Optional[int]) -> bool:
    """Check DB / API if the user has a valid and active payment (ASYNC)."""
    if not user_id:
        return False

    logger.debug("Starting check_user_payment DB call...")
    
    try:
        # הקריאה חייבת להיות עם await
        has_paid = await is_user_premium(user_id) 
        logger.debug(f"check_user_payment result for {user_id}: {has_paid}")
        return has_paid
    except Exception as e:
        logger.error(f"DB check failed for user {user_id}: {e}")
        return False 


def create_main_keyboard(has_paid: bool) -> InlineKeyboardMarkup:
    """Creates the main keyboard based on the user's payment status (requires has_paid as input)."""
    # 💡 DEBUG: מראה איזה מקלדת נוצרה
    logger.debug(f"Creating keyboard with has_paid={has_paid}")

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
