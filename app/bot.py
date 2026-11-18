import html
import logging
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    ChatMember,
    ChatMemberRestricted,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy.orm import sessionmaker

from app import services
from app.auth import TelegramUnavailable
from app.config import Settings
from app.links import build_app_link
from app.money import format_rub

log = logging.getLogger(__name__)

WELCOME_TEXT = (
    "Привет! Я помогу поделить расходы.\n"
    "Создавайте события, добавляйте чеки и отмечайте, кто что взял.\n"
    "Закрепите это сообщение, чтобы приложение было под рукой 👇"
)

GROUP_TYPES = ("group", "supergroup")
MEMBER_STATUSES = ("member", "administrator", "creator")
BOT_ADDED_STATUSES = ("member", "administrator")
BOT_REMOVED_STATUSES = ("left", "kicked")


MAX_PART_LENGTH = 100


def _shorten(text: str) -> str:
    """Обрезает строку так, чтобы после экранирования HTML она была не длиннее 100 символов."""
    if len(html.escape(text)) <= MAX_PART_LENGTH:
        return html.escape(text)
    while len(html.escape(text + "…")) > MAX_PART_LENGTH:
        text = text[:-1]
    return html.escape(text + "…")


def expense_notification_text(
    author: str, expense_title: str, event_title: str, total: int
) -> str:
    """Текст уведомления о новом расходе (HTML, пользовательские строки экранированы).

    Подпись к фото в Telegram — до 1024 символов, поэтому длинные строки обрезаются.
    """
    author = _shorten(author)
    expense_title = _shorten(expense_title)
    event_title = _shorten(event_title)
    return (
        f"<b>{author}</b> добавил(а) расход «{expense_title}» "
        f"в «{event_title}»: {format_rub(total)}\n"
        "Отметьте свои позиции 👇"
    )


def _url_button(text: str, url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, url=url)]])


def _is_in_chat(member: ChatMember) -> bool:
    if isinstance(member, ChatMemberRestricted):
        return member.is_member
    return member.status in MEMBER_STATUSES


class TelegramGateway:
    """Всё общение с Telegram Bot API в одном месте (в тестах подменяется)."""

    def __init__(self, bot: Bot, settings: Settings):
        self.bot = bot
        self.settings = settings

    async def is_member(self, chat_id: int, user_id: int) -> bool:
        """Состоит ли пользователь в чате. Если Telegram не ответил — TelegramUnavailable."""
        try:
            member = await self.bot.get_chat_member(chat_id, user_id)
        except (TelegramBadRequest, TelegramForbiddenError):
            # Telegram ответил отказом: такого участника нет или бот этот чат не видит
            log.warning("get_chat_member refused: chat=%s user=%s", chat_id, user_id)
            return False
        except TelegramAPIError as error:
            # Сеть, «слишком много запросов», ошибка сервера Telegram — ответ неизвестен
            log.warning("get_chat_member failed: chat=%s user=%s", chat_id, user_id)
            raise TelegramUnavailable() from error
        return _is_in_chat(member)

    async def send_welcome(self, chat_id: int, invite_code: str) -> None:
        link = build_app_link(
            self.settings.bot_username, self.settings.app_short_name, invite_code
        )
        try:
            await self.bot.send_message(chat_id, WELCOME_TEXT, reply_markup=_url_button("Открыть", link))
        except TelegramAPIError:
            log.exception("send_welcome failed: chat=%s", chat_id)

    async def notify_expense(
        self, chat_id: int, text: str, link: str, photo: bytes | None
    ) -> str | None:
        """Пишет в чат о новом расходе. Возвращает file_id фото, если фото сохранилось в Telegram.

        Если Telegram фото не принял, уведомление всё равно уходит — простым текстом, и вернётся None.
        """
        keyboard = _url_button("Отметиться", link)
        if photo is not None:
            try:
                message = await self.bot.send_photo(
                    chat_id,
                    BufferedInputFile(photo, filename="receipt.jpg"),
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
                return message.photo[-1].file_id
            except TelegramAPIError:
                log.exception("send_photo failed, sending text only: chat=%s", chat_id)
        try:
            await self.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=keyboard)
        except TelegramAPIError:
            log.exception("notify_expense failed: chat=%s", chat_id)
        return None

    async def download_photo(self, file_id: str) -> bytes:
        file = await self.bot.get_file(file_id)
        if file.file_path is None:
            raise ValueError("Фото недоступно")
        buffer = BytesIO()
        await self.bot.download_file(file.file_path, buffer)
        return buffer.getvalue()


async def on_my_chat_member(
    event: ChatMemberUpdated, session_factory: sessionmaker, gateway: TelegramGateway
) -> None:
    """Бота добавили в группу или удалили из неё."""
    if event.chat.type not in GROUP_TYPES:
        return
    status = event.new_chat_member.status
    was_in_chat = _is_in_chat(event.old_chat_member)
    with session_factory() as s:
        if status in BOT_ADDED_STATUSES:
            if was_in_chat:
                return  # бота лишь повысили или понизили — приветствие не повторяем
            chat = services.register_chat(s, event.chat.id, event.chat.title or "")
            s.commit()
            invite_code = chat.invite_code
        elif status in BOT_REMOVED_STATUSES:
            services.deactivate_chat(s, event.chat.id)
            s.commit()
            return
        else:
            return
    await gateway.send_welcome(event.chat.id, invite_code)


async def on_app_command(
    message: Message, session_factory: sessionmaker, gateway: TelegramGateway
) -> None:
    """/app в группе: повторить приветствие (и завести чат, если бота добавили раньше)."""
    with session_factory() as s:
        chat = services.register_chat(s, message.chat.id, message.chat.title or "")
        s.commit()
        invite_code = chat.invite_code
    await gateway.send_welcome(message.chat.id, invite_code)


async def on_chat_migrated(message: Message, session_factory: sessionmaker) -> None:
    """Группа стала супергруппой: у чата новый id, переносим на него данные приложения.

    Это служебное сообщение в старой группе; бот получает его и с включённым Privacy mode.
    """
    with session_factory() as s:
        services.migrate_chat(s, message.chat.id, message.migrate_to_chat_id)
        s.commit()


def build_router(session_factory: sessionmaker, gateway: TelegramGateway) -> Router:
    router = Router()

    @router.my_chat_member()
    async def my_chat_member(event: ChatMemberUpdated) -> None:
        await on_my_chat_member(event, session_factory, gateway)

    @router.message(Command("app"), F.chat.type.in_(GROUP_TYPES))
    async def app_command(message: Message) -> None:
        await on_app_command(message, session_factory, gateway)

    @router.message(F.migrate_to_chat_id)
    async def chat_migrated(message: Message) -> None:
        await on_chat_migrated(message, session_factory)

    return router
