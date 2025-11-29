# bot/handlers/payment_review.py

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, Application, MessageHandler, filters

from bot.config import Config
from core.logging import logger

# פונקציה ליצירת המקלדת של המנהל
def create_review_keyboard(user_id: int) -> InlineKeyboardMarkup:
    # ה-Callback data יכיל את סוג הפעולה ואת ה-user_id 
    approve_data = f"review_approve_{user_id}"
    reject_data = f"review_reject_{user_id}"
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ אשר תשלום", callback_data=approve_data),
            InlineKeyboardButton("❌ דחה תשלום", callback_data=reject_data)
        ]
    ])
    return keyboard

async def payment_image_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.photo:
        return # נתעלם אם אין תמונה
    
    # בואו נניח שהתמונה הגדולה ביותר היא הרלוונטית
    photo_file_id = message.photo[-1].file_id
    user = message.from_user
    
    logger.info("Received potential payment proof", user_id=user.id, file_id=photo_file_id)
    
    # שלח את התמונה לצ'אט המנהלים
    try:
        review_keyboard = create_review_keyboard(user_id=user.id)
        admin_chat_id = Config.ADMIN_ALERT_CHAT_ID
        
        caption_text = (
            f"💰 **בקשת אישור תשלום (תמונה)**\n\n"
            f"**מאת:** {user.full_name} (`{user.id}`)\n"
            f"**יוזר:** @{user.username or 'ללא'}\n"
            f"**כיתוב מקורי:** {message.caption or 'ללא כיתוב'}"
        )
        
        await context.bot.send_photo(
            chat_id=admin_chat_id,
            photo=photo_file_id,
            caption=caption_text,
            reply_markup=review_keyboard,
            parse_mode='Markdown'
        )
        
        await message.reply_text("קיבלנו את התמונה. אנו בודקים את אישור התשלום ונחזור אליך בהקדם.")

    except Exception as e:
        logger.error(f"Failed to forward payment proof to admin: {e}")
        await message.reply_text("אירעה שגיאה בשליחת אישור התשלום למנהל. אנא נסה שוב או פנה לתמיכה.")


def register_payment_review_handler(app: Application):
    # הוסף Handler שמגיב להודעות המכילות תמונה (filters.PHOTO)
    # ובמקביל לא מגיב לפקודות (כמו /start)
    app.add_handler(
        MessageHandler(filters.PHOTO & ~filters.COMMAND, payment_image_handler)
    )
