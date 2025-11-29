from telegram.ext import MessageHandler, filters, CallbackQueryHandler
import os
import json
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime

from db import init_schema, get_approval_stats, get_monthly_payments, get_reserve_stats, log_payment, update_payment_status, has_approved_payment, get_pending_payments

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware

from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from telegram import Update
try:
    from slh_public_api import router as public_router
except Exception:
    public_router = None
try:
    from social_api import router as social_router
except Exception:
    social_router = None
try:
    from slh_core_api import router as core_router
except Exception:
    core_router = None
try:
    from slhnet_extra import router as slhnet_extra_router
except Exception:
    slhnet_extra_router = None

from telegram.ext import CommandHandler, ContextTypes, Application

# =========================
# קונפיגורציית לוגינג משופרת
# =========================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("slhnet_bot.log", encoding='utf-8')
    ]
)
logger = logging.getLogger("slhnet")

# =========================
# FastAPI app
# =========================
app = FastAPI(
    title="SLHNET Gateway Bot",
    description="בוט קהילה ושער API עבור SLHNET",
    version="2.0.0"
)

# CORS – מאפשר גישה לדשבורד מהדומיין slh-nft.com
allowed_origins = [
    os.getenv("FRONTEND_ORIGIN", "").rstrip("/") or "https://slh-nft.com",
    "https://slh-nft.com",
    "https://www.slh-nft.com",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# אתחול סכמת בסיס הנתונים (טבלאות + רזרבות 49%)
try:
    init_schema()
except Exception as e:
    logger.warning(f"init_schema failed: {e}")

BASE_DIR = Path(__file__).resolve().parent

# סטטיק וטמפלטס עם הגנות
try:
    static_dir = BASE_DIR / "static"
    templates_dir = BASE_DIR / "templates"
    
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    else:
        logger.warning("Static directory not found, skipping static files")
    
    if templates_dir.exists():
        templates = Jinja2Templates(directory=str(templates_dir))
    else:
        logger.warning("Templates directory not found, Jinja2 templates disabled")
        templates = None
except Exception as e:
    logger.error(f"Error setting up static/templates: {e}")
    templates = None


# רואטרים של API עם הגנות
try:
    if public_router is not None:
        app.include_router(public_router, prefix="/api/public", tags=["public"])
    if social_router is not None:
        app.include_router(social_router, prefix="/api/social", tags=["social"])
    if core_router is not None:
        app.include_router(core_router, prefix="/api/core", tags=["core"])
    if slhnet_extra_router is not None:
        app.include_router(slhnet_extra_router, prefix="/api/extra", tags=["extra"])
except Exception as e:
    logger.error(f"Error including routers: {e}")


# =========================
# ניהול referral משופר
# =========================
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
REF_FILE = DATA_DIR / "referrals.json"


def load_referrals() -> Dict[str, Any]:
    """טוען נתוני referrals עם הגנת שגיאות"""
    if not REF_FILE.exists():
        return {"users": {}, "statistics": {"total_users": 0}}
    
    try:
        with open(REF_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data
    except (json.JSONDecodeError, Exception) as e:
        logger.error(f"Error loading referrals: {e}")
        return {"users": {}, "statistics": {"total_users": 0}}


def save_referrals(data: Dict[str, Any]) -> None:
    """שומר נתוני referrals עם הגנת שגיאות"""
    try:
        # עדכון סטטיסטיקות
        data["statistics"]["total_users"] = len(data["users"])
        
        with open(REF_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving referrals: {e}")


def register_referral(user_id: int, referrer_id: Optional[int] = None) -> bool:
    """רושם משתמש חדש עם referral"""
    try:
        data = load_referrals()
        suid = str(user_id)
        
        if suid in data["users"]:
            return False  # כבר רשום
            
        user_data = {
            "referrer": str(referrer_id) if referrer_id else None,
            "joined_at": datetime.now().isoformat(),
            "referral_count": 0
        }
        
        data["users"][suid] = user_data
        
        # עדכן סטטיסטיקת referrer אם קיים
        if referrer_id:
            referrer_str = str(referrer_id)
            if referrer_str in data["users"]:
                data["users"][referrer_str]["referral_count"] = data["users"][referrer_str].get("referral_count", 0) + 1
        
        save_referrals(data)
        logger.info(f"Registered new user {user_id} with referrer {referrer_id}")
        return True
        
    except Exception as e:
        logger.error(f"Error registering referral: {e}")
        return False


# =========================
# ניהול הודעות משופר
# =========================
MESSAGES_FILE = BASE_DIR / "bot_messages_slhnet.txt"


def load_message_block(block_name: str, fallback: str = "") -> str:
    """
    טוען בלוק טקסט מהקובץ עם הגנות וטקסט ברירת מחדל
    """
    if not MESSAGES_FILE.exists():
        logger.warning(f"Messages file not found: {MESSAGES_FILE}")
        return fallback or f"[שגיאה: קובץ הודעות לא נמצא]"

    try:
        content = MESSAGES_FILE.read_text(encoding="utf-8")
        lines = content.splitlines()

        result_lines = []
        in_block = False
        found_block = False
        
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("===") and block_name in stripped:
                in_block = True
                found_block = True
                continue
            if in_block and stripped.startswith("=== END"):
                break
            if in_block:
                result_lines.append(line)

        if not found_block and not fallback:
            logger.warning(f"Message block '{block_name}' not found")
            return f"[שגיאה: בלוק {block_name} לא נמצא]"
            
        if not result_lines and fallback:
            return fallback
            
        return "\n".join(result_lines).strip() or fallback
        
    except Exception as e:
        logger.error(f"Error loading message block '{block_name}': {e}")
        return fallback or f"[שגיאה בטעינת בלוק {block_name}]"


# =========================
# מודלים עם ולידציה
# =========================
class TelegramWebhookUpdate(BaseModel):
    update_id: int
    message: Optional[Dict[str, Any]] = None
    callback_query: Optional[Dict[str, Any]] = None
    edited_message: Optional[Dict[str, Any]] = None


class HealthResponse(BaseModel):
    status: str
    service: str
    timestamp: str
    version: str


# =========================
# קונפיגורציה ומשתני סביבה
# =========================

def is_admin(user_id: int) -> bool:
    """בודק אם המשתמש הוא אדמין לפי ADMIN_OWNER_IDS"""
    raw = os.getenv("ADMIN_OWNER_IDS", "")
    for part in raw.replace(",", " ").split():
        try:
            if int(part) == int(user_id):
                return True
        except ValueError:
            continue
    return False



class Config:
    """מחלקה לניהול קונפיגורציה"""
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "")
    ADMIN_ALERT_CHAT_ID: str = os.getenv("ADMIN_ALERT_CHAT_ID", "")
    LANDING_URL: str = os.getenv("LANDING_URL", "https://slh-nft.com")
    BUSINESS_GROUP_URL: str = os.getenv("BUSINESS_GROUP_URL", "")
    GROUP_STATIC_INVITE: str = os.getenv("GROUP_STATIC_INVITE", "")
    PAYBOX_URL: str = os.getenv("PAYBOX_URL", "")
    BIT_URL: str = os.getenv("BIT_URL", "")
    PAYPAL_URL: str = os.getenv("PAYPAL_URL", "")
    TON_WALLET_ADDRESS: str = os.getenv("TON_WALLET_ADDRESS", "")
    START_IMAGE_PATH: str = os.getenv("START_IMAGE_PATH", "assets/start_banner.jpg")
    LOGS_GROUP_CHAT_ID: str = os.getenv("LOGS_GROUP_CHAT_ID", ADMIN_ALERT_CHAT_ID or "")

    @classmethod
    def validate(cls) -> List[str]:
        """בודק תקינות קונפיגורציה ומחזיר רשימת אזהרות"""
        warnings = []
        if not cls.BOT_TOKEN:
            warnings.append("⚠️ BOT_TOKEN לא מוגדר")
        if not cls.WEBHOOK_URL:
            warnings.append("⚠️ WEBHOOK_URL לא מוגדר")
        if not cls.ADMIN_ALERT_CHAT_ID:
            warnings.append("⚠️ ADMIN_ALERT_CHAT_ID לא מוגדר")
        return warnings


# =========================
# Telegram Application (singleton משופר)
# =========================
class TelegramAppManager:
    """מנהל אפליקציית הטלגרם"""
    _instance: Optional[Application] = None
    _initialized: bool = False

    @classmethod
    def get_app(cls) -> Application:
        if cls._instance is None:
            if not Config.BOT_TOKEN:
                raise RuntimeError("BOT_TOKEN is not set")
            
            cls._instance = Application.builder().token(Config.BOT_TOKEN).build()
            logger.info("Telegram Application instance created")
            
        return cls._instance

    @classmethod
    def initialize_handlers(cls) -> None:
        """מאתחל handlers פעם אחת בלבד"""
        if cls._initialized:
            return
            
        app_instance = cls.get_app()
        
        # רישום handlers

handlers = [
    CommandHandler("start", start_command),
    CommandHandler("whoami", whoami_command),
    CommandHandler("stats", stats_command),
    CommandHandler("admin", admin_command),
    CommandHandler("pending", pending_command),
    CommandHandler("approve", approve_command),
    CommandHandler("reject", reject_command),
    CallbackQueryHandler(callback_query_handler),
    MessageHandler(filters.PHOTO | filters.Document.ALL, payment_proof_handler),
    MessageHandler(filters.TEXT & ~filters.COMMAND, echo_message),
    MessageHandler(filters.COMMAND, unknown_command),
]
        
        for handler in handlers:
            app_instance.add_handler(handler)
            
        cls._initialized = True
        logger.info("Telegram handlers initialized")
    @classmethod
    async def start(cls) -> None:
        """אתחול מלא של אפליקציית הטלגרם + Webhook"""
        # רישום handlers פעם אחת
        cls.initialize_handlers()
        app_instance = cls.get_app()
        if not getattr(cls, "_started", False):
            await app_instance.initialize()
            await app_instance.start()
            try:
                if Config.WEBHOOK_URL:
                    await app_instance.bot.set_webhook(Config.WEBHOOK_URL)
                    logger.info(f"Webhook set to {Config.WEBHOOK_URL}")
            except Exception as e:
                logger.error(f"Failed to set webhook: {e}")
            cls._started = True
            logger.info("Telegram Application started")

    @classmethod
    async def shutdown(cls) -> None:
        """עצירת האפליקציה בצורה נקייה"""
        try:
            app_instance = cls.get_app()
            await app_instance.stop()
            await app_instance.shutdown()
        except Exception as e:
            logger.error(f"Error during Telegram shutdown: {e}")


# =========================
# utilities משופרות
# =========================
async def send_log_message(text: str) -> None:
    """שולח הודעת לוג עם הגנות"""
    if not Config.LOGS_GROUP_CHAT_ID:
        logger.warning("LOGS_GROUP_CHAT_ID not set; skipping log message")
        return
        
    try:
        app_instance = TelegramAppManager.get_app()
        await app_instance.bot.send_message(
            chat_id=int(Config.LOGS_GROUP_CHAT_ID), 
            text=text
        )
    except Exception as e:
        logger.error(f"Failed to send log message: {e}")


def safe_get_url(url: str, fallback: str) -> str:
    """מחזיר URL עם הגנות"""
    return url if url and url.startswith(('http://', 'https://')) else fallback


# =========================
# handlers משופרים
# =========================

def build_payment_instructions() -> str:
    """מחזיר טקסט עם כל אמצעי התשלום וההסבר ללקוח."""
    lines: List[str] = [
        "💳 *אמצעי תשלום להצטרפות ב-39 ₪:*",
        "",
        "1️⃣ העברה בנקאית:",
        "   בנק הפועלים, סניף כפר גנים (153)",
        "   חשבון: 73462",
        "   המוטב: קאופמן צביקה",
        "",
    ]
    if Config.PAYBOX_URL:
        lines.append(f"2️⃣ PayBox: {Config.PAYBOX_URL}")
    if getattr(Config, "BIT_URL", None):
        lines.append(f"3️⃣ ביט: {Config.BIT_URL}")
    if Config.PAYPAL_URL:
        lines.append(f"4️⃣ PayPal: {Config.PAYPAL_URL}")
    if Config.TON_WALLET_ADDRESS:
        lines.append(f"5️⃣ ארנק TON: `{Config.TON_WALLET_ADDRESS}`")
    lines.append("")
    lines.append("לאחר שביצעת תשלום, שלח לבוט צילום מסך / אישור תשלום כהודעת תמונה או קובץ.")
    lines.append("המערכת תעביר את האישור לאדמין, ולאחר האישור תקבל קישור לקבוצת העסקים וגישה מלאה.")
    return "\n".join(lines)



async def send_start_screen(update: Update, context: ContextTypes.DEFAULT_TYPE, referrer: Optional[int] = None) -> None:
    """מציג מסך start עם הגנות"""
    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat:
        logger.error("No user or chat in update")
        return

    # רישום referral
    if user:
        register_referral(user.id, referrer)

    # טעינת הודעות עם ברירת מחדל
    title = load_message_block("START_TITLE", "🚀 ברוך הבא ל-SLHNET!")
    body = load_message_block("START_BODY", "הצטרף לקהילת העסקים של SLHNET וקבל גישה למערכת איקומרס בטלגרם, חוזים חכמים, קבלות NFT ורשת עסקים חכמה סביב טוקן SLH.")

    # שליחת תמונה עם הגנות
    image_path = BASE_DIR / Config.START_IMAGE_PATH
    try:
        if image_path.exists() and image_path.is_file():
            with image_path.open("rb") as f:
                await chat.send_photo(photo=InputFile(f), caption=title)
        else:
            logger.warning(f"Start image not found: {image_path}")
            await chat.send_message(text=title)
    except Exception as e:
        logger.error(f"Error sending start image: {e}")
        await chat.send_message(text=title)


# בניית כפתורים עם הגנות URL
pay_url = safe_get_url(Config.PAYBOX_URL, Config.LANDING_URL + "#join39")
more_info_url = safe_get_url(Config.LANDING_URL, "https://slh-nft.com")
group_url = safe_get_url(Config.BUSINESS_GROUP_URL or Config.GROUP_STATIC_INVITE, more_info_url)

has_paid = False
try:
    if user:
        has_paid = has_approved_payment(user.id)
except Exception as e:
    logger.error(f"Error checking approved payment for user {user.id}: {e}")

keyboard: List[List[InlineKeyboardButton]] = [
    [InlineKeyboardButton("💳 תשלום 39 ₪ וגישה מלאה", url=pay_url)],
    [InlineKeyboardButton("ℹ️ לפרטים נוספים", url=more_info_url)],
]
if has_paid:
    keyboard.append([InlineKeyboardButton("👥 כניסה לקבוצת העסקים", url=group_url)])
else:
    keyboard.append([InlineKeyboardButton("📤 שליחת אישור תשלום", callback_data="send_payment_instructions")])

keyboard.append([InlineKeyboardButton("📈 מידע למשקיעים", callback_data="open_investor")])
reply_markup = InlineKeyboardMarkup(keyboard)

    await chat.send_message(text=body, reply_markup=reply_markup)

    # לוגים
    log_text = (
        f"📥 משתמש חדש הפעיל את הבוט\n"
        f"👤 User ID: {user.id}\n"
        f"📛 Username: @{user.username or 'לא מוגדר'}\n"
        f"🔰 שם: {user.full_name}\n"
        f"🔄 Referrer: {referrer or 'לא צוין'}"
    )
    await send_log_message(log_text)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """פקודת start עם referral"""
    referrer = None
    if context.args:
        try:
            referrer = int(context.args[0])
            logger.info(f"Start command with referrer: {referrer}")
        except (ValueError, TypeError):
            logger.warning(f"Invalid referrer ID: {context.args[0]}")

    await send_start_screen(update, context, referrer=referrer)


async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """פקודת whoami משופרת"""
    user = update.effective_user
    chat = update.effective_chat

    if not user:
        await chat.send_message("❌ לא זיהיתי משתמש.")
        return

    # מידע נוסף מהרפר�rals
    referrals_data = load_referrals()
    user_ref_data = referrals_data["users"].get(str(user.id), {})
    
    text = (
        f"👤 **פרטי המשתמש שלך:**\n"
        f"🆔 ID: `{user.id}`\n"
        f"📛 שם משתמש: @{user.username or 'לא מוגדר'}\n"
        f"🔰 שם מלא: {user.full_name}\n"
        f"🔄 מספר הפניות: {user_ref_data.get('referral_count', 0)}\n"
        f"📅 הצטרף: {user_ref_data.get('joined_at', 'לא ידוע')}"
    )
    
    await chat.send_message(text=text, parse_mode="Markdown")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """פקודת stats חדשה - סטטיסטיקות"""
    user = update.effective_user
    chat = update.effective_chat

    if not user:
        return

    referrals_data = load_referrals()
    stats = referrals_data.get("statistics", {})
    
    text = (
        f"📊 **סטטיסטיקות קהילה:**\n"
        f"👥 סה״כ משתמשים: {stats.get('total_users', 0)}\n"
        f"📈 משתמשים פעילים: {len(referrals_data.get('users', {}))}\n"
        f"🔄 הפניות כוללות: {sum(u.get('referral_count', 0) for u in referrals_data.get('users', {}).values())}"
    )
    
    await chat.send_message(text=text, parse_mode="Markdown")




async def payment_proof_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """קבלת צילום/קובץ כאישור תשלום והעברת הלוג לקבוצת הניהול."""
    user = update.effective_user
    chat = update.effective_chat
    message = update.message

    if not user or not chat or not message:
        return

    # נוודא שזה בפרטי מול הבוט
    if chat.type != "private":
        return

    caption = message.caption or ""
    text_lower = caption.lower()

    pay_method = "unknown"
    if "ביט" in caption or "bit" in text_lower:
        pay_method = "bit"
    elif "paybox" in text_lower or "פייבוקס" in caption or "פייבוקס" in text_lower:
        pay_method = "paybox"
    elif "paypal" in text_lower or "פייפאל" in caption or "פייפאל" in text_lower:
        pay_method = "paypal"
    elif "העברה" in caption or "bank" in text_lower or "בנקה" in text_lower:
        pay_method = "bank-transfer"
    else:
        pay_method = "screenshot"

    try:
        log_payment(user.id, user.username, pay_method)
    except Exception as e:
        logger.error(f"Error logging payment for user {user.id}: {e}")

    # העתקת ההודעה לקבוצת הלוגים/ניהול
    if Config.LOGS_GROUP_CHAT_ID:
        try:
            admin_chat_id = int(Config.LOGS_GROUP_CHAT_ID)
            await context.bot.copy_message(
                chat_id=admin_chat_id,
                from_chat_id=chat.id,
                message_id=message.message_id,
            )

            # כפתורי אישור/דחייה
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ אישור תשלום", callback_data=f"approve:{user.id}"),
                    InlineKeyboardButton("❌ דחיית תשלום", callback_data=f"reject:{user.id}"),
                ]
            ])

            admin_text = (
                "📥 התקבל אישור תשלום חדש.\n\n"
                f"user_id = {user.id}\n"
                f"username = @{user.username or 'לא ידוע'}\n"
                f"from chat_id = {chat.id}\n"
                f"שיטת תשלום: {pay_method}\n\n"
                "לאישור (עבור אדמין ראשי):\n"
                f"/approve {user.id}\n"
                f"/reject {user.id} <סיבה>\n"
                "(או להשתמש בכפתורי האישור/דחייה מתחת להודעה זו)"
            )

            await context.bot.send_message(
                chat_id=admin_chat_id,
                text=admin_text,
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.error(f"Error sending payment log to admin group: {e}")

    await chat.send_message(
        "📥 קיבלנו את אישור התשלום שלך!\n"
        "ההודעה הועברה לצוות הניהול. לאחר אישור, ישלח אליך קישור לקבוצת העסקים.",
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """פאנל ניהול בסיסי למנהלים בלבד."""
    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat:
        return

    if not is_admin(user.id):
        await chat.send_message("❌ הפקודה /admin מיועדת למנהלי המערכת בלבד.")
        return

    approval_stats = get_approval_stats() or {}
    reserve_stats = get_reserve_stats() or {}

    text_lines = [
        "🛠 *פאנל ניהול SLHNET*",
        "",
        "💳 *סטטוס תשלומים:*",
        f" - ממתינים: {approval_stats.get('pending', 0)}",
        f" - אושרו: {approval_stats.get('approved', 0)}",
        f" - נדחו: {approval_stats.get('rejected', 0)}",
        "",
        "🏦 *רזרבות ותזרים (Demo מה-DB):*",
        f" - סכום רזרבה מצטבר: {reserve_stats.get('total_reserve', 0)}",
        f" - סך נטו: {reserve_stats.get('total_net', 0)}",
        f" - סך תשלומים: {reserve_stats.get('total_payments', 0)}",
        "",
        "📋 *פקודות ניהול זמינות:*",
        " - /pending  – רשימת תשלומים ממתינים",
        " - /approve <user_id>  – אישור תשלום ושליחת קישור לקבוצה",
        " - /reject <user_id> <סיבה>  – דחיית תשלום והודעה ללקוח",
        "",
        "💼 *אזור ארנקים ו-SLH:*",
        " - הגדרת ארנקים ב-BSC מתבצעת דרך MetaMask / ארנק תואם.",
        " - טוקן SLH: 0xACb0A09414CEA1C879c67bB7A877E4e19480f022 (ChainID 56).",
        " - ניתן להגדיר בהמשך חיבור מלא בין ה-DB לבין חוזה ה-SLH להעברות אוטומטיות.",
    ]

    await chat.send_message("\n".join(text_lines), parse_mode="Markdown")


async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """רשימת תשלומים ממתינים – למנהלים בלבד."""
    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat:
        return

    if not is_admin(user.id):
        await chat.send_message("❌ הפקודה /pending מיועדת למנהלי המערכת בלבד.")
        return

    pending = get_pending_payments(limit=30)
    if not pending:
        await chat.send_message("✅ אין תשלומים ממתינים כרגע.")
        return

    lines = ["💳 *תשלומים ממתינים:*", ""]
    for p in pending:
        created = p["created_at"]
        lines.append(
            f"- user_id={p['user_id']}  "
            f"@{p.get('username') or '-'}  "
            f"method={p['pay_method']}  "
            f"date={created}"
        )

    lines.append("")
    lines.append("לאישור מהיר: /approve <user_id>  |  לדחייה: /reject <user_id> <סיבה>")

    await chat.send_message("\n".join(lines), parse_mode="Markdown")


async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """אישור תשלום ידני לפי user_id."""
    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat:
        return

    if not is_admin(user.id):
        await chat.send_message("❌ הפקודה /approve מיועדת למנהלי המערכת בלבד.")
        return

    if not context.args:
        await chat.send_message("שימוש: /approve <user_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await chat.send_message("user_id לא תקין.")
        return

    try:
        update_payment_status(target_id, "approved", "approved via /approve")
    except Exception as e:
        logger.error(f"Error updating payment status for {target_id}: {e}")
        await chat.send_message("❌ שגיאה בעדכון סטטוס התשלום.")
        return

    group_url = safe_get_url(Config.BUSINESS_GROUP_URL or Config.GROUP_STATIC_INVITE, Config.LANDING_URL)

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                "✅ התשלום שלך אושר!\n\n"
                "הנה הקישור להצטרפות לקהילת העסקים שלנו:\n"
                f"{group_url}\n\n"
                "ברוך הבא 🙌"
            ),
        )
    except Exception as e:
        logger.error(f"Error sending approval message to user {target_id}: {e}")

    await chat.send_message(f"✅ התשלום של המשתמש {target_id} אושר ונשלח לו קישור לקבוצה.")


async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """דחיית תשלום לפי user_id + סיבה."""
    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat:
        return

    if not is_admin(user.id):
        await chat.send_message("❌ הפקודה /reject מיועדת למנהלי המערכת בלבד.")
        return

    if len(context.args) < 1:
        await chat.send_message("שימוש: /reject <user_id> <סיבה>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await chat.send_message("user_id לא תקין.")
        return

    reason = " ".join(context.args[1:]) if len(context.args) > 1 else "ללא סיבה מפורטת"

    try:
        update_payment_status(target_id, "rejected", reason)
    except Exception as e:
        logger.error(f"Error updating payment status (reject) for {target_id}: {e}")
        await chat.send_message("❌ שגיאה בעדכון סטטוס התשלום.")
        return

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=f"❌ התשלום שלך נדחה.\nסיבה: {reason}",
        )
    except Exception as e:
        logger.error(f"Error sending rejection message to user {target_id}: {e}")

    await chat.send_message(f"🚫 התשלום של המשתמש {target_id} נדחה ונשלחה לו הודעה.")
async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """מטפל ב-callback queries"""
    query = update.callback_query
    if not query:
        return
        
    data = query.data or ""
    await query.answer()


if data == "open_investor":
    await handle_investor_callback(update, context)
elif data == "send_payment_instructions":
    text = build_payment_instructions()
    if query.message:
        await query.message.reply_text(text, parse_mode="Markdown")
elif data.startswith("approve:"):
    if not is_admin(query.from_user.id):
        await query.answer("רק מנהל יכול לאשר תשלום.", show_alert=True)
        return
    try:
        target_id = int(data.split(":", 1)[1])
    except ValueError:
        await query.answer("user_id לא תקין.", show_alert=True)
        return
    try:
        update_payment_status(target_id, "approved", "approved via inline button")
    except Exception as e:
        logger.error(f"Error updating payment status for {target_id}: {e}")
        await query.answer("שגיאה בעדכון סטטוס התשלום.", show_alert=True)
        return

    group_url = safe_get_url(Config.BUSINESS_GROUP_URL or Config.GROUP_STATIC_INVITE, Config.LANDING_URL)
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                "✅ התשלום שלך אושר!\n\n"
                "הנה הקישור להצטרפות לקהילת העסקים שלנו:\n"
                f"{group_url}\n\n"
                "ברוך הבא 🙌"
            ),
        )
    except Exception as e:
        logger.error(f"Error sending approval message to user {target_id}: {e}")

    await query.edit_message_text(f"✅ התשלום של המשתמש {target_id} אושר ונשלח לו קישור לקבוצה.")
elif data.startswith("reject:"):
    if not is_admin(query.from_user.id):
        await query.answer("רק מנהל יכול לדחות תשלום.", show_alert=True)
        return
    try:
        target_id = int(data.split(":", 1)[1])
    except ValueError:
        await query.answer("user_id לא תקין.", show_alert=True)
        return

    try:
        update_payment_status(target_id, "rejected", "rejected via inline button")
    except Exception as e:
        logger.error(f"Error updating payment status (reject) for {target_id}: {e}")
        await query.answer("שגיאה בעדכון סטטוס התשלום.", show_alert=True)
        return

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                "❌ התשלום שלך נדחה.\n"
                "אם לדעתך מדובר בטעות, ניתן לפנות לתמיכה."
            ),
        )
    except Exception as e:
        logger.error(f"Error sending rejection message to user {target_id}: {e}")

    await query.edit_message_text(f"🚫 התשלום של המשתמש {target_id} נדחה ונשלחה לו הודעה.")
else:
    await query.edit_message_text("❌ פעולה לא מוכרת.")


async def handle_investor_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """מטפל בכפתור מידע למשקיעים"""
    query = update.callback_query
    investor_text = load_message_block(
        "INVESTOR_INFO", 
        "📈 **מידע למשקיעים**\n\nלפרטים נוספים על השקעות, צור קשר עם הנהלת הפרויקט."
    )
    
    # כפתור חזרה
    keyboard = [[InlineKeyboardButton("🔙 חזרה לתפריט הראשי", callback_data="back_to_main")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(text=investor_text, reply_markup=reply_markup)


async def echo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """מטפל בהודעות טקסט רגילות"""
    user = update.effective_user
    text = update.message.text if update.message else ""
    
    logger.info(f"Message from {user.id if user else '?'}: {text}")
    
    response = load_message_block(
        "ECHO_RESPONSE",
        "✅ תודה על ההודעה! אנחנו כאן כדי לעזור.\nהשתמש ב-/start כדי לראות את התפריט הראשי."
    )
    
    await update.message.reply_text(response)


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """מטפל בפקודות לא מוכרות"""
    await update.message.reply_text(
        "❤קודה לא מוכרת. השתמש ב-/start כדי לראות את התפריט הזמין."
    )


# =========================
# Routes של FastAPI משופרים
# =========================

@app.get@app.get("/api/metrics/finance")
async def finance_metrics():
    """סטטוס כספי כולל – הכנסות, רזרבות, נטו ואישורים."""
    from datetime import datetime
    reserve_stats = get_reserve_stats() or {}
    approval_stats = get_approval_stats() or {}

    return {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "reserve": reserve_stats,
        "approvals": approval_stats,
    }



@app.get("/metrics")
async def metrics():
    """Prometheus scrape endpoint for SLHNET metrics."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Endpoint לבריאות המערכת"""
    from datetime import datetime
    return HealthResponse(
        status="ok",
        service="slhnet-telegram-gateway",
        timestamp=datetime.now().isoformat(),
        version="2.0.0"
    )


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    """דף נחיתה"""
    if not templates:
        return HTMLResponse("<h1>SLHNET Bot - Template Engine Not Available</h1>")
    
    return templates.TemplateResponse(
        "landing.html",
        {
            "request": request,
            "landing_url": safe_get_url(Config.LANDING_URL, "https://slh-nft.com"),
            "business_group_url": safe_get_url(Config.BUSINESS_GROUP_URL, "https://slh-nft.com"),
        },
    )


@app.post("/webhook")
async def telegram_webhook(update: TelegramWebhookUpdate):
    """Webhook endpoint עם הגנות"""
    try:
        # אתחול אוטומטי אם needed
        TelegramAppManager.initialize_handlers()
        app_instance = TelegramAppManager.get_app()

        # המרה ועיבוד
        raw_update = update.dict()
        ptb_update = Update.de_json(raw_update, app_instance.bot)
        
        if ptb_update:
            await app_instance.process_update(ptb_update)
            return JSONResponse({"status": "processed"})
        else:
            return JSONResponse({"status": "no_update"}, status_code=400)
            
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


@app.on_event("startup")
async def startup_event():
    """אתחול during startup"""
    warnings = Config.validate()
    for warning in warnings:
        logger.warning(warning)
    if warnings:
        await send_log_message("⚠️ **אזהרות אתחול:**\n" + "\n".join(warnings))
    # אתחול אפליקציית טלגרם + Webhook
    try:
        await TelegramAppManager.start()
    except Exception as e:
        logger.error(f"Failed to start Telegram Application: {e}")
        # לא מפילים את השרת HTTP, אבל שומרים לוג

# הרצה מקומית
# =========================
if __name__ == "__main__":
    import uvicorn
    from datetime import datetime

    # בדיקת קונפיגורציה
    warnings = Config.validate()
    if warnings:
        print("⚠️ אזהרות קונפיגורציה:")
        for warning in warnings:
            print(f"  {warning}")

    port = int(os.getenv("PORT", "8080"))
    print(f"🚀 Starting SLHNET Bot on port {port}")
    
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        log_config=None
    )