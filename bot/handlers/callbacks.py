from telegram import Update
from telegram.ext import ContextTypes, CallbackQueryHandler, Application

from core.logging import logger
from core.metrics import COMMANDS_PROCESSED, REQUEST_DURATION
from core.cache import get_cached_message
# ייבוא הפונקציות המעודכנות:
from .keyboard import create_main_keyboard, check_user_payment 


async def generic_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    with REQUEST_DURATION.time():
        data = query.data or ""
        logger.info("Callback query", data=data, user_id=query.from_user.id)
        COMMANDS_PROCESSED.labels(command=f"cb_{data}").inc()

        if data == "open_investor":
            await query.answer("מידע למשקיעים")
            
            # --- שימוש ב-Cache ---
            text = get_cached_message("investor_info_he", fallback=(
                "📈 מידע למשקיעים\n\n"
                "מערכת החיסכון וההשקעות של SLH/SELA בנויה כקרן קהילתית שקופה, "
                "עם מודלים מתמטיים, טוקן SLH על גבי BSC, ואפשרות חיבור עתידי גם ל‑TON ו‑רשתות נוספות."
            ))
            await query.edit_message_text(text)
            
        elif data == "premium_content":
            await query.answer("גישה לתוכן המלא")

            # --- שימוש ב-Cache ---
            text = get_cached_message("premium_content_he", fallback=(
                "🚀 גישה מלאה לתוכן הפרימיום, בוטי בורסה, ניתוחים מתקדמים וחיבור למערכת האקדמיה של SLH."
            ))
            await query.edit_message_text(text)

        else:
            await query.answer("עוד מעט...")
            
            # **********************************************
            # קריאה לבדיקת התשלום האסינכרונית:
            user_id = query.from_user.id
            has_paid = await check_user_payment(user_id)
            # **********************************************
            
            # העברת התוצאה לפונקציית המקלדת:
            await query.edit_message_reply_markup(reply_markup=create_main_keyboard(has_paid=has_paid))


def register_callback_handlers(app: Application):
    app.add_handler(CallbackQueryHandler(generic_callback))
