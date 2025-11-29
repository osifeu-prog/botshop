from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters, Application # <--- MessageHandler ו-filters נוספו

from core.logging import logger
from core.cache import get_cached_message
from core.metrics import COMMANDS_PROCESSED, REQUEST_DURATION
# ייבוא הפונקציות המעודכנות:
from .keyboard import create_main_keyboard, check_user_payment 


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with REQUEST_DURATION.time():
        COMMANDS_PROCESSED.labels(command="start").inc()

        user = update.effective_user
        logger.info("Handling /start", user_id=user.id if user else None)
        
        # **********************************************
        # קריאה לבדיקת התשלום האסינכרונית:
        user_id = user.id if user else None
        has_paid = await check_user_payment(user_id) 
        # **********************************************

        intro = get_cached_message("start_main_he", fallback=(
            "🚀 ברוך הבא ל-SLH Savings & Investments Bot!\n\n"
            "כאן נוכל לחבר בין חיסכון, השקעות וקהילה – צעד אחר צעד."
        ))

        # העברת התוצאה לפונקציית המקלדת:
        keyboard = create_main_keyboard(has_paid=has_paid) 
        await update.message.reply_text(intro, reply_markup=keyboard, disable_web_page_preview=True)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # ... (הפונקציה help_command נשארת ללא שינוי)
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
    # ... (הפונקציה unknown_command נשארת ללא שינוי)
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
