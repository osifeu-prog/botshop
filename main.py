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

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# === DB & internal wallets imports ===
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
    mint_slh_from_payment,  # משמש למינט SLH אחרי תשלום / קרדיט אדמין
)

# === Optional routers ===
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
    version="2.2.0",
)

# CORS
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

BASE_DIR = Path(__file__).resolve().parent

# Static & templates
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

# Routers
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
# Referral & profile storage (file-based)
# =========================
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
REF_FILE = DATA_DIR / "referrals.json"
PROFILE_FILE = DATA_DIR / "profiles.json"
MESSAGES_FILE = BASE_DIR / "bot_messages_slhnet.txt"


def load_referrals() -> Dict[str, Any]:
    """
    טוען את קובץ ההפניות מהדיסק.
    מבנה בסיסי:
    {
        "users": {
            "<telegram_id>": {
                "referrer": "<telegram_id|None>",
                "joined_at": "ISO8601",
                "referral_count": int
            },
            ...
        },
        "statistics": {
            "total_users": int
        }
    }
    """
    if not REF_FILE.exists():
        return {"users": {}, "statistics": {"total_users": 0}}

    try:
        with REF_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if "users" not in data:
            data["users"] = {}
        if "statistics" not in data:
            data["statistics"] = {"total_users": len(data["users"])}
        return data
    except Exception as e:
        logger.error(f"Error loading referrals: {e}")
        return {"users": {}, "statistics": {"total_users": 0}}


def save_referrals(data: Dict[str, Any]) -> None:
    """שומר את קובץ ההפניות לדיסק בצורה אטומית ככל האפשר."""
    try:
        data["statistics"]["total_users"] = len(data.get("users", {}))
        tmp_path = REF_FILE.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp_path.replace(REF_FILE)
    except Exception as e:
        logger.error(f"Error saving referrals: {e}")


def register_referral(user_id: int, referrer_id: Optional[int] = None) -> None:
    """
    רושם משתמש חדש בקובץ ההפניות.
    אם referrer_id קיים כבר במערכת – מגדיל לו את מונה ההפניות.
    """
    try:
        data = load_referrals()
        suid = str(user_id)
        if suid not in data["users"]:
            data["users"][suid] = {
                "referrer": str(referrer_id) if referrer_id else None,
                "joined_at": datetime.now().isoformat(),
                "referral_count": 0,
            }
            # increment referrer counter if exists
            if referrer_id:
                rid = str(referrer_id)
                if rid in data["users"]:
                    data["users"][rid]["referral_count"] = (
                        data["users"][rid].get("referral_count", 0) + 1
                    )
            save_referrals(data)
    except Exception as e:
        logger.error(f"Error registering referral: {e}")


def get_user_referrals(user_id: int) -> List[int]:
    """
    מחזיר רשימת user_id שהופנו ע״י user_id מסויים.
    """
    data = load_referrals()
    suid = str(user_id)
    result: List[int] = []
    for k, v in data.get("users", {}).items():
        if v.get("referrer") == suid:
            try:
                result.append(int(k))
            except Exception:
                continue
    return result


# =========================
# Profiles (simple file-based storage)
# =========================
def load_profiles() -> Dict[str, Any]:
    """טוען פרופילים של משתמשים (mini-CRM)."""
    if not PROFILE_FILE.exists():
        return {}
    try:
        with PROFILE_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading profiles: {e}")
        return {}


def save_profiles(data: Dict[str, Any]) -> None:
    """שומר פרופילים לדיסק."""
    try:
        tmp_path = PROFILE_FILE.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp_path.replace(PROFILE_FILE)
    except Exception as e:
        logger.error(f"Error saving profiles: {e}")


def upsert_profile(
    user_id: int,
    username: Optional[str],
    full_name: str,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """
    מעדכן/יוצר פרופיל בסיסי למשתמש.
    זה future-ready כדי שבשלב הבא נוכל לשאול שאלות ולהעמיק בפרופיל.
    """
    try:
        profiles = load_profiles()
        suid = str(user_id)
        profile = profiles.get(suid, {})
        profile.update(
            {
                "user_id": user_id,
                "username": username,
                "full_name": full_name,
                "updated_at": datetime.now().isoformat(),
            }
        )
        if extra:
            profile.setdefault("extra", {}).update(extra)
        profiles[suid] = profile
        save_profiles(profiles)
    except Exception as e:
        logger.error(f"Error upserting profile: {e}")


# =========================
# On-chain (external) wallets per user (file-based)
# =========================

ONCHAIN_FILE = DATA_DIR / "onchain_wallets.json"


def load_onchain_wallets() -> Dict[str, Any]:
    """
    אחסון פשוט של כתובות On-chain לכל משתמש:
    {
      "123456789": {
        "bsc": "0x....",
        "ton": "UQ....",
        "updated_at": "ISO8601"
      },
      ...
    }
    """
    if not ONCHAIN_FILE.exists():
        return {}
    try:
        with ONCHAIN_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading on-chain wallets: {e}")
        return {}


def save_onchain_wallets(data: Dict[str, Any]) -> None:
    try:
        tmp = ONCHAIN_FILE.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(ONCHAIN_FILE)
    except Exception as e:
        logger.error(f"Error saving on-chain wallets: {e}")


def get_onchain_wallet(user_id: int) -> Dict[str, Optional[str]]:
    """
    מחזיר את הארנק החיצוני של המשתמש (BSC/TON).
    אם לא מוגדר – מחזיר ערכי None.
    """
    data = load_onchain_wallets()
    rec = data.get(str(user_id), {})
    return {
        "bsc": rec.get("bsc"),
        "ton": rec.get("ton"),
        "updated_at": rec.get("updated_at"),
    }


def set_onchain_wallet(
    user_id: int,
    bsc_address: Optional[str] = None,
    ton_address: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """
    מעדכן/יוצר כתובות On-chain למשתמש.
    אם אחת מהכתובות היא '-', ננקה אותה.
    """
    data = load_onchain_wallets()
    suid = str(user_id)
    rec = data.get(suid, {})
    if bsc_address is not None:
        rec["bsc"] = None if bsc_address == "-" else bsc_address
    if ton_address is not None:
        rec["ton"] = None if ton_address == "-" else ton_address
    rec["updated_at"] = datetime.now().isoformat()
    data[suid] = rec
    save_onchain_wallets(data)
    return rec


# =========================
# Messages file helper
# =========================
def load_message_block(block_name: str, fallback: str = "") -> str:
    """
    טוען בלוק מלל מתוך bot_messages_slhnet.txt.
    פורמט גס:
    === START_TITLE ===
    ...
    === END ===
    """
    if not MESSAGES_FILE.exists():
        if fallback:
            return fallback
        return "[שגיאה: קובץ הודעות לא נמצא]"

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
            return f"[שגיאה: בלוק {block_name} לא נמצא]"
        if not result_lines and fallback:
            return fallback
        return "\n".join(result_lines).strip() or fallback
    except Exception as e:
        logger.error(f"Error loading message block '{block_name}': {e}")
        return fallback or f"[שגיאה בטעינת בלוק {block_name}]"


# =========================
# Dynamic SLH price config (file-based)
# =========================
try:
    DEFAULT_SLH_PRICE = Decimal(os.getenv("SLH_NIS_PRICE", "444"))
except Exception:
    DEFAULT_SLH_PRICE = Decimal("444")

try:
    DEFAULT_ENTRY_AMOUNT = Decimal(os.getenv("NIS_ENTRY_AMOUNT", "39"))
except Exception:
    DEFAULT_ENTRY_AMOUNT = Decimal("39")

DYNAMIC_CONFIG_FILE = DATA_DIR / "slh_dynamic_config.json"


def load_dynamic_config() -> Dict[str, Any]:
    """
    קובץ קונפיגורציה דינמי ל-SLH:
    {
      "slh_nis_price": float,      # מחיר SLH אחד בש"ח
      "nis_entry_amount": float,   # סכום כניסה בש"ח (ברירת מחדל 39)
      "total_slh_minted": float    # כמה SLH חולקו ללקוחות עד כה
    }
    """
    base = {
        "slh_nis_price": float(DEFAULT_SLH_PRICE),
        "nis_entry_amount": float(DEFAULT_ENTRY_AMOUNT),
        "total_slh_minted": 0.0,
    }
    if not DYNAMIC_CONFIG_FILE.exists():
        return base
    try:
        with DYNAMIC_CONFIG_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        for k in base.keys():
            if k in data:
                base[k] = data[k]
        return base
    except Exception as e:
        logger.error(f"Error loading dynamic SLH config: {e}")
        return base


def save_dynamic_config(cfg: Dict[str, Any]) -> None:
    try:
        tmp_path = DYNAMIC_CONFIG_FILE.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        tmp_path.replace(DYNAMIC_CONFIG_FILE)
    except Exception as e:
        logger.error(f"Error saving dynamic SLH config: {e}")


def get_current_price_and_entry() -> (Decimal, Decimal):
    cfg = load_dynamic_config()
    try:
        price = Decimal(str(cfg.get("slh_nis_price", float(DEFAULT_SLH_PRICE))))
    except Exception:
        price = DEFAULT_SLH_PRICE
    try:
        entry = Decimal(str(cfg.get("nis_entry_amount", float(DEFAULT_ENTRY_AMOUNT))))
    except Exception:
        entry = DEFAULT_ENTRY_AMOUNT
    return price, entry


def record_mint_amount(amount_slh: Decimal) -> None:
    try:
        cfg = load_dynamic_config()
        current_total = Decimal(str(cfg.get("total_slh_minted", 0.0)))
        new_total = current_total + amount_slh
        cfg["total_slh_minted"] = float(new_total)
        save_dynamic_config(cfg)
    except Exception as e:
        logger.error(f"Error recording minted SLH: {e}")


def compute_slh_for_entry(price_nis: Decimal, entry_nis: Decimal) -> Decimal:
    """
    מחשב כמה SLH מקבלים עבור סכום כניסה מסויים בש"ח.
    """
    if price_nis <= 0:
        return Decimal("0")
    try:
        return (entry_nis / price_nis).quantize(Decimal("0.0001"))
    except Exception:
        return Decimal("0")


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


class ConfigSnapshot(BaseModel):
    """ייצוג בטוח (ללא סודות) של קונפיגורציית הבוט לממשק ה-API."""

    bot_username: str
    landing_url: str
    business_group_url: str
    support_group_link: str
    has_paybox: bool
    has_bit: bool
    has_paypal: bool
    has_ton: bool
    logs_group_set: bool
    slh_nis_price: float
    nis_entry_amount: float
    total_slh_minted: float
    hot_wallet_address: str
    cold_wallet_address: str


class WalletAPIResponse(BaseModel):
    user_id: int
    balance_slh: float
    staked_slh: float
    value_nis: float
    bsc_address: Optional[str]
    ton_address: Optional[str]


# =========================
# Config & helpers
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
    BOT_USERNAME: str = os.getenv("BOT_USERNAME", "Buy_My_Shop_bot")
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
    LOGS_GROUP_CHAT_ID: str = os.getenv("LOGS_GROUP_CHAT_ID", ADMIN_ALERT_CHAT_ID or "")
    SUPPORT_GROUP_LINK: str = os.getenv("SUPPORT_GROUP_LINK", "")
    STAKING_DEFAULT_APY: Decimal = Decimal(os.getenv("STAKING_DEFAULT_APY", "20"))
    STAKING_DEFAULT_DAYS: int = int(os.getenv("STAKING_DEFAULT_DAYS", "90"))

    # ארנק חם / קר – להצגה ב-/wallet ו-/admin_wallet
    HOT_WALLET_ADDRESS: str = os.getenv("HOT_WALLET_ADDRESS", "")
    COLD_WALLET_ADDRESS: str = os.getenv("COLD_WALLET_ADDRESS", "")

    @classmethod
    def validate(cls) -> List[str]:
        warnings: List[str] = []
        if not cls.BOT_TOKEN:
            warnings.append("⚠️ BOT_TOKEN לא מוגדר")
        if not cls.WEBHOOK_URL:
            warnings.append("⚠️ WEBHOOK_URL לא מוגדר")
        if not cls.ADMIN_ALERT_CHAT_ID:
            warnings.append("⚠️ ADMIN_ALERT_CHAT_ID לא מוגדר")
        return warnings

    @classmethod
    def snapshot(cls) -> ConfigSnapshot:
        """החזרת תמונת מצב בטוחה (ללא טוקנים/סודות) לקונפיגורציה."""
        cfg = load_dynamic_config()
        return ConfigSnapshot(
            bot_username=cls.BOT_USERNAME,
            landing_url=cls.LANDING_URL,
            business_group_url=cls.BUSINESS_GROUP_URL,
            support_group_link=cls.SUPPORT_GROUP_LINK,
            has_paybox=bool(cls.PAYBOX_URL),
            has_bit=bool(cls.BIT_URL),
            has_paypal=bool(cls.PAYPAL_URL),
            has_ton=bool(cls.TON_WALLET_ADDRESS),
            logs_group_set=bool(cls.LOGS_GROUP_CHAT_ID),
            slh_nis_price=float(cfg.get("slh_nis_price", float(DEFAULT_SLH_PRICE))),
            nis_entry_amount=float(
                cfg.get("nis_entry_amount", float(DEFAULT_ENTRY_AMOUNT))
            ),
            total_slh_minted=float(cfg.get("total_slh_minted", 0.0)),
            hot_wallet_address=cls.HOT_WALLET_ADDRESS,
            cold_wallet_address=cls.COLD_WALLET_ADDRESS,
        )


def safe_get_url(url: str, fallback: str) -> str:
    return url if url and url.startswith(("http://", "https://")) else fallback


def format_decimal_pretty(value: Decimal) -> str:
    try:
        if value == 0:
            return "0"
        q = value.quantize(Decimal("0.0001"))
        s = format(q, "f")
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s
    except Exception:
        return str(value)


async def send_log_message(text: str) -> None:
    """שולח הודעה לקבוצת לוגים (אם מוגדרת)."""
    if not Config.LOGS_GROUP_CHAT_ID:
        return
    try:
        app_instance = TelegramAppManager.get_app()
        await app_instance.bot.send_message(
            chat_id=int(Config.LOGS_GROUP_CHAT_ID), text=text
        )
    except Exception as e:
        logger.error(f"Failed to send log message: {e}")


async def send_bug_report(
    feature_id: str,
    user: Optional[Any],
    chat: Optional[Any],
) -> None:
    """
    שליחת דיווח באג לקבוצת הלוגים.
    feature_id – מזהה קצר של המסך / כפתור.
    """
    if not Config.LOGS_GROUP_CHAT_ID:
        return
    try:
        lines = [
            "🐞 דיווח תקלה חדש מהבוט:",
            f"📍 פיצ'ר: {feature_id}",
        ]
        if user is not None:
            lines.append(f"👤 user_id={user.id}, username=@{user.username or 'N/A'}")
            lines.append(f"👤 full_name={user.full_name}")
        if chat is not None:
            lines.append(f"💬 chat_id={chat.id}, type={chat.type}")
        await send_log_message("\n".join(lines))
    except Exception as e:
        logger.error(f"Failed to send bug report: {e}")


# =========================
# Telegram application manager
# =========================
class TelegramAppManager:
    """
    מנהל את אובייקט Application של python-telegram-bot.
    דואג שניצור את האפליקציה פעם אחת בלבד, ונגדיר handlers פעם אחת.
    """

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
            CommandHandler("start", start_command),
            CommandHandler("whoami", whoami_command),
            CommandHandler("stats", stats_command),
            CommandHandler("help", help_command),

            # אדמין – בסיס + מתקדם
            CommandHandler("admin", admin_command),
            CommandHandler("pending", pending_command),
            CommandHandler("approve", approve_command),
            CommandHandler("reject", reject_command),
            CommandHandler("set_price", set_price_command),
            CommandHandler("admin_wallet", admin_wallet_command),
            CommandHandler("admin_user", admin_user_command),
            CommandHandler("admin_credit", admin_credit_command),

            # ארנק & סטייקינג & הפניות
            CommandHandler("wallet", wallet_command),
            CommandHandler("send_slh", send_slh_command),
            CommandHandler("stake", stake_command),
            CommandHandler("mystakes", mystakes_command),
            CommandHandler("my_link", my_link_command),
            CommandHandler("my_referrals", my_referrals_command),
            CommandHandler("portfolio", portfolio_command),

            # ארנק חיצוני אישי (בדיקות בלבד)
            CommandHandler("onchain_wallet", onchain_wallet_command),
            CommandHandler("set_wallet", set_wallet_command),

            CallbackQueryHandler(callback_query_handler),
            MessageHandler(filters.PHOTO | filters.Document.ALL, payment_proof_handler),
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
            if cls._instance is not None:
                await cls._instance.stop()
                await cls._instance.shutdown()
        except Exception as e:
            logger.error(f"Error during Telegram shutdown: {e}")


# =========================
# UI builders
# =========================
def build_start_keyboard(has_paid: bool) -> InlineKeyboardMarkup:
    """
    תפריט התחלה:
    1. מה אני מקבל?
    2. איך לשלם ולשלוח אישור (תפריט אמצעי תשלום)
    3. כניסה לקבוצת העסקים (אם אושר)
    4. מידע למשקיעים
    5. האזור האישי שלי
    6. תמיכה
    7. דיווח באג
    """
    buttons: List[List[InlineKeyboardButton]] = []

    buttons.append(
        [InlineKeyboardButton("ℹ️ מה אני מקבל?", callback_data="info_benefits")]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                "💳 איך לשלם ולשלוח אישור", callback_data="send_proof_menu"
            )
        ]
    )

    group_url = safe_get_url(
        Config.BUSINESS_GROUP_URL or Config.GROUP_STATIC_INVITE, Config.LANDING_URL
    )
    if has_paid:
        buttons.append(
            [InlineKeyboardButton("👥 כניסה לקבוצת העסקים", url=group_url)]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "📈 מידע למשקיעים", callback_data="open_investor"
            )
        ]
    )

    buttons.append(
        [
            InlineKeyboardButton(
                "👤 האזור האישי שלי", callback_data="open_personal_area"
            )
        ]
    )

    support_url = safe_get_url(
        Config.SUPPORT_GROUP_LINK or Config.LANDING_URL, Config.LANDING_URL
    )
    buttons.append(
        [InlineKeyboardButton("🆘 תמיכה / צור קשר", url=support_url)]
    )

    # כפתור דיווח באג גלובלי – feature_id=start_menu
    buttons.append(
        [
            InlineKeyboardButton(
                "🐞 דיווח על תקלה / באג", callback_data="report_bug:start_menu"
            )
        ]
    )

    return InlineKeyboardMarkup(buttons)


def build_payment_menu_keyboard() -> InlineKeyboardMarkup:
    """
    תפריט לכל אמצעי התשלום. כל כפתור פותח הסבר מפורט
    איך לשלם ואיך לשלוח אישור.
    """
    rows: List[List[InlineKeyboardButton]] = []

    rows.append([InlineKeyboardButton("🏦 העברה בנקאית", callback_data="pay_bank")])

    if Config.PAYBOX_URL:
        rows.append(
            [InlineKeyboardButton("📲 תשלום PayBox", callback_data="pay_paybox")]
        )
    if Config.BIT_URL:
        rows.append(
            [InlineKeyboardButton("📲 תשלום Bit", callback_data="pay_bit")]
        )
    if Config.PAYPAL_URL:
        rows.append(
            [InlineKeyboardButton("🌍 תשלום PayPal", callback_data="pay_paypal")]
        )
    if Config.TON_WALLET_ADDRESS:
        rows.append(
            [InlineKeyboardButton("🔐 תשלום בקריפטו (TON)", callback_data="pay_ton")]
        )

    rows.append(
        [InlineKeyboardButton("🔙 חזרה לתפריט הראשי", callback_data="back_to_main")]
    )

    return InlineKeyboardMarkup(rows)


# =========================
# Telegram handlers
# =========================
async def send_start_screen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    referrer: Optional[int] = None,
) -> None:
    """
    מסך הפתיחה המרכזי. מזהה הפניות, בונה מסך שיווקי
    ומציג למשתמש כפתורים רלוונטיים.
    """
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    # register referral & update profile snapshot
    register_referral(user.id, referrer)
    upsert_profile(user.id, user.username, user.full_name)

    # load title & body
    title = load_message_block("START_TITLE", "🚀 ברוך הבא ל-SLHNET!")
    body = load_message_block(
        "START_BODY",
        (
            "ברוך הבא לשער הדיגיטלי של קהילת SLHNET.\n"
            "כאן אתה מצטרף לקהילת עסקים, מקבל גישה לארנקים, חוזים חכמים, "
            "NFT וקבלת תשלומים – הכל סביב תשלום חד־פעמי של *39 ₪*."
        ),
    )

    # send banner
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

    # check if paid
    has_paid = False
    try:
        has_paid = has_approved_payment(user.id)
    except Exception as e:
        logger.error(f"Error checking approved payment for user {user.id}: {e}")

    keyboard = build_start_keyboard(has_paid)
    await chat.send_message(text=body, reply_markup=keyboard, parse_mode="Markdown")

    # log
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
        except Exception:
            logger.warning(f"Invalid referrer param: {context.args[0]}")
    await send_start_screen(update, context, referrer)


async def whoami_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    refs = load_referrals()
    ref_data = refs.get("users", {}).get(str(user.id), {})
    text = (
        "👤 **פרטי המשתמש שלך:**\n"
        f"🆔 ID: `{user.id}`\n"
        f"📛 שם משתמש: @{user.username or 'לא מוגדר'}\n"
        f"🔰 שם מלא: {user.full_name}\n"
        f"🔄 מספר הפניות: {ref_data.get('referral_count', 0)}\n"
        f"📅 הצטרף: {ref_data.get('joined_at', 'לא ידוע')}"
    )
    await chat.send_message(text=text, parse_mode="Markdown")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    refs = load_referrals()
    stats = refs.get("statistics", {})
    total_users = stats.get("total_users", 0)
    users_count = len(refs.get("users", {}))
    total_refs = sum(
        u.get("referral_count", 0) for u in refs.get("users", {}).values()
    )

    text = (
        "📊 סטטיסטיקות קהילה:\n"
        f"👥 סה״כ משתמשים: {total_users}\n"
        f"📈 משתמשים פעילים: {users_count}\n"
        f"🔄 הפניות כוללות: {total_refs}"
    )
    await chat.send_message(text=text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    פקודת עזרה ידידותית למשתמשים.
    """
    chat = update.effective_chat
    if not chat:
        return

    text = (
        "🤖 *עזרה – SLHNET Bot*\n\n"
        "פקודות בסיסיות:\n"
        "• /start – תפריט ראשי והצטרפות\n"
        "• /my_link – קישור אישי להזמנת חברים\n"
        "• /my_referrals – רשימת הפניות שלך\n"
        "• /portfolio – סקירה של הארנק, סטייקינג והפניות\n"
        "• /wallet – פירוט ארנק SLH פנימי + חיצוני (בדיקות)\n"
        "• /mystakes – פירוט עמדות סטייקינג\n"
        "• /onchain_wallet – צפייה בארנק החיצוני (BSC/TON)\n"
        "• /set_wallet – הגדרת ארנק חיצוני (בדיקות בלבד)\n\n"
        "פקודות למנהלים בלבד:\n"
        "• /admin – פאנל ניהול\n"
        "• /pending – תשלומים ממתינים\n"
        "• /approve <user_id> – אישור תשלום + מינט SLH פנימי\n"
        "• /reject <user_id> <סיבה> – דחיית תשלום\n"
        "• /set_price <מחיר_ש\"ח_ל-SLH_1> – עדכון שער SLH\n"
        "• /admin_wallet – סקירת ארנק מערכת ושערים\n"
        "• /admin_user <user_id> – צילום מצב משתמש\n"
        "• /admin_credit <user_id> <amount_slh> – קרדיט ידני של SLH\n"
    )
    await chat.send_message(text=text, parse_mode="Markdown")


# ===== Payments & admin =====
async def payment_proof_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    קבלת צילום/קובץ כאישור תשלום והעברת הלוג לקבוצת הניהול.
    """
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
    elif "bank" in text_lower or "בנק" in text_lower or "העברה" in text_lower:
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
                chat_id=admin_chat_id,
                text=admin_text,
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.error(f"Error sending payment log to admin group: {e}")

    await chat.send_message(
        "📥 קיבלנו את אישור התשלום שלך!\n"
        "ההודעה הועברה לצוות הניהול. לאחר אישור, ישלח אליך קישור לקבוצת העסקים + זיכוי SLH בארנק הפנימי."
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    פאנל ניהול בסיסי + מתקדם למנהלים בלבד.
    """
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    if not is_admin(user.id):
        await chat.send_message("❌ הפקודה /admin מיועדת למנהלי המערכת בלבד.")
        return

    approval_stats = get_approval_stats() or {}
    reserve_stats = get_reserve_stats() or {}
    price_nis, entry_nis = get_current_price_and_entry()
    cfg = load_dynamic_config()

    text_lines = [
        "🛠 *פאנל ניהול SLHNET – תקציר מיידי*",
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
        "💎 *שער SLH ודינמיקת מינט:*",
        f" - מחיר נוכחי ל-SLH 1: ~{format_decimal_pretty(price_nis)} ₪",
        f" - סכום כניסה (NIS_ENTRY_AMOUNT): ~{format_decimal_pretty(entry_nis)} ₪",
        f" - SLH מחושב לכל כניסה: ~{format_decimal_pretty(compute_slh_for_entry(price_nis, entry_nis))} SLH",
        f" - סך SLH שחולקו ללקוחות: ~{format_decimal_pretty(Decimal(str(cfg.get('total_slh_minted', 0.0))))} SLH",
        "",
        "📋 *פקודות ניהול זמינות (לשימושך ולמסמך ללקוחות):*",
        " - /pending  – רשימת תשלומים ממתינים",
        " - /approve <user_id>  – אישור תשלום: סטטוס + שליחת קישור לקבוצה + מינט SLH אוטומטי",
        " - /reject <user_id> <סיבה>  – דחיית תשלום: סטטוס + הודעה ללקוח",
        "",
        " - /set_price <מחיר_ש\"ח_ל-SLH_1>",
        "     מעדכן את שער SLH בש\"ח. מכאן ואילך חישוב הכמות ללקוח משתנה בהתאם.",
        "",
        " - /admin_wallet",
        "     מציג תמונת מצב מערכתית: שער נוכחי, סכום כניסה, סך SLH שחולקו, כתובות ארנק חם / קר.",
        "",
        " - /admin_user <user_id>",
        "     מציג פרטי משתמש: ארנק פנימי, סטייקינג, הפניות, ארנק חיצוני – לצורך תמונת מצב לפני החלטות.",
        "",
        " - /admin_credit <user_id> <amount_slh>",
        "     מאפשר לתת זיכוי SLH פנימי ידני למשתמש (לדוגמה: בונוס, תיקון טכני, מתנה).",
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
            f"• user_id={p['user_id']} | username=@{p['username'] or 'לא ידוע'} | שיטה={p['pay_method']} | id={p['id']}"
        )

    await chat.send_message("\n".join(lines), parse_mode="Markdown")


async def auto_mint_slh_for_entry(user_id: int) -> Optional[Decimal]:
    """
    מינט SLH אוטומטי למשתמש בעקבות תשלום מאושר.
    משתמש במחיר SLH נוכחי ובסכום כניסה NIS_ENTRY_AMOUNT.
    """
    try:
        price_nis, entry_nis = get_current_price_and_entry()
        amount_slh = compute_slh_for_entry(price_nis, entry_nis)
        if amount_slh <= 0:
            logger.warning("auto_mint_slh_for_entry: computed amount <= 0, skipping")
            return None

        reason = (
            f"Entry payment {format_decimal_pretty(entry_nis)} NIS at price "
            f"{format_decimal_pretty(price_nis)} NIS per SLH"
        )

        # מינט בפועל דרך מודול הארנקים
        try:
            mint_slh_from_payment(user_id, amount_slh, reason)
        except TypeError:
            # אם הפונקציה מוגדרת בגירסה ישנה עם פחות פרמטרים – נתמוך גם בה
            mint_slh_from_payment(user_id, amount_slh)

        record_mint_amount(amount_slh)

        await send_log_message(
            "💎 מינט SLH אוטומטי בעקבות תשלום מאושר:\n"
            f"👤 user_id={user_id}\n"
            f"📊 כמות: {format_decimal_pretty(amount_slh)} SLH\n"
            f"🏷 סיבה: {reason}"
        )

        return amount_slh
    except Exception as e:
        logger.error(f"auto_mint_slh_for_entry error for user {user_id}: {e}")
        return None


async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    אישור תשלום ידני לפי user_id – למנהלים בלבד.
    שולח למשתמש גם קישור לקבוצה וגם קישור אישי להפניות + מינט SLH.
    """
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
        ensure_internal_wallet(target_id, None)
    except Exception as e:
        logger.error(f"Error updating payment status for {target_id}: {e}")
        await chat.send_message("❌ שגיאה בעדכון סטטוס התשלום.")
        return

    # מינט SLH לפי שער נוכחי
    minted = await auto_mint_slh_for_entry(target_id)
    minted_str = format_decimal_pretty(minted) if minted else None

    group_url = safe_get_url(
        Config.BUSINESS_GROUP_URL or Config.GROUP_STATIC_INVITE, Config.LANDING_URL
    )
    referral_link = f"https://t.me/{Config.BOT_USERNAME}?start={target_id}"

    try:
        extra_slh = (
            f"\n\nכחלק מההצטרפות קיבלת *{minted_str}* SLH פנימי לארנק שלך."
            if minted_str
            else ""
        )
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                "✅ התשלום שלך אושר!\n\n"
                "הנה הקישור להצטרפות לקהילת העסקים שלנו:\n"
                f"{group_url}\n\n"
                "בנוסף, זה הקישור האישי שלך להזמנת חברים:\n"
                f"{referral_link}\n"
                f"{extra_slh}\n\n"
                "תוכל תמיד לקבל את הקישור האישי שוב בפקודה /my_link.\n"
                "ברוך הבא 🙌"
            ),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Error sending approval message to user {target_id}: {e}")

    admin_msg = (
        f"✅ התשלום של המשתמש {target_id} אושר ונשלח לו קישור לקבוצה + לינק אישי."
    )
    if minted_str:
        admin_msg += f"\nנמינטו לו {minted_str} SLH פנימיים."

    await chat.send_message(admin_msg)


async def reject_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    דחיית תשלום ידנית לפי user_id – למנהלים בלבד.
    """
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

    await chat.send_message(f"🚫 התשלום של המשתמש {target_id} נדחה ונשלחה לו הודעה.")


async def set_price_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /set_price <מחיר_ש\"ח_ל-SLH_1>
    עדכון שער SLH – משפיע על כמות ה-SLH שמקבל כל לקוח בכניסה (39 ₪ כברירת מחדל).
    """
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    if not is_admin(user.id):
        await chat.send_message("❌ הפקודה /set_price מיועדת למנהלי המערכת בלבד.")
        return

    if not context.args:
        price_nis, entry_nis = get_current_price_and_entry()
        await chat.send_message(
            "ℹ️ שער SLH נוכחי:\n"
            f"• מחיר ל-SLH 1: {format_decimal_pretty(price_nis)} ₪\n"
            f"• סכום כניסה: {format_decimal_pretty(entry_nis)} ₪\n"
            f"• SLH לכל כניסה: {format_decimal_pretty(compute_slh_for_entry(price_nis, entry_nis))} SLH\n\n"
            "כדי לעדכן:\n"
            "/set_price <מחיר_ש\"ח_ל-SLH_1>\n"
            "לדוגמה: /set_price 500",
            parse_mode="Markdown",
        )
        return

    try:
        new_price = Decimal(context.args[0].replace(",", "."))
        if new_price <= 0:
            raise InvalidOperation
    except InvalidOperation:
        await chat.send_message("מחיר לא תקין. השתמש במספר גדול מאפס, לדוגמה: 444")
        return

    cfg = load_dynamic_config()
    old_price = Decimal(str(cfg.get("slh_nis_price", float(DEFAULT_SLH_PRICE))))
    cfg["slh_nis_price"] = float(new_price)
    save_dynamic_config(cfg)

    await send_log_message(
        "⚙️ עדכון שער SLH:\n"
        f"👤 admin_id={user.id}\n"
        f"ישן: {format_decimal_pretty(old_price)} ₪\n"
        f"חדש: {format_decimal_pretty(new_price)} ₪"
    )

    price_nis, entry_nis = get_current_price_and_entry()
    await chat.send_message(
        "✅ שער SLH עודכן בהצלחה.\n\n"
        f"מחיר חדש ל-SLH 1: *{format_decimal_pretty(price_nis)} ₪*\n"
        f"סכום כניסה: *{format_decimal_pretty(entry_nis)} ₪*\n"
        f"SLH מחושב לכל כניסה: *{format_decimal_pretty(compute_slh_for_entry(price_nis, entry_nis))}* SLH",
        parse_mode="Markdown",
    )


async def admin_wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    סקירת מצב מערכת: שערים, כמות SLH שחולקה, וארנק חם/קר.
    """
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    if not is_admin(user.id):
        await chat.send_message("❌ הפקודה /admin_wallet מיועדת למנהלי המערכת בלבד.")
        return

    price_nis, entry_nis = get_current_price_and_entry()
    cfg = load_dynamic_config()
    total_minted = Decimal(str(cfg.get("total_slh_minted", 0.0)))

    hot = Config.HOT_WALLET_ADDRESS or "לא הוגדר (HOT_WALLET_ADDRESS)"
    cold = Config.COLD_WALLET_ADDRESS or "לא הוגדר (COLD_WALLET_ADDRESS)"

    lines = [
        "💼 *ארנק מערכת – תמונת מצב*",
        "",
        "💎 *שער SLH נוכחי:*",
        f"• מחיר ל-SLH 1: *{format_decimal_pretty(price_nis)} ₪*",
        f"• סכום כניסה: *{format_decimal_pretty(entry_nis)} ₪*",
        f"• SLH לכל כניסה: *{format_decimal_pretty(compute_slh_for_entry(price_nis, entry_nis))}* SLH",
        "",
        f"📊 *סך SLH שחולקו ללקוחות:* *{format_decimal_pretty(total_minted)}* SLH",
        "",
        "🔥 *ארנק חם (On-chain):*",
        f"• {hot}",
        "",
        "❄️ *ארנק קר / כספת קהילה:*",
        f"• {cold}",
        "",
        "להגדרת הכתובות, עדכן משתני סביבה בשרת:\n"
        "HOT_WALLET_ADDRESS, COLD_WALLET_ADDRESS",
    ]

    await chat.send_message("\n".join(lines), parse_mode="Markdown")


async def admin_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /admin_user <user_id>
    מציג למנהל תמונת מצב על משתמש: ארנק, סטייקינג, הפניות + ארנק חיצוני (BSC/TON).
    """
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    if not is_admin(user.id):
        await chat.send_message("❌ הפקודה /admin_user מיועדת למנהלי המערכת בלבד.")
        return

    if not context.args:
        await chat.send_message("שימוש: /admin_user <user_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await chat.send_message("user_id לא תקין.")
        return

    try:
        ensure_internal_wallet(target_id, None)
        overview = get_wallet_overview(target_id) or {}
        stakes = get_user_stakes(target_id) or []
    except Exception as e:
        logger.error(f"admin_user error for {target_id}: {e}")
        await chat.send_message("❌ לא ניתן לטעון את נתוני המשתמש.")
        return

    # ארנק פנימי
    try:
        balance = Decimal(str(overview.get("balance_slh", "0")))
    except Exception:
        balance = Decimal("0")

    wallet_id = overview.get("wallet_id", "?")

    total_staked = Decimal("0")
    for s in stakes:
        try:
            total_staked += Decimal(str(s.get("amount_slh", "0")))
        except Exception:
            continue

    # הפניות
    refs = load_referrals()
    udata = refs.get("users", {}).get(str(target_id), {})
    my_ref_count = udata.get("referral_count", 0)
    joined_at = udata.get("joined_at", "לא ידוע")
    referrer = udata.get("referrer", "N/A")

    price_nis, _ = get_current_price_and_entry()
    wallet_value_nis = balance * price_nis if price_nis > 0 else Decimal("0")

    # ארנק חיצוני אישי
    onchain = get_onchain_wallet(target_id)
    bsc_addr = onchain.get("bsc") or "לא מוגדר"
    ton_addr = onchain.get("ton") or "לא מוגדר"
    updated_at = onchain.get("updated_at") or "N/A"

    lines = [
        "🔍 *צילום מצב משתמש – עבור אדמין*",
        "",
        f"🆔 user_id: `{target_id}`",
        f"🔗 referrer: {referrer}",
        f"📅 הצטרף: {joined_at}",
        "",
        "💼 *ארנק פנימי:*",
        f"• wallet_id: `{wallet_id}`",
        f"• יתרה זמינה: *{format_decimal_pretty(balance)}* SLH",
        f"• בסטייקינג: *{format_decimal_pretty(total_staked)}* SLH",
        f"• שווי משוער בש\"ח (לפי שער נוכחי): ~{format_decimal_pretty(wallet_value_nis)} ₪",
        "",
        "👥 *הפניות:*",
        f"• סה\"כ הפניות על שמו: *{my_ref_count}*",
        "",
        f"🔢 מספר עמדות סטייקינג: {len(stakes)}",
        "",
        "🌐 *ארנק חיצוני אישי (בדיקות בלבד):*",
        f"• BSC / BNB Chain: `{bsc_addr}`",
        f"• TON: `{ton_addr}`",
        f"🕒 עודכן לאחרונה: {updated_at}",
    ]

    await chat.send_message("\n".join(lines), parse_mode="Markdown")


async def admin_credit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /admin_credit <user_id> <amount_slh>
    קרדיט ידני של SLH פנימי למשתמש – לדוגמה בונוס / תיקון.
    """
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    if not is_admin(user.id):
        await chat.send_message("❌ הפקודה /admin_credit מיועדת למנהלי המערכת בלבד.")
        return

    if len(context.args) < 2:
        await chat.send_message("שימוש: /admin_credit <user_id> <amount_slh>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await chat.send_message("user_id לא תקין.")
        return

    try:
        amount = Decimal(context.args[1].replace(",", "."))
        if amount <= 0:
            raise InvalidOperation
    except InvalidOperation:
        await chat.send_message("סכום SLH לא תקין. השתמש במספר גדול מאפס.")
        return

    try:
        ensure_internal_wallet(target_id, None)
        reason = f"Manual admin credit by {user.id}"
        try:
            mint_slh_from_payment(target_id, amount, reason)
        except TypeError:
            mint_slh_from_payment(target_id, amount)

        record_mint_amount(amount)

        await context.bot.send_message(
            chat_id=target_id,
            text=(
                "💎 קיבלת זיכוי SLH מהמנהל.\n"
                f"סכום: *{format_decimal_pretty(amount)}* SLH\n"
                "הזיכוי הועבר לארנק הפנימי שלך בבוט.",
            ),
            parse_mode="Markdown",
        )

        await chat.send_message(
            f"✅ זוכו למשתמש {target_id} *{format_decimal_pretty(amount)}* SLH פנימיים.",
            parse_mode="Markdown",
        )

        await send_log_message(
            "💎 קרדיט אדמין:\n"
            f"👤 admin_id={user.id}\n"
            f"👤 target_id={target_id}\n"
            f"📊 amount={format_decimal_pretty(amount)} SLH"
        )
    except Exception as e:
        logger.error(f"admin_credit error for {target_id}: {e}")
        await chat.send_message("❌ שגיאה בעת יצירת הקרדיט.")


# ===== Wallet & staking =====
async def wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    מציג למשתמש את ארנק ה-SLH הפנימי שלו + סטייקינג + מידע SLH/ש\"ח
    + ארנק חם/קר של המערכת + ארנק חיצוני אישי (BSC/TON – בדיקות בלבד).
    """
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    # === ארנק פנימי + סטייקינג ===
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

    try:
        balance = Decimal(str(overview.get("balance_slh", "0")))
    except Exception:
        balance = Decimal("0")

    wallet_id = overview.get("wallet_id", "?")

    total_staked = Decimal("0")
    for s in stakes:
        try:
            total_staked += Decimal(str(s.get("amount_slh", "0")))
        except Exception:
            continue

    balance_str = format_decimal_pretty(balance)
    total_staked_str = format_decimal_pretty(total_staked)

    price_nis, _ = get_current_price_and_entry()
    value_nis = balance * price_nis if price_nis > 0 else Decimal("0")

    # === ארנקי מערכת (חם/קר) ===
    hot = Config.HOT_WALLET_ADDRESS or "טרם הוגדר (HOT_WALLET_ADDRESS)"
    cold = Config.COLD_WALLET_ADDRESS or "טרם הוגדר (COLD_WALLET_ADDRESS)"

    # === ארנק חיצוני אישי (On-chain) – בדיקות בלבד ===
    onchain = get_onchain_wallet(user.id)
    bsc_addr = onchain.get("bsc") or "לא מוגדר"
    ton_addr = onchain.get("ton") or "לא מוגדר"

    msg = (
        "💼 *ארנק SLH פנימי*\n\n"
        f"🆔 ID ארנק פנימי: `{wallet_id}`\n"
        f"💰 יתרה זמינה: *{balance_str}* SLH\n"
        f"🔒 סה״כ בסטייקינג: {total_staked_str} SLH\n"
        f"💱 שווי משוער בש\"ח (לפי שער נוכחי): ~{format_decimal_pretty(value_nis)} ₪\n\n"
        "כדי לפתוח סטייקינג חדש:\n"
        "*/stake <סכום_SLH> <ימי_נעילה>* לדוגמה:\n"
        "`/stake 100 30` – סטייקינג על 100 SLH ל-30 ימים.\n\n"
        "לצפייה בכל הסטייקים הפעילים:\n"
        "השתמש ב-/mystakes.\n\n"
        "🔥 *ארנק חם של המערכת (On-chain / BSC / TON):*\n"
        f"{hot}\n\n"
        "❄️ *ארנק קר / כספת קהילה של המערכת:*\n"
        f"{cold}\n\n"
        "🌐 *ארנק חיצוני אישי (בדיקות בלבד)*\n"
        f"• BSC / BNB Chain: `{bsc_addr}`\n"
        f"• TON: `{ton_addr}`\n\n"
        "לעדכון הארנק החיצוני:\n"
        "`/set_wallet <כתובת_BSC|-> [כתובת_TON|-]`\n"
        "_נכון לעכשיו החיבור החיצוני משמש להצגה ובדיקות בלבד (אין שליחה אמיתית מהבוט)._"
    )

    await chat.send_message(text=msg, parse_mode="Markdown")


async def send_slh_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    העברה פנימית של SLH בין משתמשים.
    /send_slh <amount> <user_id>
    """
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

    await chat.send_message(f"✅ הועברו {amount} SLH פנימיים למשתמש {to_user_id}.")


async def stake_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    פתיחת סטייקינג בסיסי: /stake <amount> [days]
    """
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    if not context.args:
        await chat.send_message(
            "שימוש: /stake <amount> [days]. ברירת מחדל ימים: "
            f"{Config.STAKING_DEFAULT_DAYS}, APY: {Config.STAKING_DEFAULT_APY}%."
        )
        return

    amount_str = context.args[0]
    days = Config.STAKING_DEFAULT_DAYS
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

    ok, msg = create_stake_position(user.id, amount, Config.STAKING_DEFAULT_APY, days)
    if not ok:
        await chat.send_message(f"❌ סטייקינג נכשל: {msg}")
        return

    await chat.send_message(
        f"✅ פתחת סטייקינג על {amount} SLH ל-{days} ימים.\n"
        f"APY נוכחי: {Config.STAKING_DEFAULT_APY}%."
    )


async def mystakes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    פירוט עמדות הסטייקינג של המשתמש.
    """
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
        amount = format_decimal_pretty(Decimal(str(st.get("amount_slh", "0"))))
        apy = st.get("apy", Decimal("0"))
        lock_days = st.get("lock_days", 0)
        started = st.get("started_at")
        lines.append(
            f"• {amount} SLH | {apy}% | {lock_days} ימים | סטטוס: {status} | התחלה: {started}"
        )

    await chat.send_message("\n".join(lines), parse_mode="Markdown")


# ===== Referrals & personal area =====
async def my_link_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    מחזיר למשתמש את הקישור האישי להפניות.
    """
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    # ensure user exists in referrals db
    register_referral(user.id, None)

    link = f"https://t.me/{Config.BOT_USERNAME}?start={user.id}"
    text = (
        "🔗 *הקישור האישי שלך להזמנת חברים:*\n\n"
        f"{link}\n\n"
        "כל מי שנכנס דרך הקישור הזה ונרשם – נרשם על שמך במערכת ההפניות."
    )
    await chat.send_message(text=text, parse_mode="Markdown")


async def my_referrals_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    פירוט הפניות של המשתמש.
    """
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    refs = load_referrals()
    udata = refs.get("users", {}).get(str(user.id), {})
    count = udata.get("referral_count", 0)
    referred_ids = get_user_referrals(user.id)

    lines = [
        "👥 *הפניות על שמך:*",
        f"🔢 סה\"כ הפניות: {count}",
        "",
        "רשימה (עד 10 ראשונים, לפי ID):",
    ]

    if not referred_ids:
        lines.append("אין עדיין רשומות.\n\nהמשך להזמין אנשים דרך הקישור האישי שלך!")
    else:
        for rid in referred_ids[:10]:
            lines.append(f"• user_id = {rid}")
        lines.append("\nהמשך להזמין אנשים דרך הקישור האישי שלך!")

    await chat.send_message("\n".join(lines), parse_mode="Markdown")


async def portfolio_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    סיכום אזור אישי – ארנק, סטייקינג והפניות.
    """
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    try:
        ensure_internal_wallet(user.id, user.username or None)
        overview = get_wallet_overview(user.id) or {}
        stakes = get_user_stakes(user.id) or []
    except Exception as e:
        logger.error(f"portfolio_command error: {e}")
        await chat.send_message("❌ לא ניתן לטעון את הנתונים כרגע.")
        return

    try:
        balance = Decimal(str(overview.get("balance_slh", "0")))
    except Exception:
        balance = Decimal("0")

    total_staked = Decimal("0")
    total_expected = Decimal("0")
    for s in stakes:
        try:
            amt = Decimal(str(s.get("amount_slh", "0")))
            apy = Decimal(str(s.get("apy", "0")))
            total_staked += amt
            total_expected += amt + (amt * apy / Decimal("100"))
        except Exception:
            continue

    balance_str = format_decimal_pretty(balance)
    total_staked_str = format_decimal_pretty(total_staked)
    total_expected_str = format_decimal_pretty(total_expected)

    refs = load_referrals()
    udata = refs.get("users", {}).get(str(user.id), {})
    my_ref_count = udata.get("referral_count", 0)

    price_nis, _ = get_current_price_and_entry()
    value_nis = balance * price_nis if price_nis > 0 else Decimal("0")

    text = (
        "📊 *האזור האישי שלך – SLHNET*\n\n"
        "💼 *ארנק פנימי:*\n"
        f"• יתרה זמינה: *{balance_str}* SLH\n"
        f"• בסטייקינג: *{total_staked_str}* SLH\n"
        f"• רווח משוער מכל הסטייקים (לסוף התקופות): ~{total_expected_str} SLH\n"
        f"• שווי משוער בש\"ח (לפי שער נוכחי): ~{format_decimal_pretty(value_nis)} ₪\n\n"
        "👥 *הפניות:*\n"
        f"• סה\"כ הפניות על שמך: *{my_ref_count}*\n"
        "• קבל לינק אישי בפקודה: /my_link\n"
        "• פירוט הפניות: /my_referrals\n\n"
        "🔗 *כלי עזר:*\n"
        "• /wallet – פירוט ארנק SLH\n"
        "• /mystakes – פירוט סטייקינג\n"
        "• /my_link – קישור אישי להזמנת חברים\n"
        "• /my_referrals – פירוט הפניות\n"
        "• /onchain_wallet – פירוט ארנק חיצוני (בדיקות בלבד)\n"
    )

    await chat.send_message(text=text, parse_mode="Markdown")


async def set_wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /set_wallet <bsc_address|-> [ton_address|-]
    מגדיר למשתמש את הכתובות החיצוניות שלו (בדיקות בלבד כרגע).
    שימוש ב '-' במקום כתובת – מאפס את השדה.
    """
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    if not context.args:
        await chat.send_message(
            "שימוש: /set_wallet <כתובת_BSC|-> [כתובת_TON|-]\n\n"
            "דוגמאות:\n"
            "• /set_wallet 0x1234... UQxxxxx...\n"
            "• /set_wallet 0x1234...      (רק BSC)\n"
            "• /set_wallet - UQxxxxx...    (מאפס BSC ומשאיר/מגדיר TON)\n"
            "• /set_wallet - -             (מאפס את שתי הכתובות)"
        )
        return

    bsc_arg = context.args[0]
    ton_arg = context.args[1] if len(context.args) > 1 else None

    rec = set_onchain_wallet(
        user_id=user.id,
        bsc_address=bsc_arg,
        ton_address=ton_arg,
    )

    bsc = rec.get("bsc") or "לא מוגדר"
    ton = rec.get("ton") or "לא מוגדר"

    await chat.send_message(
        "🌐 ארנק חיצוני עודכן (בדיקות בלבד):\n\n"
        f"• BSC / BNB Chain: `{bsc}`\n"
        f"• TON: `{ton}`\n\n"
        "_נכון לעכשיו זה משמש להצגה ובדיקות בלבד – אין שליחה אמיתית מהבוט._",
        parse_mode="Markdown",
    )

    await send_log_message(
        "🌐 עדכון ארנק חיצוני ע\"י משתמש:\n"
        f"👤 user_id={user.id}\n"
        f"BSC={bsc}\n"
        f"TON={ton}"
    )


async def onchain_wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /onchain_wallet – מציג למשתמש רק את הארנק החיצוני שלו (BSC/TON).
    """
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    rec = get_onchain_wallet(user.id)
    bsc = rec.get("bsc") or "לא מוגדר"
    ton = rec.get("ton") or "לא מוגדר"
    updated_at = rec.get("updated_at") or "N/A"

    await chat.send_message(
        "🌐 *ארנק חיצוני (בדיקות בלבד)*\n\n"
        f"• BSC / BNB Chain: `{bsc}`\n"
        f"• TON: `{ton}`\n"
        f"🕒 עודכן לאחרונה: {updated_at}\n\n"
        "לעדכון:\n"
        "`/set_wallet <כתובת_BSC|-> [כתובת_TON|-]`\n\n"
        "_כרגע בשימוש להצגה ובדיקות בלבד – אין ביצוע טרנזאקציות אמיתיות מהבוט._",
        parse_mode="Markdown",
    )


# ===== Callback queries =====
async def handle_investor_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    investor_text = load_message_block(
        "INVESTOR_INFO",
        (
            "📈 **מידע למשקיעים**\n\n"
            "מערכת SLHNET מחברת בין טלגרם, חוזים חכמים על Binance Smart Chain, "
            "קבלות דיגיטליות ו-NFT, כך שכל עסקה מתועדת וניתנת למעקב.\n\n"
            "ניתן להצטרף כשותף, להחזיק טוקן SLH ולקבל חלק מהתנועה במערכת."
        ),
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔙 חזרה לתפריט הראשי", callback_data="back_to_main"
                )
            ],
            [
                InlineKeyboardButton(
                    "🐞 דיווח באג במסך זה",
                    callback_data="report_bug:investor_screen",
                )
            ],
        ]
    )
    await query.edit_message_text(
        text=investor_text, reply_markup=keyboard, parse_mode="Markdown"
    )


def build_payment_instructions_text(method: str) -> str:
    """
    בונה טקסט מסודר לכל אפשרויות התשלום והוראות שליחת האישור.
    """
    base_footer = (
        "\nלאחר שביצעת תשלום באחד האמצעים למעלה:\n"
        "1️⃣ שמור צילום מסך ברור של אישור התשלום (או קובץ PDF / מסמך מהבנק).\n"
        "2️⃣ שלח את צילום המסך כאן בצ׳אט עם הבוט.\n"
        "3️⃣ המערכת תעביר את האישור אוטומטית לקבוצת הניהול.\n\n"
        "אחרי שהאדמין יאשר – תקבל קישור לקבוצת העסקים + זיכוי SLH בארנק הפנימי."
    )

    if method == "bank":
        return (
            "🏦 *תשלום בהעברה בנקאית*\n\n"
            "פרטי החשבון:\n"
            "בנק הפועלים\n"
            "סניף כפר גנים (153)\n"
            "חשבון 73462\n"
            "המוטב: קאופמן צביקה\n"
            + base_footer
        )
    if method == "paybox":
        return (
            "📲 *תשלום ב-PayBox*\n\n"
            f"השתמש בלינק הזה לתשלום 39 ₪:\n{Config.PAYBOX_URL}\n"
            + base_footer
        )
    if method == "bit":
        return (
            "📲 *תשלום ב-Bit*\n\n"
            f"השתמש בלינק הזה לתשלום 39 ₪:\n{Config.BIT_URL}\n"
            + base_footer
        )
    if method == "paypal":
        return (
            "🌍 *תשלום ב-PayPal*\n\n"
            f"השתמש בלינק הבא לתשלום 39 ₪:\n{Config.PAYPAL_URL}\n"
            + base_footer
        )
    if method == "ton":
        return (
            "🔐 *תשלום בקריפטו – TON*\n\n"
            "שלח את שווי 39 ₪ בטוקן TON לכתובת:\n"
            f"`{Config.TON_WALLET_ADDRESS}`\n"
            + base_footer
        )
    return "שגיאה: אמצעי תשלום לא ידוע."


async def handle_send_proof_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    מסך מרכזי: איך לשלם ולשלוח אישור – ממנו בוחרים אמצעי תשלום.
    """
    query = update.callback_query
    if not query:
        return
    text = (
        "💳 *איך לשלם ולשלוח אישור*\n\n"
        "בחר אחד מאמצעי התשלום למטה לקבלת הוראות מדויקות.\n"
        "לאחר התשלום, שלח כאן לבוט צילום מסך של האישור."
    )
    keyboard = build_payment_menu_keyboard()
    await query.edit_message_text(text=text, reply_markup=keyboard, parse_mode="Markdown")


async def handle_payment_method_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, method: str
) -> None:
    """
    מסך ספציפי לכל אמצעי תשלום – כולל הסבר מלא.
    """
    query = update.callback_query
    if not query:
        return
    text = build_payment_instructions_text(method)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📤 שלח עכשיו צילום מסך", callback_data="send_proof_menu"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔙 חזרה לאפשרויות תשלום", callback_data="send_proof_menu"
                )
            ],
            [InlineKeyboardButton("🏠 חזרה לתפריט הראשי", callback_data="back_to_main")],
            [
                InlineKeyboardButton(
                    "🐞 דיווח באג במסך זה",
                    callback_data=f"report_bug:pay_{method}",
                )
            ],
        ]
    )
    await query.edit_message_text(text=text, reply_markup=keyboard, parse_mode="Markdown")


async def handle_benefits_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
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
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔙 חזרה לתפריט הראשי", callback_data="back_to_main"
                )
            ],
            [
                InlineKeyboardButton(
                    "🐞 דיווח באג במסך זה",
                    callback_data="report_bug:benefits_screen",
                )
            ],
        ]
    )
    await query.edit_message_text(
        text=benefits_text, reply_markup=keyboard, parse_mode="Markdown"
    )


async def handle_personal_area_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    מסך מקוצר שהולך לכיוון האזור האישי – future-ready לשאלון אישי.
    כרגע מפנה ל-/portfolio.
    """
    query = update.callback_query
    if not query:
        return
    text = (
        "👤 *האזור האישי שלך*\n\n"
        "לקבלת סיכום מלא (ארנק, סטייקינג והפניות):\n"
        "השתמש בפקודה /portfolio בצ׳אט עם הבוט.\n\n"
        "בהמשך נוסיף כאן שאלון קצר כדי להכיר אותך טוב יותר ולחבר אותך\n"
        "למומחים ולעסקים הרלוונטיים לך."
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🏠 חזרה לתפריט הראשי", callback_data="back_to_main"
                )
            ],
            [
                InlineKeyboardButton(
                    "🐞 דיווח באג במסך זה",
                    callback_data="report_bug:personal_area",
                )
            ],
        ]
    )
    await query.edit_message_text(text=text, reply_markup=keyboard, parse_mode="Markdown")


async def handle_bug_report_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    feature_id: str,
) -> None:
    """
    כל callback_data שמתחיל ב-report_bug: מגיע לכאן.
    שולח לוג לקבוצת ה-LOGS ומחזיר למשתמש תודה קצרה.
    """
    query = update.callback_query
    if not query:
        return

    user = query.from_user
    chat = query.message.chat if query.message else None

    await send_bug_report(feature_id, user, chat)

    await query.edit_message_text(
        "🐞 תודה שדיווחת על תקלה!\n"
        "הודעה נשלחה לצוות הפיתוח עם פרטי השלב שבו לחצת.\n"
        "במידת הצורך נחזור אליך דרך הבוט או קבוצת התמיכה."
    )


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
    elif data == "send_proof_menu":
        await handle_send_proof_menu(update, context)
    elif data == "back_to_main":
        await send_start_screen(update, context)
    elif data == "open_personal_area":
        await handle_personal_area_callback(update, context)
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
    elif data.startswith("report_bug:"):
        feature_id = data.split(":", 1)[1] or "unknown_feature"
        await handle_bug_report_callback(update, context, feature_id)
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
            ensure_internal_wallet(target_id, None)
        except Exception as e:
            logger.error(f"Error updating payment status for {target_id}: {e}")
            await query.answer("שגיאה בעדכון סטטוס התשלום.", show_alert=True)
            return

        minted = await auto_mint_slh_for_entry(target_id)
        minted_str = format_decimal_pretty(minted) if minted else None

        group_url = safe_get_url(
            Config.BUSINESS_GROUP_URL or Config.GROUP_STATIC_INVITE,
            Config.LANDING_URL,
        )
        referral_link = f"https://t.me/{Config.BOT_USERNAME}?start={target_id}"

        try:
            extra_slh = (
                f"\n\nכחלק מההצטרפות קיבלת *{minted_str}* SLH פנימי לארנק שלך."
                if minted_str
                else ""
            )
            await context.bot.send_message(
                chat_id=target_id,
                text=(
                    "✅ התשלום שלך אושר!\n\n"
                    "הנה הקישור להצטרפות לקהילת העסקים שלנו:\n"
                    f"{group_url}\n\n"
                    "בנוסף, זה הקישור האישי שלך להזמנת חברים:\n"
                    f"{referral_link}\n"
                    f"{extra_slh}\n\n"
                    "תוכל תמיד לקבל אותו שוב בפקודה /my_link.\n"
                    "ברוך הבא 🙌"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.error(f"Error sending approval message to user {target_id}: {e}")

        admin_msg = (
            f"✅ התשלום של המשתמש {target_id} אושר ונשלח לו קישור לקבוצה + לינק אישי."
        )
        if minted_str:
            admin_msg += f"\nנמינטו לו {minted_str} SLH פנימיים."
        await query.edit_message_text(admin_msg)
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
    else:
        await query.edit_message_text("❌ פעולה לא מוכרת.")


async def echo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    טיפול בהודעות טקסט חופשיות (לא פקודות).
    """
    user = update.effective_user
    text = update.message.text if update.message else ""
    logger.info(f"Message from {user.id if user else '?'}: {text}")
    response = load_message_block(
        "ECHO_RESPONSE",
        (
            "✅ תודה על ההודעה! אנחנו כאן כדי לעזור.\n"
            "השתמש ב-/start כדי לראות את התפריט הראשי."
        ),
    )
    await update.message.reply_text(response)


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    טיפול בפקודות לא מוכרות.
    """
    await update.message.reply_text(
        "❓ פקודה לא מוכרת. השתמש ב-/start כדי לראות את התפריט הזמין."
    )


# =========================
# FastAPI routes
# =========================
@app.get("/api/metrics/finance")
async def finance_metrics():
    """
    סטטוס כספי כולל – הכנסות, רזרבות, נטו ואישורים.
    """
    reserve_stats = get_reserve_stats() or {}
    approval_stats = get_approval_stats() or {}
    return {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "reserve": reserve_stats,
        "approvals": approval_stats,
    }


@app.get("/api/metrics/monthly")
async def monthly_metrics():
    """
    מדד פשוט של תשלומים חודשיים מה-DB (אם ממומש בצד db.py).
    """
    try:
        data = get_monthly_payments() or []
    except Exception as e:
        logger.error(f"Error fetching monthly payments: {e}")
        data = []
    return {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "monthly_payments": data,
    }


@app.get("/api/debug/config", response_model=ConfigSnapshot)
async def debug_config():
    """
    החזרת תמונת קונפיגורציה (ללא סודות) כדי שתוכל לבדוק מה נטען בשרת.
    כולל שער SLH נוכחי ומידע ארנק חם/קר.
    """
    return Config.snapshot()


@app.get("/api/referrals/summary")
async def referrals_summary():
    """
    סיכום הפניות דרך HTTP – future-ready ללוח בקרה חיצוני.
    """
    data = load_referrals()
    return {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "statistics": data.get("statistics", {}),
        "users_count": len(data.get("users", {})),
    }


@app.get("/api/wallets/{user_id}", response_model=WalletAPIResponse)
async def api_user_wallet(user_id: int):
    """
    מחזיר תמונת מצב של ארנק לממשק ה-API:
    - ארנק פנימי (יתרה / סטייקינג / שווי בש\"ח)
    - כתובות BSC/TON (אם הוגדרו) – בדיקות בלבד.
    """
    try:
        ensure_internal_wallet(user_id, None)
        overview = get_wallet_overview(user_id) or {}
        stakes = get_user_stakes(user_id) or []
    except Exception as e:
        logger.error(f"api_user_wallet error for {user_id}: {e}")
        raise

    try:
        balance = Decimal(str(overview.get("balance_slh", "0")))
    except Exception:
        balance = Decimal("0")

    total_staked = Decimal("0")
    for s in stakes:
        try:
            total_staked += Decimal(str(s.get("amount_slh", "0")))
        except Exception:
            continue

    price_nis, _ = get_current_price_and_entry()
    value_nis = balance * price_nis if price_nis > 0 else Decimal("0")

    onchain = get_onchain_wallet(user_id)
    bsc_addr = onchain.get("bsc")
    ton_addr = onchain.get("ton")

    return WalletAPIResponse(
        user_id=user_id,
        balance_slh=float(balance),
        staked_slh=float(total_staked),
        value_nis=float(value_nis),
        bsc_address=bsc_addr,
        ton_address=ton_addr,
    )


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """
    נקודת בריאות ל-Railway (/health).
    """
    return HealthResponse(
        status="ok",
        service="slhnet-telegram-gateway",
        timestamp=datetime.now().isoformat(),
        version="2.2.0",
    )


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    """
    דף נחיתה בסיסי ל-root של השרת.
    """
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
    """
    נקודת ה-webhook של טלגרם – Railway מפנה לכאן.
    """
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
    """
    אתחול בסיסי של ה-DB ושל אפליקציית הטלגרם.
    """
    try:
        init_schema()
    except Exception as e:
        logger.warning(f"init_schema failed: {e}")
    try:
        init_internal_wallet_schema()
    except Exception as e:
        logger.warning(f"init_internal_wallet_schema failed: {e}")

    warnings = Config.validate()
    for w in warnings:
        logger.warning(w)
    if warnings:
        await send_log_message("⚠️ **אזהרות אתחול:**\n" + "\n".join(warnings))

    try:
        await TelegramAppManager.start()
    except Exception as e:
        logger.error(f"Failed to start Telegram Application: {e}")


if __name__ == "__main__":
    import uvicorn

    warnings = Config.validate()
    if warnings:
        print("⚠️ אזהרות קונפיגורציה:")
        for w in warnings:
            print("  " + w)

    port = int(os.getenv("PORT", "8080"))
    print(f"🚀 Starting SLHNET Bot on port {port}")

    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True, log_config=None)
