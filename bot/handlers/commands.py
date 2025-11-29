from telegram import Update
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters, Application

from core.logging import logger
from core.cache import get_cached_message
from core.metrics import COMMANDS_PROCESSED, REQUEST_DURATION
from bot.config import Config
from .keyboard import create_main_keyboard, check_user_payment 


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with REQUEST_DURATION.time():
        COMMANDS_PROCESSED.labels(command="start").inc()

        user = update.effective_user
        user_id = user.id if user else None
        
        logger.info("Handling /start", user_id=user_id)

        # ----------------------------------------------------
        # * שליחת התראה למנהל המערכת על משתמש חדש *
        # ----------------------------------------------------
        if user and not context.user_data.get('is_registered'):
            chat_id = Config.ADMIN_ALERT_CHAT_ID
            username = f"@{user.username}" if user.username else "ללא שם משתמש"
            
            alert_text = (
                f"👤 **משתמש חדש התחיל את הבוט!**\n\n"
                f"**ID:** `{user.id}`\n"
                f"**שם:** {user.full_name}\n"
                f"**יוזר:** {username}\n"
                f"**קישור:** [התחל צ'אט](tg://user?id={user.id})"
            )
            
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=alert_text,
                    parse_mode='Markdown'
                )
                context.user_data['is_registered'] = True
            except Exception as e:
                logger.error(f"Failed to send admin START alert: {e}")
        # ----------------------------------------------------
        
        # קריאה לבדיקת התשלום האסינכרונית
        has_paid = await check_user_payment(user_id) 

        intro = get_cached_message("start_main_he", fallback=(
            "🚀 ברוך הבא ל-SLH Savings & Investments Bot!\n\n"
            "כאן נוכל לחבר בין חיסכון, השקעות וקהילה – צעד אחר צעד."
        ))

        keyboard = create_main_keyboard(has_paid=has_paid) 
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


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responds to unknown commands that are not start/help but start with '/'."""
    with REQUEST_DURATION.time():
        COMMANDS_PROCESSED.labels(command="unknown").inc()
        text = get_cached_message("unknown_cmd_he", fallback=(
            "🤔 פקודה לא מוכרת. אנא נסה להשתמש בפקודות הבאות:\n"
            "/start – פתיחת המערכת מחדש\n"
            "/help – רשימת פקודות מלאה"
        ))
        await update.message.reply_text(text)


def register_command_handlers(app: Application):
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    
    # מטפל זה צריך להיות בסוף, כדי לתפוס את כל הפקודות שלא טופלו קודם
    app.add_handler(
        MessageHandler(filters.COMMAND, unknown_command)
    )
