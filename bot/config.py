from pydantic import BaseSettings, validator


class Settings(BaseSettings):
    BOT_TOKEN: str
    WEBHOOK_URL: str
    ADMIN_ALERT_CHAT_ID: int
    LANDING_URL: str = "https://slh-nft.com"
    PAYBOX_URL: str | None = None
    BUSINESS_GROUP_URL: str | None = None
    GROUP_STATIC_INVITE: str | None = None

    class Config:
        env_file = ".env"
        case_sensitive = False

    @validator("BOT_TOKEN")
    def validate_bot_token(cls, v: str) -> str:
        if not v or ":" not in v:
            raise ValueError("Invalid BOT_TOKEN format")
        return v

    @validator("WEBHOOK_URL")
    def validate_webhook_url(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("WEBHOOK_URL must use HTTPS")
        return v

    @classmethod
    def validate_env(cls):
        """Return (warnings, settings_instance)."""
        warnings: list[str] = []
        cfg = cls()

        if not cfg.PAYBOX_URL:
            warnings.append("PAYBOX_URL is not set – 39₪ payment button will be generic only")
        if not cfg.BUSINESS_GROUP_URL and not cfg.GROUP_STATIC_INVITE:
            warnings.append("No BUSINESS_GROUP_URL / GROUP_STATIC_INVITE – group join button may be missing")

        return warnings, cfg


# This is the shared config instance
warnings, Config = Settings.validate_env()
