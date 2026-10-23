import asyncio
import hashlib
import logging
import os
import secrets
import string
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import uvicorn

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("loader-service")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./loader.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
if DATABASE_URL.startswith("postgresql://") and "+" not in DATABASE_URL.split("://", 1)[0]:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN as an environment variable.")
if not ADMIN_IDS:
    raise RuntimeError("Set ADMIN_IDS to your Telegram numeric user ID.")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "loader_users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    subscription_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lifetime: Mapped[bool] = mapped_column(Boolean, default=False)


class SessionToken(Base):
    __tablename__ = "loader_sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


Base.metadata.create_all(engine)

PLANS = {
    "7d": (70, timedelta(days=7)),
    "month": (250, timedelta(days=30)),
    "year": (420, timedelta(days=365)),
    "lifetime": (500, None),
}
PLAN_NAMES = {
    "7d": "7 дней — 70 ₽",
    "month": "месяц (30 дней) — 250 ₽",
    "year": "год (365 дней) — 420 ₽",
    "lifetime": "навсегда — 500 ₽",
}


def hash_password(password: str) -> str:
    # PBKDF2 is used instead of storing a password in plain text.
    salt = os.getenv("PASSWORD_SALT", "change-this-salt-before-deploy").encode()
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000).hex()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_dt(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def subscription_active(user: User) -> bool:
    if user.lifetime:
        return True
    end = normalize_dt(user.subscription_until)
    return end is not None and end > utc_now()


def make_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Это бот аккаунтов лоадера.\\n\\n"
        "/register — создать аккаунт лоадера\\n"
        "/me — посмотреть состояние подписки\\n"
        "/plans — список тарифов\\n\\n"
        "Покупка пока не автоматизирована: оплату и подписку подтверждает администратор."
    )


async def plans_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "Тарифы:\\n" + "\\n".join(f"• {name}" for name in PLAN_NAMES.values())
    await update.message.reply_text(text)


async def register_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    if tg_user is None:
        return
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.telegram_id == tg_user.id))
        if user:
            await update.message.reply_text(
                f"Аккаунт уже создан.\\nЛогин: `{user.username}`\\n"
                "Если потерял пароль, обратись к администратору для сброса.",
                parse_mode="Markdown",
            )
            return
        username = f"u{tg_user.id}"
        password = make_password()
        user = User(
            telegram_id=tg_user.id,
            username=username,
            password_hash=hash_password(password),
        )
        db.add(user)
        db.commit()
    await update.message.reply_text(
        "Аккаунт создан! Сохрани данные в безопасном месте — пароль повторно не показывается.\\n\\n"
        f"Логин: `{username}`\\nПароль: `{password}`\\n\\n"
        "Подписка активируется администратором после подтверждения оплаты.",
        parse_mode="Markdown",
    )


async def me_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.telegram_id == tg_user.id))
        if not user:
            await update.message.reply_text("Аккаунта пока нет. Используй /register.")
            return
        if user.lifetime:
            status = "Активна навсегда."
        elif subscription_active(user):
            end = normalize_dt(user.subscription_until).strftime("%Y-%m-%d %H:%M UTC")
            status = f"Активна до {end}."
        else:
            status = "Не активна."
        await update.message.reply_text(f"Логин: {user.username}\\nПодписка: {status}")


async def sub_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_user = update.effective_user
    if tg_user.id not in ADMIN_IDS:
        await update.message.reply_text("Нет доступа.")
        return
    if len(context.args) != 2:
        await update.message.reply_text("Использование: /sub TELEGRAM_ID 7d|month|year|lifetime")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Telegram ID должен быть числом.")
        return
    plan = context.args[1].lower()
    if plan not in PLANS:
        await update.message.reply_text("Неизвестный тариф. Используй: 7d, month, year, lifetime")
        return
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.telegram_id == target_id))
        if not user:
            await update.message.reply_text("Пользователь ещё не зарегистрировался через /register.")
            return
        _, duration = PLANS[plan]
        if duration is None:
            user.lifetime = True
            user.subscription_until = None
        else:
            user.lifetime = False
            current_end = normalize_dt(user.subscription_until)
            start = current_end if current_end and current_end > utc_now() else utc_now()
            user.subscription_until = start + duration
        db.commit()
    await update.message.reply_text(f"Подписка {plan} активирована для Telegram ID {target_id}.")


async def reset_password_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("Нет доступа.")
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Использование: /resetpass TELEGRAM_ID")
        return
    target_id = int(context.args[0])
    password = make_password()
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.telegram_id == target_id))
        if not user:
            await update.message.reply_text("Пользователь не найден.")
            return
        user.password_hash = hash_password(password)
        db.query(SessionToken).filter(SessionToken.user_id == user.id).delete()
        db.commit()
    await update.message.reply_text(
        f"Новый пароль для {target_id}: `{password}`\\nПередай его пользователю безопасно.",
        parse_mode="Markdown",
    )


app = FastAPI(title="Loader License API", version="1.0.0")


class LoginRequest(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=8, max_length=128)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/api/login")
def api_login(payload: LoginRequest):
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == payload.username))
        if not user or not secrets.compare_digest(user.password_hash, hash_password(payload.password)):
            raise HTTPException(status_code=401, detail="Invalid username or password")
        if not subscription_active(user):
            raise HTTPException(status_code=403, detail="Subscription inactive")
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        db.add(SessionToken(
            token_hash=token_hash,
            user_id=user.id,
            expires_at=utc_now() + timedelta(hours=12),
        ))
        db.commit()
        return {"access_token": raw_token, "token_type": "bearer", "expires_in": 43200}


@app.get("/api/validate")
def api_validate(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    raw_token = authorization.split(" ", 1)[1].strip()
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    with SessionLocal() as db:
        session = db.scalar(select(SessionToken).where(SessionToken.token_hash == token_hash))
        if not session or normalize_dt(session.expires_at) <= utc_now():
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        user = db.get(User, session.user_id)
        if not user or not subscription_active(user):
            raise HTTPException(status_code=403, detail="Subscription inactive")
        return {"valid": True, "username": user.username, "lifetime": user.lifetime,
                "subscription_until": user.subscription_until.isoformat() if user.subscription_until else None}


@app.post("/api/logout")
def api_logout(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token_hash = hashlib.sha256(authorization.split(" ", 1)[1].strip().encode()).hexdigest()
    with SessionLocal() as db:
        session = db.scalar(select(SessionToken).where(SessionToken.token_hash == token_hash))
        if session:
            db.delete(session)
            db.commit()
    return {"ok": True}


async def run_bot():
    telegram_app = Application.builder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start_cmd))
    telegram_app.add_handler(CommandHandler("plans", plans_cmd))
    telegram_app.add_handler(CommandHandler("register", register_cmd))
    telegram_app.add_handler(CommandHandler("me", me_cmd))
    telegram_app.add_handler(CommandHandler("sub", sub_cmd))
    telegram_app.add_handler(CommandHandler("resetpass", reset_password_cmd))
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()


async def run_api():
    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")), log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def main():
    await asyncio.gather(run_bot(), run_api())


if __name__ == "__main__":
    asyncio.run(main())
