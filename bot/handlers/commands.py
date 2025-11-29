from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, Application

from core.logging import logger
from core.cache import get_cached_message
from core.metrics import COMMANDS_PROCESSED, REQUEST_DURATION
from .keyboard import create_main_keyboard


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with REQUEST_DURATION.time():
        COMMANDS_PROCESSED.labels(command="start").inc()

        user = update.effective_user
        logger.info("Handling /start", user_id=user.id if user else None)

        intro = get_cached_message("start_main_he", fallback=(
            "🚀 ברוך הבא ל-SLH Savings & Investments Bot!\n\n"
            "כאן נוכל לחבר בין חיסכון, השקעות וקהילה – צעד אחר צעד."
        ))

        keyboard = create_main_keyboard(user_id=user.id if user else None)
        await update.message.reply_text(intro, reply_markup=keyboard, disable_web_page_preview=True)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with REQUEST_DURATION.time():
        COMMANDS_PROCESSED.labels(command="help").inc()

        text = get_cached_message("help_he", fallback=(
            "ℹ️ פקודות עיקריות:\n"
            "/start – מסך פתיחה והסבר על המערכת\n"
            "/mathematics – איך המודלים המתמטיים עובדים\n"
            "/deposit – איך מצטרפים ומבצעים הפקדה\n"
            "/transparency – דוח שקיפות קהילתי\n"
            "/legal – מידע משפטי והצהרות סיכון"
        ))
        await update.message.reply_text(text, disable_web_page_preview=True)


def register_command_handlers(app: Application):
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
