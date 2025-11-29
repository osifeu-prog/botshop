from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import Config
from core.logging import logger


def safe_get_url(primary: Optional[str], fallback: str) -> str:
    if primary and primary.startswith("http"):
        return primary
    return fallback


def check_user_payment(user_id: Optional[int]) -> bool:
    """Placeholder: in the future query DB / API.

    For now always False so everyone sees the '39₪' path.
    """
    logger.info("check_user_payment called", user_id=user_id)
    return False


def create_main_keyboard(user_id: int | None = None) -> InlineKeyboardMarkup:
    has_paid = check_user_payment(user_id) if user_id else False

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
