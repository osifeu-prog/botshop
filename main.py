from telegram.ext import (
    MessageHandler,
    filters,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Application,
)
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)

import os
import json
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List
from decimal import Decimal, InvalidOperation
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware

from pydantic import BaseModel
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from db import (
    init_schema,
    get_approval_stats,
    get_monthly_payments,
    get_reserve_stats,
    log_payment,
    update_payment_status,
    has_approved_payment,
    get_pending_payments,
)
from slh_internal_wallets import (
    init_internal_wallet_schema,
    ensure_internal_wallet,
    get_wallet_overview,
    transfer_between_users,
    create_stake_position,
    get_user_stakes,
    mint_slh_from_payment,
)

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


# =========================
# קונפיגורציית לוגינג
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("slhnet_bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("slhnet")

# =========================
# FastAPI app
# =========================
app = FastAPI(
    title="SLHNET Gateway Bot",
    description="בוט קהילה ושער API עבור SLHNET",
    version="2.0.0",
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

# אתחול סכמת בסיס הנתונים (טבלאות + רזרבות 49%) + ארנקים פנימיים וסטייקינג
try:
    init_schema()
    init_internal_wallet_schema()
except Exception as e:
    logger.warning(f"init_schema or init_internal_wallet_schema failed: {e}")

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
# ניהול referral
# =========================
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
REF_FILE = DATA_DIR / "referrals.json"


def load_referrals() -> Dict[str, Any]:
    """טוען נתוני referrals עם הגנת שגיאות"""
    if not REF_FILE.exists():
        return {"users": {}, "statistics": {"total_users": 0}}

    try:
        with open(REF_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except (json.JSONDecodeError, Exception) as e:
        logger.error(f"Error loading referrals: {e}")
        return {"users": {}, "statistics": {"total_users": 0}}


def save_referrals(data: Dict[str, Any]) -> None:
    """שומר נתוני referrals עם הגנת שגיאות"""
    try:
        data["statistics"]["total_users"] = len(data["users"])
        with open(REF_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving referrals: {e}")


def register_referral(
    user_id: int,
    referrer_id: Optional[int] = None,
    username: Optional[str] = None,
    full_name: Optional[str] = None,
) -> bool:
    """רושם משתמש חדש עם referral + שומר קצת פרופיל בסיסי"""
    try:
        data = load_referrals()
        suid = str(user_id)

        if suid in data["users"]:
            # אם כבר קיים, נעדכן שם/יוזר אם חסרים
            existing = data["users"][suid]
            if username and not existing.get("username"):
                existing["username"] = username
            if full_name and not existing.get("full_name"):
                existing["full_name"] = full_name
            save_referrals(data)
            return False

        user_data = {
            "referrer": str(referrer_id) if referrer_id else None,
            "joined_at": datetime.now().isoformat(),
            "referral_count": 0,
            "username": username,
            "full_name": full_name,
        }

        data["users"][suid] = user_data

        if referrer_id:
            referrer_str = str(referrer_id)
            if referrer_str in data["users"]:
                data["users"][referrer_str]["referral_count"] = (
                    data["users"][referrer_str].get("referral_count", 0) + 1
                )

        save_referrals(data)
        logger.info(f"Registered new user {user_id} with referrer {referrer_id}")
        return True

    except Exception as e:
        logger.error(f"Error registering referral: {e}")
        return False


# =========================
# ניהול הודעות
# =========================
MESSAGES_FILE = BASE_DIR / "bot_messages_slhnet.txt"


def load_message_block(block_name: str, fallback: str = "") -> str:
    """
    טוען בלוק טקסט מהקובץ עם הגנות וטקסט ברירת מחדל
    """
    if not MESSAGES_FILE.exists():
        logger.warning(f"Messages file not found: {MESSAGES_FILE}")
        return fallback or "[שגיאה: קובץ הודעות לא נמצא]"

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
    START_IMAGE_PATH: str = os.getenv("START_IMAGE_PATH", "assets/start_banner.jpg")
    TON_WALLET_ADDRESS: str = os.getenv("TON_WALLET_ADDRESS", "")
    SUPPORT_GROUP_LINK: str = os.getenv("SUPPORT_GROUP_LINK", "")
    LOGS_GROUP_CHAT_ID: str = os.getenv(
        "LOGS_GROUP_CHAT_ID", ADMIN_ALERT_CHAT_ID or ""
    )
    MINT_ON_APPROVAL_SLH: str = os.getenv("MINT_ON_APPROVAL_SLH", "")

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
# Telegram Application (singleton)
# =========================
class TelegramAppManager:
    """מנהל אפליקציית הטלגרם"""

    _instance: Optional[Application] = None
    _initialized: bool = False
    _started: bool = False

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

        handlers = [
            # פקודות כניסה ומידע
            CommandHandler("start", start_command),
            CommandHandler("whoami", whoami_command),
            CommandHandler("stats", stats_command),
            CommandHandler("my_link", my_link_command),
            CommandHandler("my_referrals", my_referrals_command),
            # פקודות ניהול תשלומים
            CommandHandler("admin", admin_command),
            CommandHandler("pending", pending_command),
            CommandHandler("approve", approve_command),
            CommandHandler("reject", reject_command),
            CommandHandler("affiliates", affiliates_command),
            # ארנק פנימי וסטייקינג
            CommandHandler("wallet", wallet_command),
            CommandHandler("send_slh", send_slh_command),
            CommandHandler("stake", stake_command),
            CommandHandler("mystakes", mystakes_command),
            # Callback queries
            CallbackQueryHandler(callback_query_handler),
            # אישורי תשלום (תמונות / קבצים)
            MessageHandler(filters.PHOTO | filters.Document.ALL, payment_proof_handler),
            # טקסט חופשי + פקודות לא מוכרות
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
        cls.initialize_handlers()
        app_instance = cls.get_app()
        if not cls._started:
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
# utilities
# =========================
async def send_log_message(text: str) -> None:
    """שולח הודעת לוג עם הגנות"""
    if not Config.LOGS_GROUP_CHAT_ID:
        logger.warning("LOGS_GROUP_CHAT_ID not set; skipping log message")
        return

    try:
        app_instance = TelegramAppManager.get_app()
        await app_instance.bot.send_message(
            chat_id=int(Config.LOGS_GROUP_CHAT_ID), text=text
        )
    except Exception as e:
        logger.error(f"Failed to send log message: {e}")


def safe_get_url(url: str, fallback: str) -> str:
    """מחזיר URL עם הגנות"""
    return url if url and url.startswith(("http://", "https://")) else fallback


# ====== הודעות מפורטות לכל אמצעי תשלום ======

def base_upload_instructions() -> str:
    return (
        "לאחר שביצעת תשלום:\n"
        "1️⃣ שמור צילום מסך ברור של אישור התשלום (או קובץ PDF / מסמך מהבנק).\n"
        "2️⃣ חזור לצ׳אט עם הבוט.\n"
        "3️⃣ לחץ על *סיכת הקבצים* (או אייקון המצלמה) בטלגרם.\n"
        "4️⃣ בחר את צילום המסך / הקובץ ושלח כהודעה לבוט.\n\n"
        "המערכת תעביר את האישור אוטומטית לצוות הניהול.\n"
        "לאחר אישור – תקבל קישור לקבוצת העסקים + גישה לכל הכלים הדיגיטליים."
    )


def build_bank_instructions() -> str:
    return (
        "🏦 *תשלום בהעברה בנקאית*\n\n"
        "בנק הפועלים\n"
        "סניף כפר גנים (153)\n"
        "חשבון 73462\n"
        "המוטב: קאופמן צביקה\n\n"
        + base_upload_instructions()
    )


def build_paybox_instructions() -> str:
    if not Config.PAYBOX_URL:
        return "לא הוגדר קישור PayBox במערכת."
    return (
        "📲 *תשלום דרך PayBox*\n\n"
        f"היכנס לקישור:\n{Config.PAYBOX_URL}\n\n"
        "בצע תשלום בסך *39 ₪* לפי ההוראות באפליקציה.\n\n"
        + base_upload_instructions()
    )


def build_bit_instructions() -> str:
    if not Config.BIT_URL:
        return "לא הוגדר קישור Bit במערכת."
    return (
        "📲 *תשלום דרך Bit*\n\n"
        f"היכנס לקישור:\n{Config.BIT_URL}\n\n"
        "בצע תשלום בסך *39 ₪* לפי ההוראות.\n\n"
        + base_upload_instructions()
    )


def build_paypal_instructions() -> str:
    if not Config.PAYPAL_URL:
        return "לא הוגדר קישור PayPal במערכת."
    return (
        "🌍 *תשלום דרך PayPal*\n\n"
        f"היכנס לקישור:\n{Config.PAYPAL_URL}\n\n"
        "בצע תשלום בסך *39 ₪* במטבע המוצג.\n\n"
        + base_upload_instructions()
    )


def build_ton_instructions() -> str:
    if not Config.TON_WALLET_ADDRESS:
        return "לא הוגדר ארנק TON במערכת."
    return (
        "🔐 *תשלום בקריפטו – TON*\n\n"
        "שלח את הסכום המוסכם לארנק הבא:\n"
        f"`{Config.TON_WALLET_ADDRESS}`\n\n"
        "הכי טוב לצרף בהערות התשלום את השם שלך / טלפון, כדי שנזהה מהר.\n\n"
        + base_upload_instructions()
    )


def build_payment_overview() -> str:
    """טקסט כללי שמופיע לפני בחירת אמצעי התשלום"""
    return (
        "בחר את אמצעי התשלום המועדף עליך מתוך הכפתורים למטה.\n\n"
        "לאחר ביצוע התשלום – תתבקש לשלוח צילום מסך של האישור כאן לבוט, "
        "והאישור יעבור אוטומטית לצוות הניהול."
    )


# =========================
# handlers – לוגיקה עסקית
# =========================
async def send_start_screen(
    update: Update, context: ContextTypes.DEFAULT_TYPE, referrer: Optional[int] = None
) -> None:
    """מסך start ראשי: מה מקבלים, איך לשלם, כניסה לקבוצה, מידע למשקיעים ותמיכה."""
    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat:
        logger.error("No user or chat in update")
        return

    # רישום referral
    register_referral(
        user_id=user.id,
        referrer_id=referrer,
        username=user.username,
        full_name=user.full_name,
    )

    # טקסטים
    title = load_message_block("START_TITLE", "🚀 ברוך הבא ל-SLHNET!")
    body = load_message_block(
        "START_BODY",
        (
            "ברוך הבא לשער הדיגיטלי של קהילת SLHNET.\n"
            "כאן אתה מצטרף לקהילת עסקים, מקבל גישה לארנקים, חוזים חכמים, "
            "NFT וקבלת תשלומים – הכל סביב תשלום חד־פעמי של *39 ₪*."
        ),
    )

    # תמונת פתיחה אם קיימת
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

    # קישורים
    group_url = safe_get_url(
        Config.BUSINESS_GROUP_URL or Config.GROUP_STATIC_INVITE, Config.LANDING_URL
    )
    more_info_url = safe_get_url(Config.LANDING_URL, Config.LANDING_URL)
    support_url = safe_get_url(
        Config.SUPPORT_GROUP_LINK
        or Config.BUSINESS_GROUP_URL
        or Config.GROUP_STATIC_INVITE,
        Config.LANDING_URL,
    )

    # סטטוס תשלום
    has_paid = False
    try:
        has_paid = has_approved_payment(user.id)
    except Exception as e:
        logger.error(f"Error checking approved payment for user {user.id}: {e}")

    # תפריט ראשי – UX: קודם מה מקבלים, אח"כ איך לשלם, אח"כ כניסה
    keyboard: List[List[InlineKeyboardButton]] = []

    keyboard.append(
        [InlineKeyboardButton("ℹ️ מה אני מקבל?", callback_data="info_benefits")]
    )
    keyboard.append(
        [InlineKeyboardButton("📤 איך לשלם ולשלוח אישור", callback_data="menu_payments")]
    )

    if has_paid:
        keyboard.append(
            [InlineKeyboardButton("👥 כניסה לקבוצת העסקים", url=group_url)]
        )

    keyboard.append(
        [InlineKeyboardButton("📈 מידע למשקיעים", callback_data="open_investor")]
    )
    keyboard.append([InlineKeyboardButton("🔗 דף מידע מלא", url=more_info_url)])
    keyboard.append(
        [InlineKeyboardButton("🆘 תמיכה / צור קשר", url=support_url)]
    )

    reply_markup = InlineKeyboardMarkup(keyboard)
    await chat.send_message(text=body, reply_markup=reply_markup, parse_mode="Markdown")

    # לוג – כל משתמש שמפעיל את הבוט
    log_text = (
        "📥 משתמש חדש הפעיל את הבוט\n"
        f"👤 User ID: {user.id}\n"
        f"📛 Username: @{user.username or 'לא מוגדר'}\n"
        f"🔰 שם: {user.full_name}\n"
        f"🔄 Referrer: {referrer or 'לא צוין'}\n"
        f"💳 סטטוס תשלום מאושר: {'כן' if has_paid else 'לא'}"
    )
    await send_log_message(log_text)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """פקודת /start עם תמיכה ב-referral"""
    referrer = None
    if context.args:
        try:
            referrer = int(context.args[0])
            logger.info(f"Start command with referrer: {referrer}")
        except (ValueError, TypeError):
            logger.warning(f"Invalid referrer ID: {context.args[0]}")

    await send_start_screen(update, context, referrer=referrer)


async def my_link_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """מחזיר למשתמש קישור הזמנה אישי להפצה – /my_link"""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    try:
        me = await context.bot.get_me()
        bot_username = me.username or os.getenv("BOT_USERNAME", "Buy_My_Shop_bot")
    except Exception as e:
        logger.error(f"get_me failed in /my_link: {e}")
        bot_username = os.getenv("BOT_USERNAME", "Buy_My_Shop_bot")

    invite_link = f"https://t.me/{bot_username}?start={user.id}"

    text = (
        "🔗 *קישור ההזמנה האישי שלך:*\n\n"
        f"`{invite_link}`\n\n"
        "שלח את הקישור הזה לחברים / לקוחות.\n"
        "כל מי שייכנס דרכו ויצטרף בתשלום – ייספר כהפניה שלך.\n"
        "תוכל לראות סטטיסטיקות בפקודה /my_referrals."
    )
    await chat.send_message(text=text, parse_mode="Markdown")


async def my_referrals_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """מציג למשתמש את ההפניות האישיות שלו – /my_referrals"""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    data = load_referrals()
    users = data.get("users", {})
    my_id_str = str(user.id)

    referred_ids: List[str] = [
        uid for uid, u in users.items() if u.get("referrer") == my_id_str
    ]

    total_referrals = len(referred_ids)
    paid_referrals = 0
    paid_ids: List[str] = []

    # נבדוק מי מהם כבר עם תשלום מאושר
    for uid in referred_ids:
        try:
            if has_approved_payment(int(uid)):
                paid_referrals += 1
                paid_ids.append(uid)
        except Exception:
            continue

    if total_referrals == 0:
        text = (
            "עדיין לא רשומות הפניות על שמך.\n"
            "השתמש ב-/my_link כדי לקבל קישור אישי ולהתחיל להזמין אנשים."
        )
        await chat.send_message(text)
        return

    lines = [
        "👥 *הפניות האישיות שלך:*\n",
        f"סה״כ אנשים שנרשמו דרכך: *{total_referrals}*",
        f"מתוכם עם תשלום מאושר: *{paid_referrals}*",
        "",
    ]

    # נציג עד 20 ראשונים
    for uid in referred_ids[:20]:
        udata = users.get(uid, {})
        uname = udata.get("username")
        fname = udata.get("full_name")
        paid_mark = "✅" if uid in paid_ids else "⏳"
        label = uname or fname or f"User {uid}"
        lines.append(f"{paid_mark} {label} (ID: {uid})")

    if len(referred_ids) > 20:
        lines.append(f"\n… ועוד {len(referred_ids) - 20} הפניות.")

    await chat.send_message("\n".join(lines), parse_mode="Markdown")


async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """פקודת /whoami משופרת"""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    referrals_data = load_referrals()
    user_ref_data = referrals_data["users"].get(str(user.id), {})

    text = (
        "👤 **פרטי המשתמש שלך:**\n"
        f"🆔 ID: `{user.id}`\n"
        f"📛 שם משתמש: @{user.username or 'לא מוגדר'}\n"
        f"🔰 שם מלא: {user.full_name}\n"
        f"🔄 מספר הפניות: {user_ref_data.get('referral_count', 0)}\n"
        f"📅 הצטרף: {user_ref_data.get('joined_at', 'לא ידוע')}"
    )

    await chat.send_message(text=text, parse_mode="Markdown")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """סטטיסטיקות קהילה בסיסיות – /stats"""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    referrals_data = load_referrals()
    stats = referrals_data.get("statistics", {})

    text = (
        "📊 **סטטיסטיקות קהילה:**\n"
        f"👥 סה״כ משתמשים: {stats.get("total_users", 0)}\n"
        f"📈 משתמשים פעילים: {len(referrals_data.get('users', {}))}\n"
        "🔄 הפניות כוללות: "
        f"{sum(u.get('referral_count', 0) for u in referrals_data.get('users', {}).values())}"
    )

    await chat.send_message(text=text, parse_mode="Markdown")


# =========================
# פקודות ניהול ותשלומים – 39 ₪
# =========================
async def payment_proof_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """קבלת צילום/קובץ כאישור תשלום והעברת הלוג לקבוצת הניהול."""
    user = update.effective_user
    chat = update.effective_chat
    message = update.message

    if not user or not chat or not message:
        return

    if chat.type != "private":
        return

    caption = message.caption or ""
    text_lower = caption.lower()

    if "paybox" in text_lower or "פייבוקס" in text_lower:
        pay_method = "paybox"
    elif "paypal" in text_lower or "פייפאל" in text_lower:
        pay_method = "paypal"
    elif "bit" in text_lower or "ביט" in text_lower:
        pay_method = "bit"
    elif "העברה" in caption or "bank" in text_lower or "בנק" in text_lower:
        pay_method = "bank-transfer"
    else:
        pay_method = "screenshot"

    try:
        log_payment(user.id, user.username, pay_method)
    except Exception as e:
        logger.error(f"Error logging payment for user {user.id}: {e}")

    if Config.LOGS_GROUP_CHAT_ID:
        try:
            admin_chat_id = int(Config.LOGS_GROUP_CHAT_ID)
            await context.bot.copy_message(
                chat_id=admin_chat_id,
                from_chat_id=chat.id,
                message_id=message.message_id,
            )

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ אישור תשלום", callback_data=f"approve:{user.id}"
                        ),
                        InlineKeyboardButton(
                            "❌ דחיית תשלום", callback_data=f"reject:{user.id}"
                        ),
                    ]
                ]
            )

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
                chat_id=admin_chat_id, text=admin_text, reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Error sending payment log to admin group: {e}")

    await chat.send_message(
        "📥 קיבלנו את אישור התשלום שלך!\n"
        "ההודעה הועברה לצוות הניהול. לאחר אישור, ישלח אליך קישור לקבוצת העסקים."
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """פאנל ניהול בסיסי למנהלים בלבד – /admin"""
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
        " - /affiliates – סקירת מפנים מובילים",
    ]
    await chat.send_message("\n".join(text_lines), parse_mode="Markdown")


async def affiliates_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """סקירת מפנים מובילים – למנהלים בלבד – /affiliates"""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    if not is_admin(user.id):
        await chat.send_message("❌ הפקודה /affiliates מיועדת למנהלי המערכת בלבד.")
        return

    data = load_referrals()
    users = data.get("users", {})

    # ניקח רק מי שיש להם לפחות הפניה אחת
    referrers = [
        (uid, udata)
        for uid, udata in users.items()
        if udata.get("referral_count", 0) > 0
    ]

    if not referrers:
        await chat.send_message("עדיין אין מפנים פעילים במערכת.")
        return

    # מיין מהכי הרבה הפניות לפחות
    referrers.sort(key=lambda t: t[1].get("referral_count", 0), reverse=True)

    lines = ["🏅 *מפנים מובילים במערכת:*\n"]
    for uid, udata in referrers[:30]:
        count = udata.get("referral_count", 0)
        uname = udata.get("username")
        fname = udata.get("full_name")
        label = uname or fname or f"User {uid}"
        lines.append(f"• {label} (ID: {uid}) – {count} הפניות")

    await chat.send_message("\n".join(lines), parse_mode="Markdown")


async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """רשימת תשלומים ממתינים – למנהלים בלבד – /pending"""
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
        lines.append(
            f"• user_id={p['user_id']} | username=@{p['username'] or 'לא ידוע'} | "
            f"שיטה={p['pay_method']} | id={p['id']}"
        )

    await chat.send_message("\n".join(lines), parse_mode="Markdown")


async def _maybe_mint_on_approval(user_id: int) -> None:
    """אופציונלי: הנפקת SLH פנימי אוטומטית בעת אישור תשלום (אם מוגדר בקונפיג)."""
    if not Config.MINT_ON_APPROVAL_SLH:
        return
    try:
        amount = Decimal(Config.MINT_ON_APPROVAL_SLH.replace(",", "."))
    except InvalidOperation:
        logger.error("MINT_ON_APPROVAL_SLH not a valid decimal")
        return

    try:
        ok, msg = mint_slh_from_payment(user_id=user_id, amount_slh=amount)
        if not ok:
            logger.error(f"mint_slh_from_payment failed for {user_id}: {msg}")
    except Exception as e:
        logger.error(f"mint_slh_from_payment exception for {user_id}: {e}")


async def _send_onboarding_after_approval(
    bot, user_id: int, group_url: str
) -> None:
    """הודעת אונבורדינג מסודרת אחרי אישור תשלום."""
    onboarding_text = load_message_block(
        "ONBOARDING_AFTER_APPROVAL",
        (
            "🎉 *ברוך הבא לקהילת SLHNET!*\n\n"
            "הצטרפת רשמית דרך שער ה־39 ₪. מכאן נתקדם בשלושה צעדים פשוטים:\n\n"
            "1️⃣ היכנס לקבוצת העסקים: \n"
            f"{group_url}\n\n"
            "2️⃣ הצג את עצמך בקבוצה – מי אתה, מה העסק שלך, ואיזה ערך אתה מביא.\n\n"
            "3️⃣ שמור את הקישור האישי שלך להפניות נוספות דרך הפקודה /my_link בבוט.\n\n"
            "בכל שאלה, אפשר לפנות לתמיכה דרך הבוט או בקבוצה עצמה 🙌"
        ),
    )
    try:
        await bot.send_message(chat_id=user_id, text=onboarding_text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Failed to send onboarding message to {user_id}: {e}")


async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """אישור תשלום ידני לפי user_id – למנהלים בלבד – /approve"""
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

    # אופציונלי – הנפקת SLH פנימי
    await _maybe_mint_on_approval(target_id)

    group_url = safe_get_url(
        Config.BUSINESS_GROUP_URL or Config.GROUP_STATIC_INVITE, Config.LANDING_URL
    )

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
        # הודעת אונבורדינג מסודרת
        await _send_onboarding_after_approval(context.bot, target_id, group_url)
    except Exception as e:
        logger.error(f"Error sending approval/onboarding message to user {target_id}: {e}")

    await chat.send_message(
        f"✅ התשלום של המשתמש {target_id} אושר ונשלחו אליו קישורים והסבר התחלה."
    )


async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """דחיית תשלום ידנית לפי user_id – למנהלים בלבד – /reject"""
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
        logger.error(f"Error updating payment status for {target_id}: {e}")
        await chat.send_message("❌ שגיאה בעדכון סטטוס התשלום.")
        return

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                "❌ התשלום שלך נדחה.\n"
                f"סיבה: {reason}\n\n"
                "אם לדעתך מדובר בטעות, ניתן לפנות לתמיכה."
            ),
        )
    except Exception as e:
        logger.error(f"Error sending rejection message to user {target_id}: {e}")

    await chat.send_message(
        f"🚫 התשלום של המשתמש {target_id} נדחה ונשלחה לו הודעה."
    )


# =========================
# ארנק פנימי וסטייקינג – פקודות טלגרם
# =========================
STAKING_DEFAULT_APY = Decimal(os.getenv("STAKING_DEFAULT_APY", "20"))
STAKING_DEFAULT_DAYS = int(os.getenv("STAKING_DEFAULT_DAYS", "90"))


async def wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """מציג למשתמש את מצב הארנק הפנימי שלו – /wallet"""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    try:
        ensure_internal_wallet(user.id, user.username or None)
        wallet = get_wallet_overview(user.id)
        stakes = get_user_stakes(user.id) or []
    except Exception as e:
        logger.error(f"wallet_command error: {e}")
        await chat.send_message(
            "❌ לא הצלחתי לטעון את הארנק שלך כרגע. נסה שוב מאוחר יותר."
        )
        return

    if not wallet:
        await chat.send_message("❌ לא הצלחתי לטעון את הארנק שלך כרגע.")
        return

    balance = wallet.get("balance_slh", Decimal("0"))
    wallet_id = wallet.get("wallet_id", "?")

    stakes_lines: List[str] = []
    total_staked = Decimal("0")
    for s in stakes:
        amt = Decimal(str(s.get("amount_slh") or "0"))
        total_staked += amt
        pos_id = s.get("id", "?")
        apy = s.get("apy", "?")
        lock_days = s.get("lock_days", "?")
        stakes_lines.append(
            f"• #{pos_id}: {amt} SLH | APY {apy}% | {lock_days} ימים נעילה"
        )

    if not stakes_lines:
        stakes_text = "אין לך עדיין עמדות סטייקינג פעילות."
    else:
        stakes_text = "\n".join(stakes_lines)

    msg = (
        "💼 *ארנק SLH פנימי*\n\n"
        f"🆔 ID ארנק: `{wallet_id}`\n"
        f"💰 יתרה זמינה: *{balance}* SLH\n"
        f"🔒 סה״כ בסטייקינג: {total_staked} SLH\n\n"
        "כדי לפתוח סטייקינג חדש:\n"
        "*/stake <סכום_SLH> <ימי_נעילה>* לדוגמה:\n"
        "`/stake 100 30` – סטייקינג על 100 SLH ל-30 ימים.\n\n"
        "מצבי סטייקינג:\n"
        f"{stakes_text}"
    )

    await chat.send_message(text=msg, parse_mode="Markdown")


async def send_slh_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """מעביר SLH פנימי למשתמש אחר: /send_slh <amount> <@username|user_id>"""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    if len(context.args) < 2:
        await chat.send_message("שימוש: /send_slh <amount> <@username|user_id>")
        return

    amount_str, target = context.args[0], context.args[1]
    try:
        amount = Decimal(amount_str.replace(",", "."))
    except InvalidOperation:
        await chat.send_message("סכום לא תקין. נסה שוב עם מספר תקין.")
        return

    if target.startswith("@"):
        await chat.send_message(
            "בגרסה הנוכחית יש להשתמש ב-user_id מספרי, לא בשם משתמש. "
            "קבל את ה-ID מהפקודה /whoami אצל הצד השני."
        )
        return

    try:
        to_user_id = int(target)
    except ValueError:
        await chat.send_message("user_id חייב להיות מספרי.")
        return

    ok, msg = transfer_between_users(user.id, to_user_id, amount)
    if not ok:
        await chat.send_message(f"❌ העברה נכשלה: {msg}")
        return

    await chat.send_message(f"✅ הועברו {amount} SLH פנימיים למשתמש {to_user_id}.")


async def stake_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """פותח סטייקינג בסיסי: /stake <amount> [days]"""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    if not context.args:
        await chat.send_message(
            "שימוש: /stake <amount> [days]. ברירת מחדל ימים: "
            f"{STAKING_DEFAULT_DAYS}, APY: {STAKING_DEFAULT_APY}%."
        )
        return

    amount_str = context.args[0]
    days = STAKING_DEFAULT_DAYS
    if len(context.args) >= 2:
        try:
            days = int(context.args[1])
        except ValueError:
            await chat.send_message("ערך ימים לא תקין, משתמש בברירת מחדל.")

    try:
        amount = Decimal(amount_str.replace(",", "."))
    except InvalidOperation:
        await chat.send_message("סכום לא תקין. נסה שוב עם מספר תקין.")
        return

    ok, msg = create_stake_position(user.id, amount, STAKING_DEFAULT_APY, days)
    if not ok:
        await chat.send_message(f"❌ סטייקינג נכשל: {msg}")
        return

    await chat.send_message(
        f"✅ פתחת סטייקינג על {amount} SLH ל-{days} ימים.\n"
        f"APY נוכחי: {STAKING_DEFAULT_APY}%."
    )


async def mystakes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """מציג עמדות סטייקינג פעילות/סגורות – /mystakes"""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    stakes = get_user_stakes(user.id)
    if not stakes:
        await chat.send_message("אין לך עדיין עמדות סטייקינג.")
        return

    lines = ["📊 *עמדות הסטייקינג שלך:*\n"]
    for st in stakes:
        status = st.get("status", "unknown")
        amount = st.get("amount_slh", Decimal("0"))
        apy = st.get("apy", Decimal("0"))
        lock_days = st.get("lock_days", 0)
        started = st.get("started_at")
        lines.append(
            f"• {amount} SLH | {apy}% | {lock_days} ימים | סטטוס: {status} | התחלה: {started}"
        )

    await chat.send_message("\n".join(lines), parse_mode="Markdown")


# =========================
# Callback queries
# =========================
async def handle_investor_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """מטפל בכפתור מידע למשקיעים"""
    query = update.callback_query
    investor_text = load_message_block(
        "INVESTOR_INFO",
        (
            "📈 **מידע למשקיעים**\n\n"
            "מערכת SLHNET מחברת בין טלגרם, חוזים חכמים על Binance Smart Chain, "
            "קבלות דיגיטליות ו-NFT, כך שכל עסקה מתועדת וניתנת למעקב.\n\n"
            "ניתן להצטרף כשותף, להחזיק טוקן SLH ולקבל חלק מהתנועה במערכת."
        ),
    )

    keyboard = [[InlineKeyboardButton("🔙 חזרה לתפריט הראשי", callback_data="back_to_main")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        text=investor_text, reply_markup=reply_markup, parse_mode="Markdown"
    )


async def handle_payment_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """תפריט אמצעי תשלום – אחרי לחיצה על 'איך לשלם ולשלוח אישור'"""
    query = update.callback_query
    text = build_payment_overview()

    support_url = safe_get_url(
        Config.SUPPORT_GROUP_LINK
        or Config.BUSINESS_GROUP_URL
        or Config.GROUP_STATIC_INVITE,
        Config.LANDING_URL,
    )

    keyboard: List[List[InlineKeyboardButton]] = []

    # תמיד יש העברה בנקאית
    keyboard.append(
        [InlineKeyboardButton("🏦 העברה בנקאית", callback_data="pay_bank")]
    )

    if Config.PAYBOX_URL:
        keyboard.append(
            [InlineKeyboardButton("📲 תשלום ב-PayBox", callback_data="pay_paybox")]
        )
    if Config.BIT_URL:
        keyboard.append(
            [InlineKeyboardButton("📲 תשלום ב-Bit", callback_data="pay_bit")]
        )
    if Config.PAYPAL_URL:
        keyboard.append(
            [InlineKeyboardButton("🌍 תשלום ב-PayPal", callback_data="pay_paypal")]
        )
    if Config.TON_WALLET_ADDRESS:
        keyboard.append(
            [InlineKeyboardButton("🔐 תשלום בקריפטו (TON)", callback_data="pay_ton")]
        )

    keyboard.append(
        [
            InlineKeyboardButton(
                "📤 איך לשלוח צילום אישור", callback_data="pay_upload_help"
            )
        ]
    )

    keyboard.append(
        [InlineKeyboardButton("🔙 חזרה לתפריט הראשי", callback_data="back_to_main")]
    )
    keyboard.append(
        [InlineKeyboardButton("🆘 תמיכה / צור קשר", url=support_url)]
    )

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        text=text, reply_markup=reply_markup, parse_mode="Markdown"
    )


async def handle_payment_method_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, method: str
) -> None:
    """מסכי הוראות נפרדים לכל אמצעי תשלום"""
    query = update.callback_query

    if method == "bank":
        text = build_bank_instructions()
    elif method == "paybox":
        text = build_paybox_instructions()
    elif method == "bit":
        text = build_bit_instructions()
    elif method == "paypal":
        text = build_paypal_instructions()
    elif method == "ton":
        text = build_ton_instructions()
    elif method == "upload_help":
        text = base_upload_instructions()
    else:
        text = "אמצעי תשלום לא מוכר."

    keyboard = [
        [
            InlineKeyboardButton(
                "📤 איך לשלוח צילום אישור", callback_data="pay_upload_help"
            )
        ],
        [InlineKeyboardButton("🔙 חזרה לתפריט תשלומים", callback_data="menu_payments")],
        [InlineKeyboardButton("🔙 חזרה לתפריט הראשי", callback_data="back_to_main")],
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        text=text, reply_markup=reply_markup, parse_mode="Markdown"
    )


async def handle_send_proof_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """שמירה לאחור compatibility – מפנה לתפריט התשלומים"""
    await handle_payment_menu_callback(update, context)


async def handle_benefits_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """מסביר ללקוח מה הוא מקבל מהמערכת"""
    query = update.callback_query
    benefits_text = load_message_block(
        "BENEFITS_INFO",
        (
            "🎁 **מה מקבלים בתשלום 39 ₪?**\n\n"
            "• גישה לקבוצת עסקים חכמה בטלגרם עם תכנים, הדרכות וקהילה פעילה.\n"
            "• פתיחה וחיבור של ארנק SLH על רשת Binance Smart Chain (BSC).\n"
            "• אפשרות לקבל תשלומים דיגיטליים ועמלות הפנייה דרך המערכת.\n"
            "• חיבור לחוזים חכמים, קבלות דיגיטליות ו-NFT שמייצגים עסקאות ושערי כניסה.\n"
            "• בסיס לעתיד – סטייקינג, חסכונות והשקעות מתקדמות בתוך אקו־סיסטם SLHNET.\n\n"
            "אחרי התשלום ושליחת האישור – אתה מקבל קישור לקבוצה + סט כלים דיגיטליים להתחלה."
        ),
    )

    keyboard = [[InlineKeyboardButton("🔙 חזרה לתפריט הראשי", callback_data="back_to_main")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        text=benefits_text, reply_markup=reply_markup, parse_mode="Markdown"
    )


async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """מטפל ב-callback queries של תפריט ההתחלה והאדמין"""
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    await query.answer()

    if data == "open_investor":
        await handle_investor_callback(update, context)
    elif data in ("send_proof", "send_payment_instructions", "menu_payments"):
        await handle_payment_menu_callback(update, context)
    elif data == "info_benefits":
        await handle_benefits_callback(update, context)
    elif data == "back_to_main":
        await send_start_screen(update, context)
    elif data == "pay_bank":
        await handle_payment_method_callback(update, context, "bank")
    elif data == "pay_paybox":
        await handle_payment_method_callback(update, context, "paybox")
    elif data == "pay_bit":
        await handle_payment_method_callback(update, context, "bit")
    elif data == "pay_paypal":
        await handle_payment_method_callback(update, context, "paypal")
    elif data == "pay_ton":
        await handle_payment_method_callback(update, context, "ton")
    elif data == "pay_upload_help":
        await handle_payment_method_callback(update, context, "upload_help")
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

        await _maybe_mint_on_approval(target_id)

        group_url = safe_get_url(
            Config.BUSINESS_GROUP_URL or Config.GROUP_STATIC_INVITE,
            Config.LANDING_URL,
        )
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
            await _send_onboarding_after_approval(context.bot, target_id, group_url)
        except Exception as e:
            logger.error(f"Error sending approval message to user {target_id}: {e}")

        await query.edit_message_text(
            f"✅ התשלום של המשתמש {target_id} אושר ונשלחו אליו קישורים והסבר התחלה."
        )
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
            logger.error(
                f"Error updating payment status (reject) for {target_id}: {e}"
            )
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

        await query.edit_message_text(
            f"🚫 התשלום של המשתמש {target_id} נדחה ונשלחה לו הודעה."
        )
    else:
        await query.edit_message_text("❌ פעולה לא מוכרת.")


# =========================
# הודעות טקסט ופקודות לא מוכרות
# =========================
async def echo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """מטפל בהודעות טקסט רגילות"""
    user = update.effective_user
    text = update.message.text if update.message else ""
    logger.info(f"Message from {user.id if user else '?'}: {text}")

    response = load_message_block(
        "ECHO_RESPONSE",
        "✅ תודה על ההודעה! אנחנו כאן כדי לעזור.\n"
        "השתמש ב-/start כדי לראות את התפריט הראשי.",
    )

    await update.message.reply_text(response)


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """מטפל בפקודות לא מוכרות"""
    await update.message.reply_text(
        "❓ פקודה לא מוכרת. השתמש ב-/start כדי לראות את התפריט הזמין."
    )


# =========================
# Routes של FastAPI
# =========================
@app.get("/api/metrics/finance")
async def finance_metrics():
    """סטטוס כספי כולל – הכנסות, רזרבות, נטו ואישורים."""
    reserve_stats = get_reserve_stats() or {}
    approval_stats = get_approval_stats() or {}

    return {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "reserve": reserve_stats,
        "approvals": approval_stats,
    }


@app.get("/api/metrics/referrals")
async def referrals_metrics():
    """סטטוס רשת הפניות – לצורך דשבורד."""
    data = load_referrals()
    users = data.get("users", {})
    stats = data.get("statistics", {})
    total_users = stats.get("total_users", len(users))
    total_referrals = sum(u.get("referral_count", 0) for u in users.values())
    referrers = sum(1 for u in users.values() if u.get("referral_count", 0) > 0)

    return {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "total_users": total_users,
        "total_referrals": total_referrals,
        "active_referrers": referrers,
    }


@app.get("/metrics")
async def metrics():
    """Prometheus scrape endpoint for SLHNET metrics."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Endpoint לבריאות המערכת"""
    return HealthResponse(
        status="ok",
        service="slhnet-telegram-gateway",
        timestamp=datetime.now().isoformat(),
        version="2.0.0",
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
            "business_group_url": safe_get_url(
                Config.BUSINESS_GROUP_URL, "https://slh-nft.com"
            ),
        },
    )


@app.post("/webhook")
async def telegram_webhook(update: TelegramWebhookUpdate):
    """Webhook endpoint עם הגנות"""
    try:
        TelegramAppManager.initialize_handlers()
        app_instance = TelegramAppManager.get_app()

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
    try:
        init_internal_wallet_schema()
    except Exception as e:
        logger.error(f"init_internal_wallet_schema failed: {e}")

    warnings = Config.validate()
    for warning in warnings:
        logger.warning(warning)
    if warnings:
        await send_log_message("⚠️ **אזהרות אתחול:**\n" + "\n".join(warnings))

    try:
        await TelegramAppManager.start()
    except Exception as e:
        logger.error(f"Failed to start Telegram Application: {e}")


# =========================
# הרצה מקומית
# =========================
if __name__ == "__main__":
    import uvicorn

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
        log_config=None,
    )
