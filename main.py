from telegram.ext import (
    MessageHandler,
    filters,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Application,
)
import os
import json
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
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

# ===== DB & SLH internal wallet imports =====
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

# ===== Optional routers =====
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
# Logging
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

# CORS – לאפשר דשבורד מהדומיין
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

# אתחול סכמת DB + ארנקים פנימיים
try:
    init_schema()
    init_internal_wallet_schema()
except Exception as e:
    logger.warning(f"init_schema or init_internal_wallet_schema failed: {e}")

BASE_DIR = Path(__file__).resolve().parent

# סטטיק + טמפלטים
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

# רואטרים חיצוניים
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
# Referrals – JSON store
# =========================
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
REF_FILE = DATA_DIR / "referrals.json"


def load_referrals() -> Dict[str, Any]:
    if not REF_FILE.exists():
        return {"users": {}, "statistics": {"total_users": 0}}

    try:
        with open(REF_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "users" not in data:
            data["users"] = {}
        if "statistics" not in data:
            data["statistics"] = {"total_users": 0}
        return data
    except Exception as e:
        logger.error(f"Error loading referrals: {e}")
        return {"users": {}, "statistics": {"total_users": 0}}


def save_referrals(data: Dict[str, Any]) -> None:
    try:
        data.setdefault("statistics", {})
        data["statistics"]["total_users"] = len(data.get("users", {}))
        with open(REF_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving referrals: {e}")


def register_referral(user_id: int, referrer_id: Optional[int] = None) -> bool:
    """
    רושם משתמש חדש עם referrer (אם קיים).
    אם המשתמש כבר קיים – לא דורס.
    """
    try:
        data = load_referrals()
        suid = str(user_id)

        if suid in data["users"]:
            return False

        user_data = {
            "referrer": str(referrer_id) if referrer_id else None,
            "joined_at": datetime.now().isoformat(),
            "referral_count": 0,
        }
        data["users"][suid] = user_data

        if referrer_id:
            rid = str(referrer_id)
            if rid in data["users"]:
                data["users"][rid]["referral_count"] = (
                    data["users"][rid].get("referral_count", 0) + 1
                )

        save_referrals(data)
        logger.info(f"Registered new user {user_id} with referrer {referrer_id}")
        return True
    except Exception as e:
        logger.error(f"Error registering referral: {e}")
        return False


# =========================
# Messages file (blocks)
# =========================
MESSAGES_FILE = BASE_DIR / "bot_messages_slhnet.txt"


def load_message_block(block_name: str, fallback: str = "") -> str:
    if not MESSAGES_FILE.exists():
        logger.warning(f"Messages file not found: {MESSAGES_FILE}")
        return fallback or f"[שגיאה: קובץ הודעות לא נמצא]"

    try:
        content = MESSAGES_FILE.read_text(encoding="utf-8")
        lines = content.splitlines()
        result_lines: List[str] = []
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
# Pydantic models
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
# Config & Admin logic
# =========================
def is_admin(user_id: int) -> bool:
    raw = os.getenv("ADMIN_OWNER_IDS", "")
    for part in raw.replace(",", " ").split():
        try:
            if int(part) == int(user_id):
                return True
        except ValueError:
            continue
    return False


class Config:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    BOT_USERNAME: str = os.getenv("BOT_USERNAME", "")
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
    SUPPORT_GROUP_LINK: str = os.getenv("SUPPORT_GROUP_LINK", "")
    REF_BASE_URL: str = os.getenv("REF_BASE_URL", "")

    @classmethod
    def validate(cls) -> List[str]:
        warnings: List[str] = []
        if not cls.BOT_TOKEN:
            warnings.append("⚠️ BOT_TOKEN לא מוגדר")
        if not cls.WEBHOOK_URL:
            warnings.append("⚠️ WEBHOOK_URL לא מוגדר")
        if not cls.ADMIN_ALERT_CHAT_ID:
            warnings.append("⚠️ ADMIN_ALERT_CHAT_ID לא מוגדר")
        if not cls.BOT_USERNAME:
            warnings.append("⚠️ BOT_USERNAME לא מוגדר – חשוב עבור /my_link")
        return warnings


# =========================
# Telegram Application Manager
# =========================
class TelegramAppManager:
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
        if cls._initialized:
            return

        app_instance = cls.get_app()

        handlers = [
            # כניסה ומידע
            CommandHandler("start", start_command),
            CommandHandler("whoami", whoami_command),
            CommandHandler("stats", stats_command),
            CommandHandler("portfolio", portfolio_command),

            # הפניות / רפרלים
            CommandHandler("my_link", my_link_command),
            CommandHandler("my_referrals", my_referrals_command),
            CommandHandler("my_card", my_card_command),

            # ניהול תשלומים
            CommandHandler("admin", admin_command),
            CommandHandler("pending", pending_command),
            CommandHandler("approve", approve_command),
            CommandHandler("reject", reject_command),

            # ארנק פנימי וסטייקינג
            CommandHandler("wallet", wallet_command),
            CommandHandler("send_slh", send_slh_command),
            CommandHandler("stake", stake_command),
            CommandHandler("mystakes", mystakes_command),

            # Callback queries
            CallbackQueryHandler(callback_query_handler),

            # אישורי תשלום (תמונות/מסמכים)
            MessageHandler(filters.PHOTO | filters.Document.ALL, payment_proof_handler),

            # טקסט חופשי + פקודות לא מוכרות
            MessageHandler(filters.TEXT & ~filters.COMMAND, echo_message),
            MessageHandler(filters.COMMAND, unknown_command),
        ]

        for h in handlers:
            app_instance.add_handler(h)

        cls._initialized = True
        logger.info("Telegram handlers initialized")

    @classmethod
    async def start(cls) -> None:
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
        try:
            app_instance = cls.get_app()
            await app_instance.stop()
            await app_instance.shutdown()
        except Exception as e:
            logger.error(f"Error during Telegram shutdown: {e}")


# =========================
# Utilities
# =========================
def safe_get_url(url: str, fallback: str) -> str:
    return url if url and url.startswith(("http://", "https://")) else fallback


async def send_log_message(text: str) -> None:
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


def build_payment_overview_text() -> str:
    """
    טקסט קצר שמסביר על כל אפשרויות התשלום (לשימוש במסך הכללי של "איך לשלם").
    """
    parts: List[str] = []

    # בנק
    parts.append(
        "🏦 *העברה בנקאית:*\n"
        "בנק הפועלים\n"
        "סניף כפר גנים (153)\n"
        "חשבון 73462\n"
        "המוטב: קאופמן צביקה\n\n"
    )

    if Config.PAYBOX_URL:
        parts.append(f"📲 *PayBox*: [לינק לתשלום]({Config.PAYBOX_URL})\n")
    if Config.BIT_URL:
        parts.append(f"📲 *Bit*: [לינק לתשלום]({Config.BIT_URL})\n")
    if Config.PAYPAL_URL:
        parts.append(f"🌍 *PayPal*: [לינק לתשלום]({Config.PAYPAL_URL})\n")
    if Config.TON_WALLET_ADDRESS:
        parts.append(
            f"🔐 *ארנק TON (קריפטו):*\n`{Config.TON_WALLET_ADDRESS}`\n"
        )

    parts.append(
        "\nלאחר ביצוע תשלום באחד הערוצים – שלח צילום מסך של האישור כאן בבוט.\n"
        "המערכת תעביר את האישור אוטומטית לקבוצת הניהול לאישור ידני.\n"
        "אחרי אישור – תקבל קישור לקבוצת העסקים + כל הכלים הדיגיטליים."
    )
    return "".join(parts)


def build_start_keyboard(has_paid: bool) -> InlineKeyboardMarkup:
    """
    תפריט התחלה:
    1. מה אני מקבל?
    2. איך לשלם ולשלוח אישור
    3. תשלום מהיר (PayBox) אם קיים
    4. כניסה לקבוצת העסקים (אם אושר)
    5. מידע למשקיעים
    6. אזור אישי
    7. תמיכה
    """
    buttons: List[List[InlineKeyboardButton]] = []

    # מה אני מקבל
    buttons.append(
        [InlineKeyboardButton("ℹ️ מה אני מקבל?", callback_data="info_benefits")]
    )

    # איך לשלם ולשלוח אישור – פותח תפריט אמצעי תשלום
    buttons.append(
        [
            InlineKeyboardButton(
                "💳 איך לשלם ולשלוח אישור", callback_data="send_proof_menu"
            )
        ]
    )

    # תשלום מהיר PayBox (אם יש URL)
    if Config.PAYBOX_URL:
        buttons.append(
            [
                InlineKeyboardButton(
                    "⚡ תשלום מהיר – PayBox", url=Config.PAYBOX_URL
                )
            ]
        )

    # כפתור כניסה לקבוצה אם המשתמש כבר אושר
    group_url = safe_get_url(
        Config.BUSINESS_GROUP_URL or Config.GROUP_STATIC_INVITE, Config.LANDING_URL
    )
    if has_paid:
        buttons.append(
            [InlineKeyboardButton("👥 כניסה לקבוצת העסקים", url=group_url)]
        )

    # מידע למשקיעים
    buttons.append(
        [
            InlineKeyboardButton(
                "📈 מידע למשקיעים", callback_data="open_investor"
            )
        ]
    )

    # אזור אישי
    buttons.append(
        [
            InlineKeyboardButton(
                "👤 האזור האישי שלי", callback_data="open_personal_area"
            )
        ]
    )

    # תמיכה
    support_url = safe_get_url(
        Config.SUPPORT_GROUP_LINK or Config.LANDING_URL, Config.LANDING_URL
    )
    buttons.append(
        [InlineKeyboardButton("🆘 תמיכה / צור קשר", url=support_url)]
    )

    return InlineKeyboardMarkup(buttons)


def build_payment_menu_keyboard() -> InlineKeyboardMarkup:
    """
    תפריט כפתורים של אמצעי תשלום.
    """
    rows: List[List[InlineKeyboardButton]] = []

    if Config.PAYBOX_URL:
        rows.append(
            [
                InlineKeyboardButton(
                    "📲 תשלום ב-PayBox", callback_data="pay_paybox"
                ),
                InlineKeyboardButton("📲 תשלום ב-Bit", callback_data="pay_bit"),
            ]
        )
    else:
        # גם אם אין PayBox, עדיין אפשר להציג Bit (אם קיים)
        if Config.BIT_URL:
            rows.append(
                [
                    InlineKeyboardButton(
                        "📲 תשלום ב-Bit", callback_data="pay_bit"
                    )
                ]
            )

    # PayPal
    if Config.PAYPAL_URL:
        rows.append(
            [
                InlineKeyboardButton(
                    "🌍 תשלום ב-PayPal", callback_data="pay_paypal"
                )
            ]
        )

    # העברה בנקאית
    rows.append(
        [
            InlineKeyboardButton(
                "🏦 העברה בנקאית", callback_data="pay_bank"
            )
        ]
    )

    # TON
    if Config.TON_WALLET_ADDRESS:
        rows.append(
            [
                InlineKeyboardButton(
                    "🔐 תשלום בקריפטו (TON)", callback_data="pay_ton"
                )
            ]
        )

    # איך לשלוח אישור
    rows.append(
        [
            InlineKeyboardButton(
                "📤 איך לשלוח צילום אישור", callback_data="send_proof_instructions"
            )
        ]
    )

    # חזרה
    rows.append(
        [
            InlineKeyboardButton(
                "🔙 חזרה לתפריט הראשי", callback_data="back_to_main"
            )
        ]
    )

    return InlineKeyboardMarkup(rows)


async def credit_user_after_approval(
    user_id: int, context: ContextTypes.DEFAULT_TYPE
) -> str:
    """
    מנסה לזכות את המשתמש ב-SLH פנימיים אחרי אישור תשלום.
    מחזיר טקסט קצר שיוסף להודעת האישור (או "" אם לא הצליח/לא רלוונטי).
    הכל עטוף ב-try/except כדי לא להפיל את הבוט.
    """
    try:
        ensure_internal_wallet(user_id, None)

        minted = None
        try:
            # נסיון 1 – אולי הפונקציה מוגדרת כמו mint_slh_from_payment(user_id)
            minted = mint_slh_from_payment(user_id)
        except TypeError:
            try:
                # נסיון 2 – אולי צריך גם סכום 39
                minted = mint_slh_from_payment(user_id, Decimal("39"))
            except Exception as e2:
                logger.error(
                    f"mint_slh_from_payment signature mismatch for {user_id}: {e2}"
                )
                minted = None
        except Exception as e:
            logger.error(f"mint_slh_from_payment error for {user_id}: {e}")
            minted = None

        if minted is None:
            return ""

        # אם חזרה מילון
        if isinstance(minted, dict):
            amount = minted.get("minted_slh") or minted.get("amount") or None
        else:
            amount = minted

        if amount is None:
            return ""

        try:
            amount_dec = Decimal(str(amount))
        except Exception:
            amount_dec = None

        display_amount = amount_dec if amount_dec is not None else amount

        return (
            f"\n\n💰 בנוסף, זוכית ב-*{display_amount}* SLH פנימיים בתוך המערכת.\n"
            "בדוק את היתרה שלך בפקודה /wallet."
        )

    except Exception as e:
        logger.error(f"Error crediting SLH after approval for {user_id}: {e}")
        return ""


# =========================
# Telegram Handlers
# =========================
async def send_start_screen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    referrer: Optional[int] = None,
) -> None:
    """מסך התחלה כולל תשלום 39 ₪, הפניות ותפריט בסיסי."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        logger.error("No user or chat in update for start screen")
        return

    # רישום referral
    register_referral(user.id, referrer)

    title = load_message_block("START_TITLE", "🚀 ברוך הבא ל-SLHNET!")
    body = load_message_block(
        "START_BODY",
        (
            "ברוך הבא לשער הדיגיטלי של קהילת SLHNET.\n"
            "כאן אתה מצטרף לקהילת עסקים, מקבל גישה לארנקים, חוזים חכמים, "
            "NFT וקבלת תשלומים – הכל סביב תשלום חד־פעמי של *39 ₪*."
        ),
    )

    image_path = BASE_DIR / Config.START_IMAGE_PATH
    try:
        if image_path.exists() and image_path.is_file():
            with image_path.open("rb") as f:
                await chat.send_photo(photo=InputFile(f), caption=title)
        else:
            await chat.send_message(text=title)
    except Exception as e:
        logger.error(f"Error sending start image: {e}")
        await chat.send_message(text=title)

    has_paid = False
    try:
        has_paid = has_approved_payment(user.id)
    except Exception as e:
        logger.error(f"Error checking approved payment for user {user.id}: {e}")

    reply_markup = build_start_keyboard(has_paid=has_paid)

    await chat.send_message(text=body, reply_markup=reply_markup, parse_mode="Markdown")

    # לוג לקבוצת לוגים
    log_text = (
        "📥 משתמש חדש הפעיל את הבוט\n"
        f"👤 User ID: {user.id}\n"
        f"📛 Username: @{user.username or 'לא מוגדר'}\n"
        f"🔰 שם: {user.full_name}\n"
        f"🔄 Referrer: {referrer or 'לא צוין'}"
    )
    await send_log_message(log_text)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    referrer = None
    if context.args:
        try:
            referrer = int(context.args[0])
        except (ValueError, TypeError):
            logger.warning(f"Invalid referrer ID in /start args: {context.args[0]}")
    await send_start_screen(update, context, referrer=referrer)


async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    referrals_data = load_referrals()
    user_data = referrals_data.get("users", {}).get(str(user.id), {})

    text = (
        "👤 *פרטי המשתמש שלך:*\n"
        f"🆔 ID: `{user.id}`\n"
        f"📛 שם משתמש: @{user.username or 'לא מוגדר'}\n"
        f"🔰 שם מלא: {user.full_name}\n"
        f"🔄 מספר הפניות: {user_data.get('referral_count', 0)}\n"
        f"📅 הצטרף: {user_data.get('joined_at', 'לא ידוע')}"
    )
    await chat.send_message(text=text, parse_mode="Markdown")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    referrals_data = load_referrals()
    stats = referrals_data.get("statistics", {})
    total_users = stats.get("total_users", 0)
    users_dict = referrals_data.get("users", {})
    total_referrals = sum(
        u.get("referral_count", 0) for u in users_dict.values()
    )

    text = (
        "📊 סטטיסטיקות קהילה:\n"
        f"👥 סה״כ משתמשים: {total_users}\n"
        f"📈 משתמשים פעילים: {len(users_dict)}\n"
        f"🔄 הפניות כוללות: {total_referrals}"
    )
    await chat.send_message(text=text)


# ===== הפניות / רפררים =====
async def my_link_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    # ודא שהמשתמש רשום ב-referrals (גם אם בלי referrer)
    register_referral(user.id, None)

    if Config.REF_BASE_URL:
        base = Config.REF_BASE_URL.rstrip("/")
    else:
        # בסיס לפי שם הבוט
        if Config.BOT_USERNAME:
            base = f"https://t.me/{Config.BOT_USERNAME}"
        else:
            base = "https://t.me"

    personal_link = f"{base}?start={user.id}"

    text = (
        "🔗 *הלינק האישי שלך להזמנת חברים:*\n\n"
        f"`{personal_link}`\n\n"
        "כל מי שנכנס דרך הקישור הזה ונרשם – נספר כהפניה על שמך.\n"
        "בדוק את הסטטוס ב-/my_referrals."
    )
    await chat.send_message(text=text, parse_mode="Markdown")


async def my_referrals_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    data = load_referrals()
    users = data.get("users", {})
    suid = str(user.id)
    me = users.get(suid)

    if not me:
        await chat.send_message(
            "עדיין לא רשומות הפניות על שמך.\n"
            "השתמש ב-/my_link כדי לקבל קישור אישי ולהתחיל להזמין אנשים."
        )
        return

    my_count = me.get("referral_count", 0)

    referred_ids = [
        uid for uid, info in users.items() if info.get("referrer") == suid
    ]
    sample_ids = ", ".join(referred_ids[:10]) if referred_ids else "אין עדיין רשומות."

    text = (
        "👥 *הפניות על שמך:*\n"
        f"🔢 סה\"כ הפניות: *{my_count}*\n\n"
        "רשימה (עד 10 ראשונים, לפי ID):\n"
        f"{sample_ids}\n\n"
        "המשך להזמין אנשים דרך הקישור האישי שלך!"
    )
    await chat.send_message(text=text, parse_mode="Markdown")


async def my_card_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    גרסה ראשונית – מסבירה למשתמש איך לבנות כרטיס אישי.
    (אפשר להרחיב בעתיד לשמירת פרופיל מלא ב-JSON).
    """
    chat = update.effective_chat
    if not chat:
        return

    text = (
        "📇 *כרטיס אישי – גרסה ראשונית*\n\n"
        "בשלב זה, כדי לבנות כרטיס אישי לפרסום בקהילה:\n"
        "1️⃣ כתוב בקצרה: מי אתה, מה העסק שלך, למי אתה יכול לעזור.\n"
        "2️⃣ הוסף לינקים חשובים (אתר, ווטסאפ, טלגרם, אינסטגרם וכו').\n"
        "3️⃣ שלח את הטקסט כאן בצ׳אט, ותוכל להשתמש בו לפרסום בקבוצה.\n\n"
        "בהמשך נוסיף שמירה אוטומטית, תצוגה יפה ושליחת הכרטיס בלחיצת כפתור."
    )
    await chat.send_message(text=text, parse_mode="Markdown")


# ===== תשלומים 39 ₪ =====
async def payment_proof_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """קבלת צילום/מסמך של אישור תשלום והעברת הלוג לקבוצת הניהול."""
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
    elif "bit" in text_lower or "ביט" in text_lower:
        pay_method = "bit"
    elif "paypal" in text_lower or "פייפאל" in text_lower:
        pay_method = "paypal"
    elif "העברה" in caption or "bank" in text_lower or "בנק" in text_lower:
        pay_method = "bank-transfer"
    elif "ton" in text_lower:
        pay_method = "ton"
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
                            "✅ אישור תשלום",
                            callback_data=f"approve:{user.id}",
                        ),
                        InlineKeyboardButton(
                            "❌ דחיית תשלום",
                            callback_data=f"reject:{user.id}",
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
                chat_id=admin_chat_id,
                text=admin_text,
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.error(f"Error sending payment log to admin group: {e}")

    await chat.send_message(
        "📥 קיבלנו את אישור התשלום שלך!\n"
        "ההודעה הועברה לצוות הניהול. לאחר אישור, ישלח אליך קישור לקבוצת העסקים."
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    ]
    await chat.send_message("\n".join(text_lines), parse_mode="Markdown")


async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
            f"• user_id={p['user_id']} | username=@{p['username'] or 'לא ידוע'} "
            f"| שיטה={p['pay_method']} | id={p['id']}"
        )

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

    group_url = safe_get_url(
        Config.BUSINESS_GROUP_URL or Config.GROUP_STATIC_INVITE, Config.LANDING_URL
    )

    bonus_text = await credit_user_after_approval(target_id, context)

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                "✅ התשלום שלך אושר!\n\n"
                "הנה הקישור להצטרפות לקהילת העסקים שלנו:\n"
                f"{group_url}\n\n"
                "ברוך הבא 🙌"
                f"{bonus_text}"
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Error sending approval message to user {target_id}: {e}")

    await chat.send_message(
        f"✅ התשלום של המשתמש {target_id} אושר ונשלח לו קישור לקבוצה."
    )


async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """דחיית תשלום ידנית לפי user_id."""
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


# ===== ארנק פנימי + סטייקינג =====
STAKING_DEFAULT_APY = Decimal(os.getenv("STAKING_DEFAULT_APY", "20"))
STAKING_DEFAULT_DAYS = int(os.getenv("STAKING_DEFAULT_DAYS", "90"))


async def wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ארנק SLH פנימי + סיכום סטייקינג בסיסי."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    try:
        ensure_internal_wallet(user.id, user.username or None)
        overview = get_wallet_overview(user.id) or {}
        stakes = get_user_stakes(user.id) or []
    except Exception as e:
        logger.error(f"wallet_command error: {e}")
        await chat.send_message(
            "❌ לא ניתן לטעון את ארנק ה-SLH כרגע. נסה שוב מאוחר יותר."
        )
        return

    balance = Decimal(str(overview.get("balance_slh", "0")))
    wallet_id = overview.get("wallet_id", "?")

    total_staked = Decimal("0")
    for s in stakes:
        try:
            total_staked += Decimal(str(s.get("amount_slh", "0")))
        except Exception:
            continue

    msg = (
        "💼 *ארנק SLH פנימי*\n\n"
        f"🆔 ID ארנק: `{wallet_id}`\n"
        f"💰 יתרה זמינה: *{balance}* SLH\n"
        f"🔒 סה״כ בסטייקינג: {total_staked} SLH\n\n"
        "כדי לפתוח סטייקינג חדש:\n"
        "*/stake <סכום_SLH> <ימי_נעילה>* לדוגמה:\n"
        "`/stake 100 30` – סטייקינג על 100 SLH ל-30 ימים.\n\n"
        "לצפייה בכל הסטייקים הפעילים:\n"
        "השתמש ב-/mystakes."
    )

    await chat.send_message(text=msg, parse_mode="Markdown")


async def send_slh_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """העברת SLH פנימיים: /send_slh <amount> <user_id>"""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    if len(context.args) < 2:
        await chat.send_message("שימוש: /send_slh <amount> <user_id>")
        return

    amount_str, target = context.args[0], context.args[1]
    try:
        amount = Decimal(amount_str.replace(",", "."))
    except InvalidOperation:
        await chat.send_message("סכום לא תקין. נסה שוב עם מספר תקין.")
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

    await chat.send_message(
        f"✅ הועברו {amount} SLH פנימיים למשתמש {to_user_id}."
    )


async def stake_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """סטייקינג: /stake <amount> [days]"""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    if not context.args:
        await chat.send_message(
            "שימוש: /stake <amount> [days]\n"
            f"ברירת מחדל ימים: {STAKING_DEFAULT_DAYS}, APY: {STAKING_DEFAULT_APY}%.",
            parse_mode="Markdown",
        )
        return

    amount_str = context.args[0]
    days = STAKING_DEFAULT_DAYS
    if len(context.args) >= 2:
        try:
            days = int(context.args[1])
        except ValueError:
            await chat.send_message(
                "ערך ימים לא תקין, משתמש בברירת המחדל."
            )

    try:
        amount = Decimal(amount_str.replace(",", "."))
    except InvalidOperation:
        await chat.send_message(
            "סכום לא תקין. נסה שוב עם מספר תקין."
        )
        return

    ok, msg = create_stake_position(user.id, amount, STAKING_DEFAULT_APY, days)
    if not ok:
        await chat.send_message(f"❌ סטייקינג נכשל: {msg}")
        return

    await chat.send_message(
        f"✅ פתחת סטייקינג על {amount} SLH ל-{days} ימים.\n"
        f"APY נוכחי: {STAKING_DEFAULT_APY}%.",
        parse_mode="Markdown",
    )


async def mystakes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """מציג עמדות סטייקינג עם תשואה משוערת."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    try:
        stakes = get_user_stakes(user.id) or []
    except Exception as e:
        logger.error(f"mystakes_command error: {e}")
        await chat.send_message(
            "❌ לא ניתן לטעון את הסטייקינג כרגע. נסה שוב מאוחר יותר."
        )
        return

    if not stakes:
        await chat.send_message("אין לך עדיין עמדות סטייקינג.")
        return

    lines = ["📊 *עמדות הסטייקינג שלך:*\n"]
    for st in stakes:
        try:
            amount = Decimal(str(st.get("amount_slh", "0")))
        except Exception:
            amount = Decimal("0")

        try:
            apy = Decimal(str(st.get("apy", STAKING_DEFAULT_APY)))
        except Exception:
            apy = STAKING_DEFAULT_APY

        try:
            lock_days = int(st.get("lock_days", STAKING_DEFAULT_DAYS))
        except Exception:
            lock_days = STAKING_DEFAULT_DAYS

        status = st.get("status", "unknown")
        started_raw = st.get("started_at")
        started_str = str(started_raw) if started_raw else "לא ידוע"

        expected_reward = (
            amount * apy / Decimal("100") * Decimal(lock_days) / Decimal("365")
        )

        lines.append(
            f"• {amount} SLH | {apy}% | {lock_days} ימים | סטטוס: {status}\n"
            f"  התחלה: {started_str}\n"
            f"  רווח משוער לסוף התקופה: ~{expected_reward:.4f} SLH\n"
        )

    await chat.send_message("\n".join(lines), parse_mode="Markdown")


async def portfolio_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """אזור אישי פיננסי – ארנק + סטייקינג + הפניות."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    # ארנק וסטייקינג
    try:
        ensure_internal_wallet(user.id, user.username or None)
        overview = get_wallet_overview(user.id) or {}
        stakes = get_user_stakes(user.id) or []
    except Exception as e:
        logger.error(f"portfolio wallet error: {e}")
        overview = {}
        stakes = []

    balance = Decimal(str(overview.get("balance_slh", "0")))
    total_staked = Decimal("0")
    total_expected = Decimal("0")

    for st in stakes:
        try:
            amount = Decimal(str(st.get("amount_slh", "0")))
        except Exception:
            amount = Decimal("0")
        try:
            apy = Decimal(str(st.get("apy", STAKING_DEFAULT_APY)))
        except Exception:
            apy = STAKING_DEFAULT_APY
        try:
            lock_days = int(st.get("lock_days", STAKING_DEFAULT_DAYS))
        except Exception:
            lock_days = STAKING_DEFAULT_DAYS

        total_staked += amount
        total_expected += (
            amount * apy / Decimal("100") * Decimal(lock_days) / Decimal("365")
        )

    # הפניות
    refs = load_referrals()
    users = refs.get("users", {})
    me = users.get(str(user.id), {})
    my_ref_count = me.get("referral_count", 0)

    text = (
        "📊 *האזור האישי שלך – SLHNET*\n\n"
        "💼 *ארנק פנימי:*\n"
        f"• יתרה זמינה: *{balance}* SLH\n"
        f"• בסטייקינג: *{total_staked}* SLH\n"
        f"• רווח משוער מכל הסטייקים (לסוף התקופות): ~{total_expected:.4f} SLH\n\n"
        "👥 *הפניות:*\n"
        f"• סה\"כ הפניות על שמך: *{my_ref_count}*\n"
        "• קבל לינק אישי בפקודה: /my_link\n"
        "• פירוט הפניות: /my_referrals\n\n"
        "🔗 *כלי עזר:*\n"
        "• /wallet – פירוט ארנק SLH\n"
        "• /mystakes – פירוט סטייקינג\n"
        "• /my_link – קישור אישי להזמנת חברים\n"
        "• /my_referrals – פירוט הפניות\n"
    )
    await chat.send_message(text=text, parse_mode="Markdown")


# ===== Callback queries =====
async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    await query.answer()

    if data == "open_investor":
        await handle_investor_callback(update, context)
    elif data == "info_benefits":
        await handle_benefits_callback(update, context)
    elif data == "back_to_main":
        await send_start_screen(update, context)
    elif data == "send_proof_menu":
        await handle_send_proof_menu(update, context)
    elif data == "send_proof_instructions":
        await handle_send_proof_instructions(update, context)
    elif data == "pay_paybox":
        await handle_paybox_callback(update, context)
    elif data == "pay_bit":
        await handle_bit_callback(update, context)
    elif data == "pay_paypal":
        await handle_paypal_callback(update, context)
    elif data == "pay_bank":
        await handle_bank_callback(update, context)
    elif data == "pay_ton":
        await handle_ton_callback(update, context)
    elif data == "open_personal_area":
        await handle_personal_area_callback(update, context)
    elif data.startswith("approve:"):
        await handle_inline_approve(update, context, data)
    elif data.startswith("reject:"):
        await handle_inline_reject(update, context, data)
    else:
        await query.edit_message_text("❌ פעולה לא מוכרת.")


async def handle_investor_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    investor_text = load_message_block(
        "INVESTOR_INFO",
        (
            "📈 *מידע למשקיעים*\n\n"
            "מערכת SLHNET מחברת בין טלגרם, חוזים חכמים על Binance Smart Chain, "
            "קבלות דיגיטליות ו-NFT, כך שכל עסקה מתועדת וניתנת למעקב.\n\n"
            "ניתן להצטרף כשותף, להחזיק טוקן SLH ולקבל חלק מהתנועה במערכת."
        ),
    )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 חזרה לתפריט הראשי", callback_data="back_to_main")]]
    )
    await query.edit_message_text(
        text=investor_text, reply_markup=keyboard, parse_mode="Markdown"
    )


async def handle_benefits_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    benefits_text = load_message_block(
        "BENEFITS_INFO",
        (
            "🎁 *מה מקבלים בתשלום 39 ₪?*\n\n"
            "• גישה לקבוצת עסקים חכמה בטלגרם עם תכנים, הדרכות וקהילה פעילה.\n"
            "• פתיחה וחיבור של ארנק SLH על רשת Binance Smart Chain (BSC).\n"
            "• אפשרות לקבל תשלומים דיגיטליים ועמלות הפנייה דרך המערכת.\n"
            "• חיבור לחוזים חכמים, קבלות דיגיטליות ו-NFT שמייצגים עסקאות ושערי כניסה.\n"
            "• בסיס לעתיד – סטייקינג, חסכונות והשקעות מתקדמות בתוך אקו־סיסטם SLHNET.\n\n"
            "אחרי התשלום ושליחת האישור – אתה מקבל קישור לקבוצה + סט כלים דיגיטליים להתחלה."
        ),
    )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 חזרה לתפריט הראשי", callback_data="back_to_main")]]
    )
    await query.edit_message_text(
        text=benefits_text, reply_markup=keyboard, parse_mode="Markdown"
    )


async def handle_send_proof_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    text = (
        "💳 *איך לשלם ולשלוח אישור:*\n\n"
        "בחר את אמצעי התשלום שמתאים לך מהתפריט למטה.\n"
        "לאחר ביצוע התשלום – שלח כאן לבוט צילום מסך ברור של האישור.\n\n"
        "אפשרויות זמינות בשלבים הנוכחיים:"
    )
    keyboard = build_payment_menu_keyboard()
    await query.edit_message_text(
        text=text, reply_markup=keyboard, parse_mode="Markdown"
    )


async def handle_send_proof_instructions(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    text = (
        "📤 *איך לשלוח אישור תשלום:*\n\n"
        "1️⃣ בצע תשלום באחד מאמצעי התשלום הזמינים.\n"
        "2️⃣ קח *צילום מסך ברור* של האישור (או PDF מהבנק).\n"
        "3️⃣ חזור לצ׳אט עם הבוט ושלח את התמונה/הקובץ כהודעה רגילה.\n"
        "4️⃣ המערכת תעביר את האישור אוטומטית לקבוצת הניהול לאישור.\n\n"
        "אחרי שהאדמין יאשר – תקבל קישור לקבוצת העסקים + גישה לכל הכלים."
    )
    keyboard = build_payment_menu_keyboard()
    await query.edit_message_text(
        text=text, reply_markup=keyboard, parse_mode="Markdown"
    )


async def handle_paybox_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    url = Config.PAYBOX_URL
    if not url:
        await query.answer("קישור PayBox לא מוגדר כרגע.", show_alert=True)
        return

    text = (
        "📲 *תשלום דרך PayBox*\n\n"
        f"1️⃣ לחץ על הקישור: {url}\n"
        "2️⃣ בצע תשלום חד־פעמי של *39 ₪*.\n"
        "3️⃣ שמור צילום מסך של אישור התשלום.\n"
        "4️⃣ שלח את צילום המסך כאן לבוט.\n\n"
        "המערכת תעביר את האישור לקבוצת הניהול, ולאחר אישור תקבל קישור לקבוצה."
    )
    keyboard = build_payment_menu_keyboard()
    await query.edit_message_text(
        text=text, reply_markup=keyboard, parse_mode="Markdown"
    )


async def handle_bit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    url = Config.BIT_URL
    if not url:
        await query.answer("קישור Bit לא מוגדר כרגע.", show_alert=True)
        return

    text = (
        "📲 *תשלום דרך Bit*\n\n"
        f"1️⃣ לחץ על הקישור: {url}\n"
        "2️⃣ בצע תשלום חד־פעמי של *39 ₪*.\n"
        "3️⃣ שמור צילום מסך של אישור התשלום.\n"
        "4️⃣ שלח את צילום המסך כאן לבוט.\n\n"
        "המערכת תעביר את האישור לקבוצת הניהול, ולאחר אישור תקבל קישור לקבוצה."
    )
    keyboard = build_payment_menu_keyboard()
    await query.edit_message_text(
        text=text, reply_markup=keyboard, parse_mode="Markdown"
    )


async def handle_paypal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    url = Config.PAYPAL_URL
    if not url:
        await query.answer("קישור PayPal לא מוגדר כרגע.", show_alert=True)
        return

    text = (
        "🌍 *תשלום דרך PayPal*\n\n"
        f"1️⃣ לחץ על הקישור: {url}\n"
        "2️⃣ בצע תשלום חד־פעמי של *39 ₪* (או סכום מוסכם מראש).\n"
        "3️⃣ שמור צילום מסך של אישור התשלום.\n"
        "4️⃣ שלח את צילום המסך כאן לבוט.\n\n"
        "המערכת תעביר את האישור לקבוצת הניהול, ולאחר אישור תקבל קישור לקבוצה."
    )
    keyboard = build_payment_menu_keyboard()
    await query.edit_message_text(
        text=text, reply_markup=keyboard, parse_mode="Markdown"
    )


async def handle_bank_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    text = (
        "🏦 *תשלום בהעברה בנקאית*\n\n"
        "פרטי החשבון:\n"
        "בנק הפועלים\n"
        "סניף כפר גנים (153)\n"
        "חשבון 73462\n"
        "המוטב: קאופמן צביקה\n\n"
        "1️⃣ בצע העברה של *39 ₪* לחשבון לעיל.\n"
        "2️⃣ שמור צילום מסך ברור / PDF של אישור ההעברה.\n"
        "3️⃣ שלח את האישור כאן לבוט.\n\n"
        "המערכת תעביר את האישור לקבוצת הניהול, ולאחר אישור תקבל קישור לקבוצה."
    )
    keyboard = build_payment_menu_keyboard()
    await query.edit_message_text(
        text=text, reply_markup=keyboard, parse_mode="Markdown"
    )


async def handle_ton_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not Config.TON_WALLET_ADDRESS:
        await query.answer("ארנק TON לא מוגדר כרגע.", show_alert=True)
        return

    text = (
        "🔐 *תשלום בקריפטו – TON*\n\n"
        "שלח תשלום לכתובת הארנק:\n"
        f"`{Config.TON_WALLET_ADDRESS}`\n\n"
        "1️⃣ בצע תשלום בסכום שסוכם (לדוגמה, ערך מקביל ל-39 ₪).\n"
        "2️⃣ שמור צילום מסך של הטרנזקציה (או לינק ל-TonScan).\n"
        "3️⃣ שלח את צילום המסך / הלינק כאן לבוט.\n\n"
        "לאחר אישור בצד הניהול – תקבל קישור לקבוצה + כל הכלים הדיגיטליים."
    )
    keyboard = build_payment_menu_keyboard()
    await query.edit_message_text(
        text=text, reply_markup=keyboard, parse_mode="Markdown"
    )


async def handle_personal_area_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """פותח למשתמש תיאור קצר של האזור האישי והפקודות הרלוונטיות."""
    query = update.callback_query
    text = (
        "👤 *האזור האישי שלך – סיכום:*\n\n"
        "• /portfolio – סיכום ארנק + סטייקינג + הפניות.\n"
        "• /wallet – פירוט הארנק הפנימי.\n"
        "• /mystakes – פירוט הסטייקים הפיננסיים.\n"
        "• /my_link – קישור אישי להזמנת חברים.\n"
        "• /my_referrals – רשימת הפניות.\n"
        "• /my_card – כרטיס אישי בסיסי לפרסום בקהילה.\n\n"
        "המשך להתקדם – כל צעד כאן בונה את הכלכלה האישית והקהילתית שלך."
    )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔙 חזרה לתפריט הראשי", callback_data="back_to_main")]]
    )
    await query.edit_message_text(
        text=text, reply_markup=keyboard, parse_mode="Markdown"
    )


async def handle_inline_approve(
    update: Update, context: ContextTypes.DEFAULT_TYPE, data: str
) -> None:
    query = update.callback_query
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
        Config.BUSINESS_GROUP_URL or Config.GROUP_STATIC_INVITE, Config.LANDING_URL
    )
    bonus_text = await credit_user_after_approval(target_id, context)

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                "✅ התשלום שלך אושר!\n\n"
                "הנה הקישור להצטרפות לקהילת העסקים שלנו:\n"
                f"{group_url}\n\n"
                "ברוך הבא 🙌"
                f"{bonus_text}"
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Error sending approval message to user {target_id}: {e}")

    await query.edit_message_text(
        f"✅ התשלום של המשתמש {target_id} אושר ונשלח לו קישור לקבוצה."
    )


async def handle_inline_reject(
    update: Update, context: ContextTypes.DEFAULT_TYPE, data: str
) -> None:
    query = update.callback_query
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


# ===== טקסט חופשי / פקודות לא מוכרות =====
async def echo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    await update.message.reply_text(
        "❓ פקודה לא מוכרת. השתמש ב-/start כדי לראות את התפריט הזמין."
    )


# =========================
# FastAPI Routes
# =========================
@app.get("/api/metrics/finance")
async def finance_metrics():
    reserve_stats = get_reserve_stats() or {}
    approval_stats = get_approval_stats() or {}
    return {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "reserve": reserve_stats,
        "approvals": approval_stats,
    }


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service="slhnet-telegram-gateway",
        timestamp=datetime.now().isoformat(),
        version="2.0.0",
    )


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    if not templates:
        return HTMLResponse("<h1>SLHNET Bot - Template Engine Not Available</h1>")

    return templates.TemplateResponse(
        "landing.html",
        {
            "request": request,
            "landing_url": safe_get_url(Config.LANDING_URL, "https://slh-nft.com"),
            "business_group_url": safe_get_url(
                Config.BUSINESS_GROUP_URL or Config.GROUP_STATIC_INVITE,
                "https://slh-nft.com",
            ),
        },
    )


@app.post("/webhook")
async def telegram_webhook(update: TelegramWebhookUpdate):
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
    try:
        init_internal_wallet_schema()
    except Exception as e:
        logger.error(f"init_internal_wallet_schema failed: {e}")

    warnings = Config.validate()
    for w in warnings:
        logger.warning(w)
    if warnings:
        await send_log_message("⚠️ **אזהרות אתחול:**\n" + "\n".join(warnings))

    try:
        await TelegramAppManager.start()
    except Exception as e:
        logger.error(f"Failed to start Telegram Application: {e}")


# =========================
# Local run
# =========================
if __name__ == "__main__":
    import uvicorn

    warnings = Config.validate()
    if warnings:
        print("⚠️ אזהרות קונפיגורציה:")
        for w in warnings:
            print(f"  {w}")

    port = int(os.getenv("PORT", "8080"))
    print(f"🚀 Starting SLHNET Bot on port {port}")

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        log_config=None,
    )
