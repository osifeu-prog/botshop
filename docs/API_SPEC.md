# SLHNET Telegram Gateway – API Specification

Base URL (prod example): `https://botshop-production.up.railway.app`

## 1. Health & Meta

### GET `/healthz`

Returns basic process/uptime status used by Railway.

### GET `/health/detailed`

Returns a detailed status JSON:

- `status`: healthy / degraded
- `services.telegram_bot`: מצב חיבור לבוט טלגרם
- `services.database`: מצב DB (אם מוגדר `DATABASE_URL`)
- `services.files`: קיום קובצי messages / referrals / תמונת פתיחה

## 2. Telegram Webhook

### POST `/webhook`

- Body: JSON של Telegram Update (נשלח ע"י BotFather webhook)
- בדיקות:
  - ולידציה ל‑`update_id`
  - הגנת SPAM (עדכונים ריקים)
  - הגנת כפל (duplicate updates) לפי `update_id`
  - Rate limiting באמצעות `slowapi`

מעביר את ה‑Update ל‑`TelegramAppManager` ומשם ל‑handlers של python‑telegram‑bot.

## 3. Finance Metrics

### GET `/api/metrics/finance`

מחזיר מידע מסכם על approvals ו‑reserve (אם יש DB):

```json
{
  "timestamp": "...",
  "reserve": {
    "total_amount": 78,
    "total_reserve": 0,
    "total_net": 0,
    "total_payments": 14,
    "approved_count": 8,
    "pending_count": 6,
    "rejected_count": 0
  },
  "approvals": {
    "pending": 6,
    "approved": 8,
    "rejected": 0,
    "total": 14
  }
}
```

אם אין DB או טבלה מתאימה – מוחזרים ערכי ברירת מחדל (0) וה‑API ממשיך לעבוד.

## 4. Metrics (Prometheus)

### GET `/metrics`

Prometheus scrape endpoint עבור:

- `slhnet_messages_received_total`
- `slhnet_commands_processed_total{command="start"}`
- `slhnet_request_duration_seconds_bucket` וכו'.
