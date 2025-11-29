from telegram import Update
from telegram.ext import ContextTypes, CallbackQueryHandler, Application

from core.logging import logger
from core.metrics import COMMANDS_PROCESSED, REQUEST_DURATION
from core.cache import get_cached_message
from bot.config import Config
from .keyboard import create_main_keyboard, check_user_payment 
# TODO: ודא שה-import הבא נכון ושהפונקציה קיימת ב-core/db.py
from core.db import update_user_payment_status 

# הלינק לקבוצה שהוגדר על ידי המשתמש
PREMIUM_GROUP_LINK = "https://t.me/+HIzvM8sEgh1kNWY0"


# פונקציה חדשה לטיפול באישור ודחייה
async def payment_review_callback(query, context: ContextTypes.DEFAULT_TYPE, action: str, user_to_update_id: int):
    
    # ודא שהפעולה בוצעה על ידי מנהל (או משתמש מורשה)
    if query.from_user.id not in Config.ADMIN_OWNER_IDS:
        await query.answer("אינך מורשה לבצע פעולה זו.")
        return

    # הסר את הכפתורים מההודעה כדי למנוע לחיצות כפולות
    await query.edit_message_reply_markup(reply_markup=None)
    
    admin_name = query.from_user.full_name
    
    try:
        # קריאה לפונקציית ה-DB לעדכון סטטוס המשתמש
        is_approved = action == "approve"
        await update_user_payment_status(user_to_update_id, is_approved) 
        
        # בניית ההודעות
        if is_approved:
            
            # הודעה למנהל המערכת (היכן שהכפתור נלחץ)
            admin_response = f"✅ **אושר!** התשלום עבור משתמש `{user_to_update_id}` אושר על ידי {admin_name}."
            await context.bot.send_message(query.message.chat_id, admin_response, parse_mode='Markdown')
            
            # הודעה למשתמש
            user_response = (
                f"✅ **התשלום אושר!**\n\n"
                f"תודה רבה על הצטרפותך. להלן הקישור לקבוצת ההטבות הסגורה:\n"
                f"**{PREMIUM_GROUP_LINK}**\n\n"
                f"לחץ על /start כדי לרענן את המקלדת בבוט."
            )
            await context.bot.send_message(user_to_update_id, user_response, disable_web_page_preview=True)
            
        else: # reject
            
            # הודעה למנהל המערכת
            admin_response = f"❌ **נדחה!** התשלום עבור משתמש `{user_to_update_id}` נדחה על ידי {admin_name}."
            await context.bot.send_message(query.message.chat_id, admin_response, parse_mode='Markdown')
            
            # הודעה למשתמש
            user_response = (
                "❌ **אישור התשלום נדחה.**\n\n"
                "נא ודא כי התמונה ברורה ומכילה את כל פרטי התשלום הנדרשים.\n"
                "אנא שלח את התמונה שוב או פנה לתמיכה."
            )
            await context.bot.send_message(user_to_update_id, user_response)

    except Exception as e:
        logger.error(f"Failed to process payment review for {user_to_update_id}: {e}")
        await query.answer(f"אירעה שגיאת שרת: {e}")


# הפונקציה הראשית
async def generic_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    with REQUEST_DURATION.time():
        data = query.data or ""
        logger.info("Callback query", data=data, user_id=query.from_user.id)
        COMMANDS_PROCESSED.labels(command=f"cb_{data}").inc()

        # ----------------------------------------------------
        # * טיפול בפעולות אישור תשלום של מנהל *
        # ----------------------------------------------------
        if data.startswith("review_"):
            parts = data.split('_') # review_approve_USERID
            if len(parts) >= 3:
                action = parts[1] # 'approve' או 'reject'
                # מזהה המשתמש לעדכון נמצא במקום השלישי
                user_to_update_id = int(parts[2]) 
                await payment_review_callback(query, context, action, user_to_update_id)
                return
        # ----------------------------------------------------
        
        # טיפול ב-Callbacks הרגילים
        if data == "open_investor":
            await query.answer("מידע למשקיעים")
            text = get_cached_message("investor_info_he", fallback=(
                "📈 מידע למשקיעים\n\n"
                "מערכת החיסכון וההשקעות של SLH/SELA בנויה כקרן קהילתית שקופה, "
                "עם מודלים מתמטיים, טוקן SLH על גבי BSC, ואפשרות חיבור עתידי גם ל‑TON ו‑רשתות נוספות."
            ))
            await query.edit_message_text(text)
            
        elif data == "premium_content":
            await query.answer("גישה לתוכן המלא")
            text = get_cached_message("premium_content_he", fallback=(
                "🚀 גישה מלאה לתוכן הפרימיום, בוטי בורסה, ניתוחים מתקדמים וחיבור למערכת האקדמיה של SLH."
            ))
            await query.edit_message_text(text)

        else:
            await query.answer("עוד מעט...")
            # רענון המקלדת לאחר בדיקה מחודשת של סטטוס התשלום
            user_id = query.from_user.id
            has_paid = await check_user_payment(user_id)
            await query.edit_message_reply_markup(reply_markup=create_main_keyboard(has_paid=has_paid))


def register_callback_handlers(app: Application):
    app.add_handler(CallbackQueryHandler(generic_callback))
