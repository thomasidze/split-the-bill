"""Сборка приложения: база, бот, веб-страницы и вебхук Telegram.

Запуск: uv run uvicorn --factory app.main:build
"""

import secrets
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

from app.auth import MembershipChecker
from app.bot import TelegramGateway, build_router
from app.config import Settings
from app.db import init_db, make_engine, make_session_factory
from app.routes import pages
from app.web import BASE_DIR, add_error_handlers


def create_app(
    settings: Settings,
    gateway: TelegramGateway | None = None,
    *,
    setup_webhook: bool = True,
) -> FastAPI:
    """gateway=None — настоящий Telegram; в тестах передаётся подделка."""
    engine = make_engine(settings.db_path)
    init_db(engine)
    session_factory = make_session_factory(engine)

    bot = Bot(settings.bot_token)  # сам по себе в сеть не ходит
    if gateway is None:
        gateway = TelegramGateway(bot, settings)
    dispatcher = Dispatcher()
    dispatcher.include_router(build_router(session_factory, gateway))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if setup_webhook:
            await bot.set_webhook(
                f"{settings.app_url}/tg/webhook",
                secret_token=settings.webhook_secret,
                allowed_updates=dispatcher.resolve_used_update_types(),
            )
        yield
        await bot.session.close()
        engine.dispose()

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.gateway = gateway
    app.state.membership = MembershipChecker(gateway.is_member)  # один кеш на всё приложение

    add_error_handlers(app)
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    app.include_router(pages.router)

    @app.post("/tg/webhook")
    async def telegram_webhook(request: Request) -> Response:
        """Сюда Telegram присылает обновления для бота."""
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not secrets.compare_digest(secret.encode(), settings.webhook_secret.encode()):
            return Response(status_code=403)
        update = Update.model_validate(await request.json(), context={"bot": bot})
        await dispatcher.feed_update(bot, update)
        return Response()

    return app


def build() -> FastAPI:
    """Точка входа для uvicorn: настройки берутся из переменных окружения и .env."""
    return create_app(Settings())
