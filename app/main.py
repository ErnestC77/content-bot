"""Точка входа: FastAPI-приложение с ботом (webhook/polling), планировщиком и админкой."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

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
        await bot.set_webhook(webhook_url, drop_pending_updates=True)
        logger.info("Webhook установлен: %s", webhook_url)
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
    if secret != settings.webhook_secret:
        return Response(status_code=403)
    data = await request.json()
    update = Update.model_validate(data, context={"bot": bot})
    await dp.feed_update(bot, update)
    return Response(status_code=200)
