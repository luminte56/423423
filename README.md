# Telegram loader accounts + subscription API

## Features
- Telegram account registration (`/register`)
- Subscription status (`/me`)
- Admin subscription activation (`/sub TELEGRAM_ID 7d|month|year|lifetime`)
- Admin password reset (`/resetpass TELEGRAM_ID`)
- Loader API: `POST /api/login`, `GET /api/validate`, `POST /api/logout`
- PostgreSQL on Render; SQLite is used for local development by default

## Important security notes
- Never commit your Telegram bot token or other secrets to GitHub.
- If a real token was posted in chat or committed to a repository, revoke it with BotFather and use the replacement only as a Render environment variable.
- Set `ADMIN_IDS` to your own numeric Telegram user ID, not a bot ID.
- `PASSWORD_SALT` should be a long random secret. Changing it invalidates existing passwords.
- This is a starter implementation. Add HTTPS-only deployment, rate limiting, monitoring, and payment-provider verification before selling access.
- The `/sub` command only grants access manually; it does not verify payment.

## Local run (Windows)
```bat
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
set BOT_TOKEN=YOUR_NEW_BOT_TOKEN
set ADMIN_IDS=YOUR_TELEGRAM_USER_ID
set PASSWORD_SALT=some-long-random-secret
python main.py
```

Open `http://127.0.0.1:10000/health` to check the API.

## Commands
- `/start`
- `/register`
- `/plans`
- `/me`
- Admin: `/sub TELEGRAM_ID 7d|month|year|lifetime`
- Admin: `/resetpass TELEGRAM_ID`

## API
`POST /api/login`
```json
{"username":"u123456","password":"the-password-from-the-bot"}
```
Returns a bearer token when the account has an active subscription.

`GET /api/validate`
Header: `Authorization: Bearer YOUR_ACCESS_TOKEN`

`POST /api/logout`
Header: `Authorization: Bearer YOUR_ACCESS_TOKEN`

The loader must call `/api/validate` when it needs to check the active session. Do not rely only on a local client-side subscription flag.
