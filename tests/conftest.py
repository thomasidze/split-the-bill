import pytest
from fastapi.testclient import TestClient

from app import services
from app.auth import TelegramUnavailable
from app.config import Settings
from app.db import init_db, make_engine, make_session_factory
from app.main import create_app
from tests.helpers import make_init_data

TEST_SETTINGS = Settings(
    bot_token="123456:TEST-TOKEN",
    bot_username="split_test_bot",
    app_short_name="split",
    app_url="https://example.test",
    webhook_secret="test-secret",
    db_path=":memory:",
)


@pytest.fixture
def session():
    """Сессия с пустой базой SQLite в памяти и созданной схемой."""
    engine = make_engine(":memory:")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as s:
        yield s
    engine.dispose()


@pytest.fixture
def session_factory():
    """Фабрика сессий для кода, который сам открывает и закрывает сессию (бот)."""
    engine = make_engine(":memory:")
    init_db(engine)
    yield make_session_factory(engine)
    engine.dispose()


class FakeGateway:
    """Подменяет TelegramGateway в тестах: ничего не отправляет, всё запоминает."""

    def __init__(self):
        self.members: set[tuple[int, int]] = set()  # (chat_id, user_id) — кто состоит в чате
        self.welcomed: list[tuple[int, str]] = []  # (chat_id, invite_code)
        self.notified: list[dict] = []  # аргументы notify_expense
        self.photos: dict[str, bytes] = {}  # file_id -> байты фото
        # Что вернёт notify_expense, если есть фото; None — Telegram фото не принял
        self.notify_file_id: str | None = None
        self.telegram_down = False  # True — проверка членства падает, как при сбое Telegram

    async def is_member(self, chat_id: int, user_id: int) -> bool:
        if self.telegram_down:
            raise TelegramUnavailable()
        return (chat_id, user_id) in self.members

    async def send_welcome(self, chat_id: int, invite_code: str) -> None:
        self.welcomed.append((chat_id, invite_code))

    async def notify_expense(
        self, chat_id: int, text: str, link: str, photo: bytes | None
    ) -> str | None:
        self.notified.append({"chat_id": chat_id, "text": text, "link": link, "photo": photo})
        if photo is None or self.notify_file_id is None:
            return None
        self.photos[self.notify_file_id] = photo  # «Telegram» запомнил фото под этим file_id
        return self.notify_file_id

    async def download_photo(self, file_id: str) -> bytes:
        return self.photos[file_id]


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def client(gateway):
    """Приложение с базой в памяти и FakeGateway; в сеть не ходит."""
    app = create_app(TEST_SETTINGS, gateway, setup_webhook=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db(client):
    """Фабрика сессий базы приложения: подготовить данные и проверить результат.

    Открывай сессию на короткое время и закрывай до следующего запроса:
        with db() as s:
            ...
            s.commit()
    """
    return client.app.state.session_factory


@pytest.fixture
def make_chat(db, gateway):
    """Заводит чат и делает пользователей его участниками. Возвращает invite_code."""

    def make(
        chat_id: int = -1001,
        title: str = "Друзья",
        members: tuple[int, ...] = (),
        bot_active: bool = True,
    ) -> str:
        with db() as s:
            chat = services.register_chat(s, chat_id, title)
            chat.bot_active = bot_active
            s.commit()
            invite_code = chat.invite_code
        gateway.members.update((chat_id, user_id) for user_id in members)
        return invite_code

    return make


@pytest.fixture
def auth_headers():
    """auth_headers(user_id, start_param) -> заголовки с подписанной initData."""

    def make(user_id: int, start_param: str | None, first_name: str = "Тест") -> dict[str, str]:
        init_data = make_init_data(
            TEST_SETTINGS.bot_token, user_id, first_name=first_name, start_param=start_param
        )
        return {"X-Init-Data": init_data}

    return make
