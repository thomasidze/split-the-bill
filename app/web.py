"""Общее для веб-части: шаблоны, сессия базы на запрос, вход пользователя (Ctx) и ошибки."""

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import jinja2
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import services
from app.auth import AUTH_ERROR_TEXT, AuthError, InitData, TelegramUnavailable, validate_init_data
from app.colors import event_color, initial, person_color
from app.links import parse_start_param
from app.models import Chat, Event, Expense, Item, Settlement, User
from app.money import format_rub, format_rub_input

BASE_DIR = Path(__file__).resolve().parent.parent

CHAT_NOT_FOUND_TEXT = "Открой приложение по кнопке в чате"
BOT_REMOVED_TEXT = "Бот удалён из чата — добавьте его обратно"
NOT_MEMBER_TEXT = "Это доступно только участникам чата"
TELEGRAM_DOWN_TEXT = "Telegram не отвечает, попробуй ещё раз"
BAD_INPUT_TEXT = "Проверь введённые данные"


# --- шаблоны ---


def display_name(user: User) -> str:
    """Имя для показа: «Имя Фамилия», а если имени нет — @username."""
    name = " ".join(part for part in (user.first_name, user.last_name) if part)
    if name:
        return name
    return f"@{user.username}" if user.username else "Без имени"


templates = Jinja2Templates(
    env=jinja2.Environment(
        loader=jinja2.FileSystemLoader(BASE_DIR / "templates"),
        autoescape=True,  # пользовательский текст в шаблонах всегда экранируется
    )
)
templates.env.filters["rub"] = format_rub  # {{ 135050 | rub }} -> «1 350,50 ₽»
templates.env.filters["rub_input"] = format_rub_input  # {{ 65050 | rub_input }} -> «650,50»
templates.env.filters["name"] = display_name  # {{ user | name }}
templates.env.filters["event_color"] = event_color  # {{ (event | event_color).main }} -> «#D85A30»
templates.env.filters["person_color"] = person_color  # {{ (user | person_color).bg }}
templates.env.filters["initial"] = initial  # {{ user | initial }} -> «А»


def render(request: Request, template: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(request, template, context)


# --- сессия базы и вход ---


async def get_session(request: Request) -> AsyncIterator[Session]:
    """Сессия на один запрос: без ошибок — commit, при любой ошибке — rollback."""
    with request.app.state.session_factory() as session:
        try:
            yield session
        except Exception:
            session.rollback()
            raise
        session.commit()


@dataclass
class Ctx:
    """Кто делает запрос и из какого чата."""

    session: Session
    user: User
    chat: Chat
    init: InitData


async def get_ctx(
    request: Request,
    # scope="function": commit случится до отправки ответа, а не после
    session: Session = Depends(get_session, scope="function"),
) -> Ctx:
    """Проверяет initData из заголовка X-Init-Data, чат из start_param и членство в чате."""
    settings = request.app.state.settings
    raw = request.headers.get("X-Init-Data", "")
    try:
        init = validate_init_data(raw, settings.bot_token, now=int(time.time()))
    except AuthError:
        raise HTTPException(401, AUTH_ERROR_TEXT) from None

    invite_code, _ = parse_start_param(init.start_param)
    chat = services.get_chat_by_code(session, invite_code) if invite_code else None
    if chat is None:
        raise HTTPException(404, CHAT_NOT_FOUND_TEXT)
    if not chat.bot_active:
        raise HTTPException(403, BOT_REMOVED_TEXT)
    # Проверка членства может сходить в Telegram. Пока ждём ответа, в базу ничего не пишем:
    # незавершённая запись в SQLite держит блокировку и тормозит остальные запросы.
    try:
        is_member = await request.app.state.membership.is_member(chat.id, init.user.id)
    except TelegramUnavailable:
        raise HTTPException(503, TELEGRAM_DOWN_TEXT) from None
    if not is_member:
        raise HTTPException(403, NOT_MEMBER_TEXT)

    user = services.upsert_user(session, init.user)
    # Сразу сохраняем пользователя: дальше роут может ждать Telegram (фото, уведомление),
    # а незавершённая запись держала бы блокировку SQLite. Сам по себе пользователь безвреден.
    session.commit()
    return Ctx(session=session, user=user, chat=chat, init=init)


# --- объекты только из своего чата ---


def chat_event(ctx: Ctx, event_id: int) -> Event:
    """Событие из чата пользователя; чужое или несуществующее — 404."""
    event = ctx.session.get(Event, event_id)
    if event is None or event.chat_id != ctx.chat.id:
        raise HTTPException(404, services.NOT_FOUND)
    return event


def chat_expense(ctx: Ctx, expense_id: int) -> Expense:
    """Расход из чата пользователя; чужой или несуществующий — 404."""
    expense = ctx.session.get(Expense, expense_id)
    if expense is None:
        raise HTTPException(404, services.NOT_FOUND)
    chat_event(ctx, expense.event_id)  # чат определяется по событию расхода
    return expense


def chat_item(ctx: Ctx, item_id: int) -> Item:
    """Позиция расхода из чата пользователя; чужая или несуществующая — 404."""
    item = ctx.session.get(Item, item_id)
    if item is None:
        raise HTTPException(404, services.NOT_FOUND)
    chat_expense(ctx, item.expense_id)  # чат определяется по расходу позиции
    return item


def chat_settlement(ctx: Ctx, settlement_id: int) -> Settlement:
    """Перевод из чата пользователя; чужой или несуществующий — 404."""
    settlement = ctx.session.get(Settlement, settlement_id)
    if settlement is None:
        raise HTTPException(404, services.NOT_FOUND)
    chat_event(ctx, settlement.event_id)  # чат определяется по событию перевода
    return settlement


# --- ошибки: всегда простой текст, его app.js покажет пользователю ---


async def http_error(request: Request, exc: StarletteHTTPException) -> PlainTextResponse:
    return PlainTextResponse(str(exc.detail), status_code=exc.status_code, headers=exc.headers)


async def service_error(request: Request, exc: services.ServiceError) -> PlainTextResponse:
    return PlainTextResponse(str(exc), status_code=400)


async def bad_input(request: Request, exc: RequestValidationError) -> PlainTextResponse:
    return PlainTextResponse(BAD_INPUT_TEXT, status_code=400)


def add_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(services.ServiceError, service_error)
    app.add_exception_handler(RequestValidationError, bad_input)
