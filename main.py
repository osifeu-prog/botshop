from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
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

# ==== יבואי DB / ארנקים פנימיים ====
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

# ==== רואטרים חיצוניים (עטופים ב-try למקרה שלא קיימים) ====
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

# סטטיק וטמפלטס
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

# רואטרים של API
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
# ניהול referral + פרופילים
# =========================
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
REF_FILE = DATA_DIR / "referrals.json"
PROFILES_FILE = DATA_DIR / "profiles.json"

# מצב זמני לשאלות פרופיל (אזור אישי)
PROFILE_SESSIONS: Dict[int, Dict[str, Any]] = {}
PROFILE_QUESTIONS = [
    "איך קוראים לך / שם העסק שלך?",
    "מה אתה עושה / מה אתה מציע בקהילה (תחום פעילות / התמחות)?",
    "איך הכי טוב ליצור איתך קשר? (טלפון / טלגרם / אתר / מייל)",
]


def load_referrals() -> Dict[str, Any]:
    """טוען נתוני referrals עם הגנת שגיאות"""
    if not REF_FILE.exists():
        return {"users": {}, "statistics": {"total_users": 0}}

    try:
        with open(REF_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "users" not in data:
            data["users"] = {}
        if "statistics" not in data:
            data["statistics"] = {"total_users": len(data["users"])}
        return data
    except (json.JSONDecodeError, Exception) as e:
        logger.error(f"Error loading referrals: {e}")
        return {"users": {}, "statistics": {"total_users": 0}}


def save_referrals(data: Dict[str, Any]) -> None:
    """שומר נתוני referrals עם הגנת שגיאות"""
    try:
        data.setdefault("users", {})
        data.setdefault("statistics", {})
        data["statistics"]["total_users"] = len(data["users"])

        with open(REF_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving referrals: {e}")


def register_referral(user_id: int, referrer_id: Optional[int] = None) -> bool:
    """רושם משתמש חדש עם referral (אם עדיין לא קיים)"""
    try:
        data = load_referrals()
        suid = str(user_id)

        if suid in data["users"]:
            return False  # כבר רשום

        user_data = {
            "referrer": str(referrer_id) if referrer_id else None,
            "joined_at": datetime.now().isoformat(),
            "referral_count": 0,
        }

        data["users"][suid] = user_data

        # עדכון סטטיסטיקה של המפנה אם קיים
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


def build_referral_link(user_id: int) -> str:
    """יוצר קישור הפניה אישי לבוט: https://t.me/<bot>?start=<user_id>"""
    bot_username = os.getenv("BOT_USERNAME", "Buy_My_Shop_bot").lstrip("@")
    return f"https://t.me/{bot_username}?start={user_id}"


def load_profiles() -> Dict[str, Any]:
    """טוען פרופילים מאזור אישי"""
    if not PROFILES_FILE.exists():
        return {"profiles": {}}
    try:
        with open(PROFILES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "profiles" not in data:
            data["profiles"] = {}
        return data
    except (json.JSONDecodeError, Exception) as e:
        logger.error(f"Error loading profiles: {e}")
        return {"profiles": {}}


def save_profiles(data: Dict[str, Any]) -> None:
    """שומר פרופילים לאזור אישי"""
    try:
        data.setdefault("profiles", {})
        with open(PROFILES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving profiles: {e}")


# =========================
# ניהול הודעות (בלוקים מטקסט)
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
# מודלים של Webhook + Health
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
    LOGS_GROUP_CHAT_ID: str = os.getenv(
        "LOGS_GROUP_CHAT_ID", ADMIN_ALERT_CHAT_ID or ""
    )
    SUPPORT_GROUP_LINK: str = os.getenv(
        "SUPPORT_GROUP_LINK", os.getenv("SUPPORT_GROUP_URL", "")
    )

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
# Telegram Application Manager
# =========================
class TelegramAppManager:
    """מנהל אפליקציית הטלגרם (Singleton)"""

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

            # פקודות הפניות / לינק אישי
            CommandHandler("my_link", my_link_command),
            CommandHandler("my_referrals", my_referrals_command),

            # תפריט חברים / אזור אישי
            CommandHandler("member", member_command),
            CommandHandler("my_card", my_card_command),
            CommandHandler("profile", my_card_command),

            # פקודות ניהול תשלומים
            CommandHandler("admin", admin_command),
            CommandHandler("pending", pending_command),
            CommandHandler("approve", approve_command),
            CommandHandler("reject", reject_command),

            # ארנק פנימי וסטייקינג
            CommandHandler("wallet", wallet_command),
            CommandHandler("send_slh", send_slh_command),
            CommandHandler("stake", stake_command),
            CommandHandler("mystakes", mystakes_command),

            # Callback queries (תפריטים וכפתורים)
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
# Utilities
# =========================
async def send_log_message(text: str) -> None:
    """שולח הודעת לוג לקבוצת לוגים/אדמין"""
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


def build_payment_instructions() -> str:
    """טקסט כללי על אמצעי תשלום"""
    bank_details = (
        "🏦 *העברה בנקאית:*\n"
        "בנק הפועלים\n"
        "סניף כפר גנים (153)\n"
        "חשבון 73462\n"
        "המוטב: קאופמן צביקה\n\n"
    )

    parts = [bank_details]

    if Config.PAYBOX_URL:
        parts.append(f"📲 *PayBox*: [לינק לתשלום]({Config.PAYBOX_URL})\n")
    if Config.BIT_URL:
        parts.append(f"📲 *Bit*: [לינק לתשלום]({Config.BIT_URL})\n")
    if Config.PAYPAL_URL:
        parts.append(f"🌍 *PayPal*: [לינק לתשלום]({Config.PAYPAL_URL})\n")
    if Config.TON_WALLET_ADDRESS:
        parts.append(
            f"🔐 *ארנק TON*: `{Config.TON_WALLET_ADDRESS}`\n"
        )

    footer = (
        "\nלאחר התשלום, שלח צילום מסך של האישור כאן בבוט, "
        "והמערכת תעביר אותו אוטומטית לאישור אצלנו.\n"
        "אחרי האישור תקבל קישור לקבוצת העסקים + גישה לכל הכלים הדיגיטליים."
    )

    parts.append(footer)
    return "".join(parts)


def build_payment_method_text(method: str) -> str:
    """טקסט מפורט לכל אמצעי תשלום + הוראות צילום ושליחה"""

    base_footer = (
        "\nלאחר ביצוע התשלום:\n"
        "1️⃣ שמור צילום מסך ברור של אישור התשלום (או קובץ PDF / מסמך מהבנק).\n"
        "2️⃣ שלח את צילום המסך כאן בצ׳אט עם הבוט.\n"
        "3️⃣ המערכת תעביר את האישור לקבוצת הניהול, ולאחר האישור תקבל קישור לקבוצת העסקים.\n"
    )

    if method == "bank":
        details = (
            "🏦 *תשלום בהעברה בנקאית*\n\n"
            "בנק הפועלים\n"
            "סניף כפר גנים (153)\n"
            "חשבון 73462\n"
            "המוטב: קאופמן צביקה\n"
        )
    elif method == "paybox":
        details = (
            "📲 *תשלום דרך PayBox*\n\n"
            f"השתמש בקישור הבא לביצוע תשלום 39 ₪:\n{Config.PAYBOX_URL}\n"
        )
    elif method == "bit":
        details = (
            "📲 *תשלום דרך Bit*\n\n"
            f"השתמש בקישור הבא לביצוע תשלום 39 ₪:\n{Config.BIT_URL}\n"
        )
    elif method == "paypal":
        details = (
            "🌍 *תשלום דרך PayPal*\n\n"
            f"השתמש בקישור הבא לביצוע תשלום 39 ₪:\n{Config.PAYPAL_URL}\n"
        )
    elif method == "ton":
        details = (
            "🔐 *תשלום בקריפטו – ארנק TON*\n\n"
            f"שלח 39 ₪ (או שוויו בקריפטו) אל הכתובת:\n`{Config.TON_WALLET_ADDRESS}`\n"
        )
    else:
        details = "אמצעי תשלום לא מוכר."

    return details + "\n" + base_footer


# =========================
# Handlers – START / מידע
# =========================
async def send_start_screen(
    update: Update, context: ContextTypes.DEFAULT_TYPE, referrer: Optional[int] = None
) -> None:
    """מסך פתיחה: מה מקבלים + איך לשלם"""
    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat:
        logger.error("No user or chat in update")
        return

    # רישום referral (גם אם referrer=None)
    register_referral(user.id, referrer)

    # כותרת וטקסט גוף
    title = load_message_block("START_TITLE", "🚀 ברוך הבא ל-SLHNET!")
    body = load_message_block(
        "START_BODY",
        (
            "ברוך הבא לשער הדיגיטלי של קהילת SLHNET.\n"
            "כאן אתה מצטרף לקהילת עסקים, מקבל גישה לארנקים, חוזים חכמים, "
            "NFT וקבלת תשלומים – הכל סביב תשלום חד־פעמי של *39 ₪*."
        ),
    )

    # שליחת תמונה / טקסט
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

    # בדיקה אם המשתמש כבר אושר תשלום
    has_paid = False
    try:
        has_paid = has_approved_payment(user.id)
    except Exception as e:
        logger.error(f"Error checking approved payment for user {user.id}: {e}")

    group_url = safe_get_url(
        Config.BUSINESS_GROUP_URL or Config.GROUP_STATIC_INVITE, Config.LANDING_URL
    )
    more_info_url = safe_get_url(Config.LANDING_URL, Config.LANDING_URL)

    # כפתורים – קודם מה מקבלים, אח"כ איך לשלם
    keyboard: List[List[InlineKeyboardButton]] = [
        [InlineKeyboardButton("ℹ️ מה אני מקבל?", callback_data="info_benefits")],
        [InlineKeyboardButton("💳 איך לשלם ולשלוח אישור", callback_data="send_proof")],
    ]

    if has_paid:
        keyboard.append(
            [InlineKeyboardButton("👥 כניסה לקבוצת העסקים", url=group_url)]
        )
        keyboard.append(
            [InlineKeyboardButton("🔗 הקישור האישי שלי", callback_data="open_my_link")]
        )
        keyboard.append(
            [
                InlineKeyboardButton(
                    "🎛 תפריט לחברים (Member Panel)",
                    callback_data="open_member_panel",
                )
            ]
        )

    keyboard.append(
        [InlineKeyboardButton("📈 מידע למשקיעים", callback_data="open_investor")]
    )
    keyboard.append(
        [InlineKeyboardButton("🔗 דף מידע מלא", url=more_info_url)]
    )

    reply_markup = InlineKeyboardMarkup(keyboard)

    await chat.send_message(text=body, reply_markup=reply_markup, parse_mode="Markdown")

    # לוגים – כל משתמש חדש / חזרה ל-start
    log_text = (
        "📥 משתמש הפעיל את /start\n"
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
    """פקודת whoami – פרטי משתמש + הפניות"""
    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat:
        return

    referrals_data = load_referrals()
    user_ref_data = referrals_data.get("users", {}).get(str(user.id), {})

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
    users_dict = referrals_data.get("users", {})

    total_users = stats.get("total_users", len(users_dict))
    active_users = len(users_dict)
    total_referrals = sum(
        u.get("referral_count", 0) for u in users_dict.values()
    )

    text = (
        "📊 **סטטיסטיקות קהילה:**\n"
        f"👥 סה״כ משתמשים: {total_users}\n"
        f"📈 משתמשים פעילים: {active_users}\n"
        f"🔄 הפניות כוללות: {total_referrals}"
    )

    await chat.send_message(text=text, parse_mode="Markdown")


# =========================
# הפניות – /my_link /my_referrals
# =========================
async def my_link_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """מחזיר למשתמש את קישור ההפניה האישי שלו"""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    # וידוא שהמשתמש רשום בקובץ referrals (אם לא – נרשום אותו בלי referrer)
    data = load_referrals()
    if str(user.id) not in data.get("users", {}):
        register_referral(user.id)

    data = load_referrals()
    user_ref_data = data.get("users", {}).get(str(user.id), {})
    count = user_ref_data.get("referral_count", 0)

    ref_link = build_referral_link(user.id)

    text = (
        "🔗 *הקישור האישי שלך להצטרפות לקהילת SLHNET:*\n\n"
        f"`{ref_link}`\n\n"
        "כל מי שיכנס דרך הקישור הזה וילחץ /start יירשם כהפניה שלך.\n\n"
        f"עד עכשיו רשומות על שמך *{count}* הפניות."
    )

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📤 שיתוף הקישור", url=ref_link)],
            [
                InlineKeyboardButton(
                    "👥 לראות את רשימת ההפניות", callback_data="open_my_referrals"
                )
            ],
        ]
    )

    await chat.send_message(text=text, parse_mode="Markdown", reply_markup=keyboard)


async def my_referrals_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """מציג סטטוס הפניות של המשתמש + הקישור שלו"""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    data = load_referrals()
    users = data.get("users", {})

    # אם המשתמש עדיין לא מופיע – נרשום אותו בלי referrer
    if str(user.id) not in users:
        register_referral(user.id)
        data = load_referrals()
        users = data.get("users", {})

    user_ref_data = users.get(str(user.id), {})
    count = user_ref_data.get("referral_count", 0)
    joined_at = user_ref_data.get("joined_at", "לא ידוע")

    ref_link = build_referral_link(user.id)

    text = (
        "👥 *הפניות אישיות – סטטוס*\n\n"
        f"📅 הצטרפת למערכת: {joined_at}\n"
        f"🔄 סך הכל הפניות על שמך: *{count}*\n\n"
        "🔗 *הקישור האישי שלך:*\n"
        f"`{ref_link}`\n\n"
        "העתק את הקישור או לחץ על כפתור השיתוף כדי לשלוח לחברים/עסקים."
    )

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📤 שיתוף הקישור", url=ref_link)],
            [
                InlineKeyboardButton(
                    "🔗 לקבל שוב את הקישור /my_link", callback_data="open_my_link"
                )
            ],
            [InlineKeyboardButton("🔙 חזרה לתפריט הראשי", callback_data="back_to_main")],
        ]
    )

    await chat.send_message(text=text, parse_mode="Markdown", reply_markup=keyboard)


# =========================
# תפריט חברים + אזור אישי
# =========================
async def member_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """תפריט לחברים אחרי אישור תשלום – /member"""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    paid = False
    try:
        paid = has_approved_payment(user.id)
    except Exception as e:
        logger.error(f"Error checking approved payment in /member for {user.id}: {e}")

    if not paid:
        await chat.send_message(
            "כדי לקבל גישה לתפריט החברים, צריך קודם להשלים תשלום 39 ₪ ולאשר אותו.\n"
            "השתמש ב-/start כדי לראות איך מצטרפים.",
        )
        return

    ref_link = build_referral_link(user.id)
    group_url = safe_get_url(
        Config.BUSINESS_GROUP_URL or Config.GROUP_STATIC_INVITE, Config.LANDING_URL
    )
    support_url = (
        Config.SUPPORT_GROUP_LINK or Config.BUSINESS_GROUP_URL or Config.LANDING_URL
    )

    text = (
        "🎛 *תפריט חבר בקהילת SLHNET*\n\n"
        "מכאן אפשר להגיע לכל מה שחשוב לחבר חדש:\n\n"
        "• 🔗 הקישור האישי שלך לשיתוף והפניות.\n"
        "• 👤 יצירת / עדכון כרטיס אישי (אזור אישי).\n"
        "• 💼 ארנק SLH פנימי וסטייקינג.\n"
        "• 📊 סטטוס הפניות.\n"
        "• 👥 מעבר מהיר לקבוצת העסקים.\n"
        "• 🆘 תמיכה / קשר אישי.\n\n"
        f"להפצה מיידית – הקישור שלך:\n`{ref_link}`"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔗 הקישור האישי שלי", callback_data="open_my_link"
                )
            ],
            [
                InlineKeyboardButton(
                    "👤 אזור אישי / כרטיס עסקי", callback_data="open_profile"
                )
            ],
            [InlineKeyboardButton("💼 ארנק פנימי /wallet", callback_data="hint_wallet")],
            [
                InlineKeyboardButton(
                    "📊 סטטוס הפניות", callback_data="open_my_referrals"
                )
            ],
            [InlineKeyboardButton("👥 קבוצת העסקים", url=group_url)],
            [InlineKeyboardButton("🆘 תמיכה / קשר אישי", url=support_url)],
            [InlineKeyboardButton("🔙 חזרה לתפריט הראשי", callback_data="back_to_main")],
        ]
    )

    await chat.send_message(text=text, parse_mode="Markdown", reply_markup=keyboard)


async def my_card_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """מתחיל תהליך יצירת/עדכון כרטיס אישי (אזור אישי)"""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    paid = False
    try:
        paid = has_approved_payment(user.id)
    except Exception as e:
        logger.error(f"Error checking approved payment in /my_card for {user.id}: {e}")

    if not paid:
        await chat.send_message(
            "כרטיס אישי זמין לחברי קהילה אחרי אישור תשלום.\n"
            "השתמש ב-/start כדי לראות איך מצטרפים.",
        )
        return

    # אתחול סשן פרופיל
    PROFILE_SESSIONS[user.id] = {"step": 0, "answers": []}

    await chat.send_message(
        "👤 *כרטיס אישי לחבר בקהילת SLHNET*\n\n"
        "אענה איתך על כמה שאלות קצרות, כדי ליצור כרטיס עסקי יפה שתוכל לשתף בקהילה.\n\n"
        "נתחיל:",
        parse_mode="Markdown",
    )

    await chat.send_message(PROFILE_QUESTIONS[0])


def build_profile_card_text(user_id: int) -> str:
    """בונה טקסט כרטיס אישי על בסיס הנתונים השמורים"""
    profiles_data = load_profiles()
    profile = profiles_data.get("profiles", {}).get(str(user_id))
    ref_link = build_referral_link(user_id)

    if not profile:
        return (
            "עדיין לא הגדרת כרטיס אישי.\n"
            "השתמש ב-/my_card כדי להתחיל.\n"
        )

    name = profile.get("name", "לא צוין")
    about = profile.get("about", "לא צוין")
    contact = profile.get("contact", "לא צוין")
    updated_at = profile.get("updated_at", "לא ידוע")

    text = (
        "👤 *כרטיס אישי – SLHNET*\n\n"
        f"📛 *שם / עסק*: {name}\n"
        f"💼 *מה אני עושה*: {about}\n"
        f"☎️ *איך ליצור קשר*: {contact}\n"
        f"🕒 עודכן לאחרונה: {updated_at}\n\n"
        "🔗 *הקישור האישי שלי להצטרפות:*\n"
        f"`{ref_link}`\n\n"
        "אפשר להעתיק את הטקסט הזה ולשתף אותו בקבוצת העסקים / ברשתות.\n"
    )
    return text


# =========================
# פקודות ניהול ותשלומים – 39 ₪
# =========================
async def payment_proof_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """קבלת צילום/קובץ כאישור תשלום והעברת הלוג לקבוצת הניהול."""
    user = update.effective_user
    chat = update.effective_chat
    message = update.message

    if not user or not chat or not message:
        return

    # נוודא שזה בפרטי מול הבוט בלבד
    if chat.type != "private":
        return

    caption = message.caption or ""
    text_lower = caption.lower()

    # ניסיון לזהות את סוג אמצעי התשלום
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

    # העתקת ההודעה לקבוצת הלוגים/ניהול
    if Config.LOGS_GROUP_CHAT_ID:
        try:
            admin_chat_id = int(Config.LOGS_GROUP_CHAT_ID)
            await context.bot.copy_message(
                chat_id=admin_chat_id,
                from_chat_id=chat.id,
                message_id=message.message_id,
            )

            # כפתורי אישור/דחייה לאדמין
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
        " - /approve <user_id>  – אישור תשלום ושליחת קישור לקבוצה + קישור אישי",
        " - /reject <user_id> <סיבה>  – דחיית תשלום והודעה ללקוח",
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
        lines.append(
            f"• user_id={p['user_id']} | username=@{p['username'] or 'לא ידוע'} | שיטה={p['pay_method']} | id={p['id']}"
        )

    await chat.send_message("\n".join(lines), parse_mode="Markdown")


async def _send_approval_package(
    bot, target_id: int, group_url: str
) -> None:
    """שולח למשתמש חבילת אישור: קישור קבוצה + קישור אישי + המלצה על /member ו-/my_card"""
    ref_link = build_referral_link(target_id)
    text = (
        "✅ התשלום שלך אושר!\n\n"
        "הנה הקישור להצטרפות לקהילת העסקים שלנו:\n"
        f"{group_url}\n\n"
        "ברוך הבא 🙌\n\n"
        "🔗 *הקישור האישי שלך לשיתוף עם חברים ועסקים:*\n"
        f"`{ref_link}`\n\n"
        "מומלץ עכשיו לפתוח את *תפריט החברים* עם הפקודה /member\n"
        "ולהגדיר כרטיס אישי דרך /my_card.\n"
    )
    await bot.send_message(chat_id=target_id, text=text, parse_mode="Markdown")


async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """אישור תשלום ידני לפי user_id – למנהלים בלבד."""
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

    group_url = safe_get_url(
        Config.BUSINESS_GROUP_URL or Config.GROUP_STATIC_INVITE, Config.LANDING_URL
    )

    try:
        await _send_approval_package(context.bot, target_id, group_url)
    except Exception as e:
        logger.error(f"Error sending approval package to user {target_id}: {e}")

    await chat.send_message(
        f"✅ התשלום של המשתמש {target_id} אושר ונשלח לו קישור לקבוצה + קישור אישי."
    )


async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """דחיית תשלום ידנית לפי user_id – למנהלים בלבד."""
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
    """מציג את ארנק ה-SLH הפנימי ומצבי הסטייקינג של המשתמש."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    try:
        # וידוא קיום ארנק
        ensure_internal_wallet(user.id, user.username or None)
        overview = get_wallet_overview(user.id) or {}
        stakes = get_user_stakes(user.id) or []
    except Exception as e:
        logger.error(f"wallet_command error: {e}")
        await chat.send_message(
            "❌ לא ניתן לטעון את ארנק ה-SLH כרגע. נסה שוב מאוחר יותר."
        )
        return

    balance = overview.get("balance_slh", 0)
    wallet_id = overview.get("wallet_id", "?")

    stakes_lines: List[str] = []
    total_staked = Decimal("0")
    for s in stakes:
        amt = s.get("amount_slh") or Decimal("0")
        total_staked += Decimal(str(amt))
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

    # נסה לפענח user_id
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
    """פותח עמדת סטייקינג חדשה על בסיס ארנק פנימי."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    args = context.args or []
    if len(args) < 2:
        help_text = (
            "כדי לפתוח סטייקינג השתמש:\n"
            "*/stake <סכום_SLH> <ימי_נעילה>* לדוגמה:\n"
            "`/stake 100 30` – סטייקינג על 100 SLH ל-30 ימים.\n\n"
            "לפני כן ודא שיש לך יתרה בארנק דרך הפקודה /wallet."
        )
        await chat.send_message(help_text, parse_mode="Markdown")
        return

    try:
        amount_slh = Decimal(str(args[0]).replace(",", "."))
        lock_days = int(args[1])
    except (InvalidOperation, ValueError):
        await chat.send_message(
            "❌ פורמט לא תקין. נסה שוב: `/stake 100 30`.",
            parse_mode="Markdown",
        )
        return

    if amount_slh <= 0 or lock_days <= 0:
        await chat.send_message("❌ הסכום וימי הנעילה חייבים להיות חיוביים.")
        return

    try:
        apy_percent = Decimal(os.getenv("INTERNAL_STAKING_APY", "15"))
    except InvalidOperation:
        apy_percent = Decimal("15")

    ok, message = create_stake_position(
        user_id=user.id,
        amount_slh=amount_slh,
        apy=apy_percent,
        lock_days=lock_days,
    )

    await chat.send_message(message)


async def mystakes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """מציג עמדות סטייקינג פעילות/סגורות"""
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
# Callback Query Handler
# =========================
async def callback_query_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """מטפל ב-callback queries של תפריט ההתחלה והאדמין"""
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    await query.answer()

    # תפריט משקיעים / יתרונות / חזרה
    if data == "open_investor":
        await handle_investor_callback(update, context)

    elif data == "info_benefits":
        await handle_benefits_callback(update, context)

    elif data == "back_to_main":
        await send_start_screen(update, context)

    # תפריט חברים
    elif data == "open_member_panel":
        await member_command(update, context)

    elif data == "open_profile":
        text = (
            "👤 *אזור אישי / כרטיס עסקי*\n\n"
            "כאן יוצרים כרטיס קצר שמציג מי אתה, מה אתה עושה ואיך ליצור איתך קשר.\n"
            "לאחר מכן אפשר להעתיק את הכרטיס ולשתף בקבוצת העסקים.\n\n"
            "לחץ על הכפתור כדי להתחיל:"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✏️ יצירת / עדכון כרטיס אישי", callback_data="start_profile"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔙 חזרה לתפריט החברים", callback_data="open_member_panel"
                    )
                ],
            ]
        )
        await query.edit_message_text(
            text=text, reply_markup=keyboard, parse_mode="Markdown"
        )

    elif data == "start_profile":
        # מפעיל את אותו flow של /my_card
        await my_card_command(update, context)

    elif data == "hint_wallet":
        text = (
            "כדי לראות את ארנק ה-SLH הפנימי שלך, השתמש בפקודה:\n"
            "`/wallet`\n\n"
            "ולפתיחת סטייקינג:\n"
            "`/stake 100 30` (לדוגמה)."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔙 חזרה לתפריט החברים", callback_data="open_member_panel"
                    )
                ]
            ]
        )
        await query.edit_message_text(
            text=text, reply_markup=keyboard, parse_mode="Markdown"
        )

    # תשלום – תפריט ראשי "איך לשלם"
    elif data in ("send_proof", "send_payment_instructions"):
        await handle_send_proof_callback(update, context)

    # דריל־דאון לפי אמצעי תשלום
    elif data == "pay_bank":
        text = build_payment_method_text("bank")
        support_url = (
            Config.SUPPORT_GROUP_LINK
            or Config.BUSINESS_GROUP_URL
            or Config.LANDING_URL
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📤 שליחת צילום מסך של האישור",
                        callback_data="upload_proof",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🆘 תמיכה / מענה אישי", url=support_url
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔙 חזרה לשיטות תשלום", callback_data="send_proof"
                    )
                ],
            ]
        )
        await query.edit_message_text(
            text=text, reply_markup=keyboard, parse_mode="Markdown"
        )

    elif data == "pay_paybox":
        text = build_payment_method_text("paybox")
        support_url = (
            Config.SUPPORT_GROUP_LINK
            or Config.BUSINESS_GROUP_URL
            or Config.LANDING_URL
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📤 שליחת צילום מסך של האישור",
                        callback_data="upload_proof",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🆘 תמיכה / מענה אישי", url=support_url
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔙 חזרה לשיטות תשלום", callback_data="send_proof"
                    )
                ],
            ]
        )
        await query.edit_message_text(
            text=text, reply_markup=keyboard, parse_mode="Markdown"
        )

    elif data == "pay_bit":
        text = build_payment_method_text("bit")
        support_url = (
            Config.SUPPORT_GROUP_LINK
            or Config.BUSINESS_GROUP_URL
            or Config.LANDING_URL
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📤 שליחת צילום מסך של האישור",
                        callback_data="upload_proof",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🆘 תמיכה / מענה אישי", url=support_url
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔙 חזרה לשיטות תשלום", callback_data="send_proof"
                    )
                ],
            ]
        )
        await query.edit_message_text(
            text=text, reply_markup=keyboard, parse_mode="Markdown"
        )

    elif data == "pay_paypal":
        text = build_payment_method_text("paypal")
        support_url = (
            Config.SUPPORT_GROUP_LINK
            or Config.BUSINESS_GROUP_URL
            or Config.LANDING_URL
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📤 שליחת צילום מסך של האישור",
                        callback_data="upload_proof",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🆘 תמיכה / מענה אישי", url=support_url
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔙 חזרה לשיטות תשלום", callback_data="send_proof"
                    )
                ],
            ]
        )
        await query.edit_message_text(
            text=text, reply_markup=keyboard, parse_mode="Markdown"
        )

    elif data == "pay_ton":
        text = build_payment_method_text("ton")
        support_url = (
            Config.SUPPORT_GROUP_LINK
            or Config.BUSINESS_GROUP_URL
            or Config.LANDING_URL
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📤 שליחת צילום מסך של האישור",
                        callback_data="upload_proof",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🆘 תמיכה / מענה אישי", url=support_url
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔙 חזרה לשיטות תשלום", callback_data="send_proof"
                    )
                ],
            ]
        )
        await query.edit_message_text(
            text=text, reply_markup=keyboard, parse_mode="Markdown"
        )

    elif data == "upload_proof":
        support_url = (
            Config.SUPPORT_GROUP_LINK
            or Config.BUSINESS_GROUP_URL
            or Config.LANDING_URL
        )
        text = (
            "📤 *איך לשלוח צילום מסך של אישור התשלום*\n\n"
            "1️⃣ לחץ על כפתור ה־📎 (או אייקון המצלמה) כאן בצ׳אט עם הבוט.\n"
            "2️⃣ בחר את צילום המסך של אישור התשלום (או קובץ PDF / מסמך מהבנק).\n"
            "3️⃣ שלח את הקובץ.\n\n"
            "המערכת תעביר את האישור לקבוצת הניהול, ולאחר האישור תקבל קישור לקבוצת העסקים.\n"
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "🔙 חזרה לשיטות תשלום", callback_data="send_proof"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🆘 תמיכה / מענה אישי", url=support_url
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔙 חזרה לתפריט הראשי", callback_data="back_to_main"
                    )
                ],
            ]
        )
        await query.edit_message_text(
            text=text, reply_markup=keyboard, parse_mode="Markdown"
        )

    # כפתורי ניהול אישור/דחייה מתוך הלוגים
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

        group_url = safe_get_url(
            Config.BUSINESS_GROUP_URL or Config.GROUP_STATIC_INVITE,
            Config.LANDING_URL,
        )
        try:
            await _send_approval_package(context.bot, target_id, group_url)
        except Exception as e:
            logger.error(f"Error sending approval package to user {target_id}: {e}")

        await query.edit_message_text(
            f"✅ התשלום של המשתמש {target_id} אושר ונשלח לו קישור לקבוצה + קישור אישי."
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

        await query.edit_message_text(
            f"🚫 התשלום של המשתמש {target_id} נדחה ונשלחה לו הודעה."
        )

    # פתיחת מסכים של /my_link /my_referrals מתוך כפתור אינליין
    elif data == "open_my_link":
        await my_link_command(update, context)

    elif data == "open_my_referrals":
        await my_referrals_command(update, context)

    else:
        await query.edit_message_text("❌ פעולה לא מוכרת.")


async def handle_investor_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """מטפל בכפתור מידע למשקיעים"""
    query = update.callback_query
    investor_text = load_message_block(
        "INVESTOR_INFO",
        "📈 **מידע למשקיעים**\n\n"
        "מערכת SLHNET מחברת בין טלגרם, חוזים חכמים על Binance Smart Chain, "
        "קבלות דיגיטליות ו-NFT, כך שכל עסקה מתועדת וניתנת למעקב.\n\n"
        "ניתן להצטרף כשותף, להחזיק טוקן SLH ולקבל חלק מהתנועה במערכת.",
    )

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 חזרה לתפריט הראשי", callback_data="back_to_main")]]
    )

    await query.edit_message_text(
        text=investor_text, reply_markup=keyboard, parse_mode="Markdown"
    )


async def handle_send_proof_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """מסביר איך לשלם – ונותן כפתורים לכל אמצעי תשלום + שליחת אישור"""
    query = update.callback_query

    text = (
        "💳 *איך לשלם 39 ₪ ולהצטרף לקהילה*\n\n"
        "בחר את אמצעי התשלום המתאים לך מהכפתורים למטה.\n"
        "בכל אמצעי תקבל הסבר מדויק איך לבצע את התשלום ואיך לשלוח צילום מסך של האישור.\n"
    )

    buttons: List[List[InlineKeyboardButton]] = []

    # אמצעי התשלום השונים
    buttons.append(
        [InlineKeyboardButton("🏦 העברה בנקאית", callback_data="pay_bank")]
    )

    if Config.PAYBOX_URL:
        buttons.append(
            [InlineKeyboardButton("📲 PayBox", callback_data="pay_paybox")]
        )
    if Config.BIT_URL:
        buttons.append([InlineKeyboardButton("📲 Bit", callback_data="pay_bit")])
    if Config.PAYPAL_URL:
        buttons.append(
            [InlineKeyboardButton("🌍 PayPal", callback_data="pay_paypal")]
        )
    if Config.TON_WALLET_ADDRESS:
        buttons.append(
            [
                InlineKeyboardButton(
                    "🔐 ארנק TON (קריפטו)", callback_data="pay_ton"
                )
            ]
        )

    # כפתור כללי לשליחת צילום מסך + תמיכה
    buttons.append(
        [
            InlineKeyboardButton(
                "📤 איך לשלוח צילום מסך", callback_data="upload_proof"
            )
        ]
    )

    support_url = (
        Config.SUPPORT_GROUP_LINK
        or Config.BUSINESS_GROUP_URL
        or Config.LANDING_URL
    )
    buttons.append(
        [InlineKeyboardButton("🆘 תמיכה / מענה אישי", url=support_url)]
    )

    buttons.append(
        [InlineKeyboardButton("🔙 חזרה לתפריט הראשי", callback_data="back_to_main")]
    )

    reply_markup = InlineKeyboardMarkup(buttons)

    await query.edit_message_text(
        text=text, reply_markup=reply_markup, parse_mode="Markdown"
    )


async def handle_benefits_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """מסביר ללקוח מה הוא מקבל מהמערכת"""
    query = update.callback_query
    benefits_text = load_message_block(
        "BENEFITS_INFO",
        "🎁 **מה מקבלים בתשלום 39 ₪?**\n\n"
        "• גישה לקבוצת עסקים חכמה בטלגרם עם תכנים, הדרכות וקהילה פעילה.\n"
        "• פתיחה וחיבור של ארנק SLH על רשת Binance Smart Chain (BSC).\n"
        "• אפשרות לקבל תשלומים דיגיטליים ועמלות הפנייה דרך המערכת.\n"
        "• חיבור לחוזים חכמים, קבלות דיגיטליות ו-NFT שמייצגים עסקאות ושערי כניסה.\n"
        "• בסיס לעתיד – סטייקינג, חסכונות והשקעות מתקדמות בתוך אקו־סיסטם SLHNET.\n\n"
        "אחרי התשלום ושליחת האישור – אתה מקבל קישור לקבוצה + סט כלים דיגיטליים להתחלה.",
    )

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 חזרה לתפריט הראשי", callback_data="back_to_main")]]
    )

    await query.edit_message_text(
        text=benefits_text, reply_markup=keyboard, parse_mode="Markdown"
    )


# =========================
# הודעות טקסט רגילות / פקודות לא מוכרות
# =========================
async def echo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """מטפל בהודעות טקסט רגילות – כולל המשך של אזור אישי אם יש סשן פתוח"""
    user = update.effective_user
    chat = update.effective_chat
    message = update.message
    if not user or not chat or not message:
        return

    text = message.text or ""

    # אם המשתמש באמצע תהליך כרטיס אישי – נטפל בזה קודם
    session = PROFILE_SESSIONS.get(user.id)
    if session is not None:
        step = session.get("step", 0)
        answers = session.get("answers", [])

        # שמירת התשובה
        answers.append(text.strip())
        step += 1
        session["step"] = step
        session["answers"] = answers
        PROFILE_SESSIONS[user.id] = session

        if step < len(PROFILE_QUESTIONS):
            # שאלה הבאה
            await chat.send_message(PROFILE_QUESTIONS[step])
            return
        else:
            # סיום – בניית כרטיס ושמירה
            profiles_data = load_profiles()
            profiles = profiles_data.setdefault("profiles", {})
            profiles[str(user.id)] = {
                "name": answers[0] if len(answers) > 0 else "",
                "about": answers[1] if len(answers) > 1 else "",
                "contact": answers[2] if len(answers) > 2 else "",
                "updated_at": datetime.now().isoformat(),
            }
            save_profiles(profiles_data)
            PROFILE_SESSIONS.pop(user.id, None)

            card_text = build_profile_card_text(user.id)

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📤 שיתוף בקבוצת העסקים",
                            url=safe_get_url(
                                Config.BUSINESS_GROUP_URL or Config.LANDING_URL,
                                Config.LANDING_URL,
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🎛 פתיחת תפריט החברים", callback_data="open_member_panel"
                        )
                    ],
                ]
            )

            await chat.send_message(
                "🎉 הכרטיס האישי שלך נשמר בהצלחה!\n\n" + card_text,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            return

    # אם לא באזור אישי – הודעת דיפולט
    logger.info(f"Message from {user.id}: {text}")

    response = load_message_block(
        "ECHO_RESPONSE",
        "✅ תודה על ההודעה! אנחנו כאן כדי לעזור.\nהשתמש ב-/start כדי לראות את התפריט הראשי.",
    )

    await message.reply_text(response)


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
            "landing_url": safe_get_url(
                Config.LANDING_URL, "https://slh-nft.com"
            ),
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
