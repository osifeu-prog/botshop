# SLHNET Telegram Gateway – Architecture

השער הזה יושב בין Telegram לבין כל שכבת ה‑API / DB / Blockchain שלך.

## Layers

1. **Presentation – Telegram Bot**
   - python‑telegram‑bot v21 (async)
   - Webhook only (Railway friendly)
   - Handlers:
     - `/start` – תצוגת חיסכון והשקעה + כפתורי 39 ₪ וקבוצה
     - `/help` – פירוט פקודות עיקריות
     -Callbacks – כפתורי "מידע למשקיעים", "גישה לתוכן מלא" וכו'.

2. **Gateway / API – FastAPI**
   - `/webhook` – קבלת עדכונים מטלגרם
   - `/healthz`, `/health/detailed` – ניטור
   - `/api/metrics/finance` – מדדי אישורים / Reserve
   - `/metrics` – Prometheus

3. **Services**
   - `core.db.DatabaseManager` – חיבור ל‑Postgres (asyncpg)
   - `core.cache` – טעינת בלוקים מ‑messages.md עם cache
   - `bot.telegram_manager` – Lifecycle של Application (initialize/start/stop)

4. **Observability & Security**
   - Structured logging (structlog)
   - Rate limiting (`slowapi`) ל‑`/webhook`
   - מדדי Prometheus
   - Health checks מלאים

ה‑gateway הזה אמור לחיות לצד שאר השירותים שלך (SLH_API, SLH_TON וכו') ולהיות השכבה שדרכה כל תנועת הטלגרם עוברת.
