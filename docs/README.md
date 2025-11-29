# Buy My Shop – Telegram Gateway Bot

בוט טלגרם שמשמש כ"שער כניסה" לקהילת עסקים, עם:

- תשלום חד־פעמי (39 ₪) במספר ערוצים (בנק, פייבוקס, ביט, PayPal, TON).
- אישור תשלום ידני + שליחת קישור לקהילת העסקים.
- העברת לוגים של תשלומים לקבוצת ניהול.
- תמונת שער עם מונים (כמה פעמים הוצגה, כמה עותקים נשלחו אחרי אישור).
- תפריט אדמין עם סטטוס מערכת, מונים ורעיונות לפיתוח עתידי.
- אינטגרציה אופציונלית ל-PostgreSQL דרך `db.py`.
- דף נחיתה סטטי ב-GitHub Pages לשיתוף ברשתות:
  - `https://osifeu-prog.github.io/botshop/`

## קבצים עיקריים

- `main.py` – לוגיקת הבוט + FastAPI + webhook + JobQueue.
- `requirements.txt` – ספריות נדרשות.
- `Procfile` – פקודת הרצה ל-PaaS (Railway).
- `.gitignore` – הגדרות גיט.
- `assets/start_banner.jpg` – תמונת שער ל-/start (הבוט משתמש בה).
- `docs/index.html` – דף נחיתה ל-GitHub Pages (עם Open Graph לתמונה).
- `db.py` (אופציונלי) – חיבור ל-PostgreSQL ללוגים של תשלומים.
- `.env.example` – דוגמה למשתני סביבה.

## משתני סביבה (Railway → Variables)

חובה:

- `BOT_TOKEN` – הטוקן שקיבלת מ-@BotFather.
- `WEBHOOK_URL` – ה-URL המלא של ה-webhook, לדוגמה:  
  `https://webwook-production-4861.up.railway.app/webhook`

אופציונלי, אבל מומלץ:

- `PAYBOX_URL` – לינק תשלום לפייבוקס (אפשר להחליף מדי פעם).
- `BIT_URL` – לינק תשלום לביט.
- `PAYPAL_URL` – לינק ל-PayPal.
- `LANDING_URL` – לינק לדף הנחיתה (ברירת מחדל: GitHub Pages).
- `START_IMAGE_PATH` – נתיב לתמונת השער (ברירת מחדל: `assets/start_banner.jpg`).
- `DATABASE_URL` – אם משתמשים ב-PostgreSQL (מבנה: `postgres://user:pass@host:port/dbname`).

## הרצה לוקאלית

```bash
python -m venv .venv
source .venv/bin/activate  # ב-Windows: .venv\Scripts\activate
pip install -r requirements.txt

# הגדרת משתני סביבה לדוגמה:
export BOT_TOKEN="123:ABC"
export WEBHOOK_URL="https://your-public-url/webhook"

uvicorn main:app --host 0.0.0.0 --port 8000
