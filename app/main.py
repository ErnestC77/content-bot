"""Точка входа: FastAPI-приложение с ботом (webhook/polling), планировщиком и Mini App."""

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import MenuButtonWebApp, Update, WebAppInfo
from fastapi import FastAPI, Request, Response

from app.bot.handlers import router as bot_router
from app.bot.middlewares import OwnerOnlyMiddleware
from app.config.settings import get_settings
from app.services.scheduler import build_scheduler
from app.webapp.routes import include_webapp

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

    scheduler = build_scheduler(bot)
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

    # Кнопка меню Telegram открывает Mini App (требует https-адрес)
    base_url = settings.effective_webhook_url.rstrip("/")
    if base_url.startswith("https://"):
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="🗂 Панель", web_app=WebAppInfo(url=f"{base_url}/webapp"))
            )
            logger.info("Кнопка меню Mini App установлена")
        except Exception:
            logger.exception("Не удалось установить кнопку меню Mini App")

    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        if polling_task:
            await dp.stop_polling()
            polling_task.cancel()
        await bot.session.close()


app = FastAPI(title="Content Bot", lifespan=lifespan)
include_webapp(app)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def root():
    return {"service": "content-bot", "webapp": "/webapp"}


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
