from datetime import datetime

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import GetChatMember, SendPhoto
from aiogram.types import Chat as TgChat
from aiogram.types import (
    ChatMemberAdministrator,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    Message,
    User,
)
from sqlalchemy import select

from app import services
from app.auth import TelegramUnavailable, TgUser
from app.bot import TelegramGateway, expense_notification_text, on_chat_migrated, on_my_chat_member
from app.models import Chat, Event
from tests.conftest import TEST_SETTINGS

BOT = User(id=1, is_bot=True, first_name="SplitBot")
ADMIN = User(id=2, is_bot=False, first_name="Аня")


class FakeGateway:
    def __init__(self):
        self.welcomes: list[tuple[int, str]] = []

    async def send_welcome(self, chat_id: int, invite_code: str) -> None:
        self.welcomes.append((chat_id, invite_code))


def make_update(old_status: str, new_status: str, chat_type: str = "group") -> ChatMemberUpdated:
    members = {
        "member": ChatMemberMember,
        "left": ChatMemberLeft,
        "administrator": ChatMemberAdministrator.model_construct,
    }
    return ChatMemberUpdated(
        chat=TgChat(id=-100, type=chat_type, title="Друзья"),
        from_user=ADMIN,
        date=datetime(2026, 11, 1),
        old_chat_member=members[old_status](status=old_status, user=BOT),
        new_chat_member=members[new_status](status=new_status, user=BOT),
    )


def test_notification_text_escapes_html():
    text = expense_notification_text("Аня", "<b>Пиво & чипсы</b>", "Пятница", 100)
    assert "&lt;b&gt;Пиво &amp; чипсы&lt;/b&gt;" in text


def test_notification_text_amount():
    text = expense_notification_text("Аня", "Ужин", "Пятница", 320000)
    assert "3 200 ₽" in text  # тысячи разделены неразрывным пробелом


async def test_bot_added_registers_chat_and_welcomes(session_factory):
    gateway = FakeGateway()
    await on_my_chat_member(make_update("left", "member"), session_factory, gateway)

    with session_factory() as s:
        chat = s.get(Chat, -100)
        assert chat is not None
        assert chat.title == "Друзья"
        assert chat.bot_active is True
        invite_code = chat.invite_code
    assert gateway.welcomes == [(-100, invite_code)]


async def test_bot_removed_deactivates_chat(session_factory):
    gateway = FakeGateway()
    await on_my_chat_member(make_update("left", "member"), session_factory, gateway)
    await on_my_chat_member(make_update("member", "left"), session_factory, gateway)

    with session_factory() as s:
        assert s.get(Chat, -100).bot_active is False
    assert len(gateway.welcomes) == 1


async def test_private_chat_is_ignored(session_factory):
    gateway = FakeGateway()
    await on_my_chat_member(make_update("left", "member", "private"), session_factory, gateway)

    with session_factory() as s:
        assert s.get(Chat, -100) is None
    assert gateway.welcomes == []


async def test_bot_promoted_does_not_rewelcome(session_factory):
    gateway = FakeGateway()
    await on_my_chat_member(make_update("left", "member"), session_factory, gateway)
    await on_my_chat_member(make_update("member", "administrator"), session_factory, gateway)

    assert len(gateway.welcomes) == 1


def test_notification_text_fits_photo_caption():
    long_title = "а" * 500
    text = expense_notification_text(long_title, long_title, long_title, 100)
    assert len(text) <= 1024


def test_notification_text_fits_photo_caption_when_escaping_grows():
    ampersands = "&" * 500  # каждый & превращается в &amp;
    text = expense_notification_text(ampersands, ampersands, ampersands, 100)
    assert len(text) <= 1024
    assert "&amp;&amp;…" in text


async def test_group_upgraded_to_supergroup_moves_chat(session_factory):
    gateway = FakeGateway()
    await on_my_chat_member(make_update("left", "member"), session_factory, gateway)
    with session_factory() as s:
        services.upsert_user(s, TgUser(2, "Аня", None, None, None))
        services.create_event(s, -100, "Пятница", 2)
        s.commit()
        invite_code = s.get(Chat, -100).invite_code
    message = Message(
        message_id=1,
        date=datetime(2026, 11, 1),
        chat=TgChat(id=-100, type="group", title="Друзья"),
        from_user=ADMIN,
        migrate_to_chat_id=-100500,
    )

    await on_chat_migrated(message, session_factory)

    with session_factory() as s:
        assert s.get(Chat, -100) is None
        assert s.get(Chat, -100500).invite_code == invite_code
        assert s.scalars(select(Event)).one().chat_id == -100500


# --- TelegramGateway с поддельным Bot ---


class FakeBot:
    """Вместо aiogram Bot: запоминает отправленные тексты, ошибки задаются заранее."""

    def __init__(self, photo_error: Exception | None = None, member_error: Exception | None = None):
        self.photo_error = photo_error
        self.member_error = member_error
        self.messages: list[tuple[str, object]] = []  # (текст, кнопки)

    async def send_photo(self, chat_id, photo, **kwargs):
        raise self.photo_error

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((text, kwargs.get("reply_markup")))

    async def get_chat_member(self, chat_id, user_id):
        raise self.member_error


async def test_rejected_photo_still_sends_notification():
    error = TelegramBadRequest(SendPhoto(chat_id=-100, photo="x"), "Bad Request: IMAGE_PROCESS_FAILED")
    bot = FakeBot(photo_error=error)
    gateway = TelegramGateway(bot, TEST_SETTINGS)

    file_id = await gateway.notify_expense(-100, "Новый расход", "https://t.me/bot/app", b"photo")

    assert file_id is None  # фото не сохранилось
    ((text, keyboard),) = bot.messages
    assert text == "Новый расход"
    assert keyboard.inline_keyboard[0][0].url == "https://t.me/bot/app"


GET_MEMBER = GetChatMember(chat_id=-100, user_id=5)


@pytest.mark.parametrize(
    "error",
    [
        TelegramBadRequest(GET_MEMBER, "Bad Request: user not found"),
        TelegramForbiddenError(GET_MEMBER, "Forbidden: bot was kicked from the group chat"),
    ],
)
async def test_is_member_false_when_telegram_says_no(error):
    gateway = TelegramGateway(FakeBot(member_error=error), TEST_SETTINGS)
    assert await gateway.is_member(-100, 5) is False


@pytest.mark.parametrize(
    "error",
    [
        TelegramNetworkError(GET_MEMBER, "timeout"),
        TelegramServerError(GET_MEMBER, "Internal Server Error"),
        TelegramRetryAfter(GET_MEMBER, "Too Many Requests", retry_after=5),
    ],
)
async def test_is_member_raises_when_telegram_unavailable(error):
    gateway = TelegramGateway(FakeBot(member_error=error), TEST_SETTINGS)
    with pytest.raises(TelegramUnavailable):
        await gateway.is_member(-100, 5)
