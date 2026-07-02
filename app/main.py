"""Точка входа: FastAPI-приложение с ботом (webhook/polling), планировщиком и админкой."""

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

from app.admin.routes import router as admin_router
from app.bot.handlers import router as bot_router
from app.bot.middlewares import OwnerOnlyMiddleware
from app.config.settings import get_settings
from app.services.scheduler import build_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

settings = get_settings()

bot = Bot(token=settings.bot_token)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
dp.message.middleware(OwnerOnlyMiddleware())
dp.callback_query.middleware(OwnerOnlyMiddleware())
dp.include_router(bot_router)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.bot = bot
    app.state.storage = storage

    scheduler = build_scheduler(bot, storage)
    scheduler.start()

    polling_task: asyncio.Task | None = None
    if settings.bot_mode == "webhook":
        base = settings.effective_webhook_url.rstrip("/")
        if not base:
            raise RuntimeError("BOT_MODE=webhook, но WEBHOOK_URL/RENDER_EXTERNAL_URL не заданы")
        webhook_url = f"{base}/webhook/{settings.webhook_secret}"
        # secret_token: Telegram будет слать заголовок X-Telegram-Bot-Api-Secret-Token
        # только со своей стороны — так подделать запрос нельзя, даже зная URL.
        await bot.set_webhook(
            webhook_url,
            secret_token=settings.webhook_secret,
            drop_pending_updates=True,
            # включаем my_chat_member/channel_post — по умолчанию Telegram их не шлёт
            allowed_updates=dp.resolve_used_update_types(),
        )
        logger.info("Webhook установлен на %s/webhook/***", base)
    else:
        await bot.delete_webhook(drop_pending_updates=True)
        polling_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        logger.info("Бот запущен в режиме polling")

    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        if polling_task:
            await dp.stop_polling()
            polling_task.cancel()
        await bot.session.close()


app = FastAPI(title="Content Bot", lifespan=lifespan)


@app.middleware("http")
async def admin_csrf_guard(request: Request, call_next):
    """Same-origin проверка для изменяющих запросов админки.

    Basic Auth сам по себе уязвим к CSRF: браузер шлёт учётку автоматически.
    Требуем, чтобы небезопасные методы к /admin приходили с того же origin.
    """
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/admin"):
        source = request.headers.get("origin") or request.headers.get("referer")
        # Блокируем только явный кросс-доменный источник. Если браузер не прислал
        # ни Origin, ни Referer (строгая privacy-политика) — не мешаем владельцу:
        # для CSRF-атаки источник как раз был бы прислан и не совпал бы.
        if source and urlparse(source).netloc != request.url.netloc:
            return Response(status_code=403, content="CSRF check failed")
    return await call_next(request)


app.include_router(admin_router)

static_dir = Path(__file__).parent / "admin" / "static"
app.mount("/admin-static", StaticFiles(directory=str(static_dir)), name="admin-static")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def root():
    return {"service": "content-bot", "admin": "/admin"}


@app.post("/webhook/{secret}")
async def telegram_webhook(secret: str, request: Request):
    # Constant-time сравнение секрета в пути + обязательный заголовок от Telegram.
    header_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not (
        secrets.compare_digest(secret, settings.webhook_secret)
        and secrets.compare_digest(header_token, settings.webhook_secret)
    ):
        return Response(status_code=403)
    data = await request.json()
    update = Update.model_validate(data, context={"bot": bot})
    await dp.feed_update(bot, update)
    return Response(status_code=200)
