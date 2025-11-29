from telegram import Update
from telegram.ext import ContextTypes, CallbackQueryHandler, Application

from core.logging import logger
from core.metrics import COMMANDS_PROCESSED, REQUEST_DURATION
from .keyboard import create_main_keyboard


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
            await query.edit_message_text(
                "📈 מידע למשקיעים\n\n"
                "מערכת החיסכון וההשקעות של SLH/SELA בנויה כקרן קהילתית שקופה, "
                "עם מודלים מתמטיים, טוקן SLH על גבי BSC, ואפשרות חיבור עתידי גם ל‑TON ו‑רשתות נוספות."
            )
        elif data == "premium_content":
            await query.answer("גישה לתוכן המלא")
            await query.edit_message_text(
                "🚀 גישה מלאה לתוכן הפרימיום, בוטי בורסה, ניתוחים מתקדמים וחיבור למערכת האקדמיה של SLH."
            )
        else:
            await query.answer("עוד מעט...")
            await query.edit_message_reply_markup(reply_markup=create_main_keyboard(query.from_user.id))


def register_callback_handlers(app: Application):
    app.add_handler(CallbackQueryHandler(generic_callback))
