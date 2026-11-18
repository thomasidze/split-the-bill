import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiogram.utils.web_app import safe_parse_webapp_init_data

AUTH_ERROR_TEXT = "Открой приложение заново из чата"


@dataclass(frozen=True)
class TgUser:
    id: int
    first_name: str
    last_name: str | None
    username: str | None
    photo_url: str | None


@dataclass(frozen=True)
class InitData:
    user: TgUser
    start_param: str | None
    auth_date: int


class AuthError(Exception):
    """initData не прошла проверку: плохая подпись, устарела или без пользователя."""


def validate_init_data(raw: str, bot_token: str, now: int, max_age: int = 86400) -> InitData:
    """Проверяет подпись и возраст initData от Telegram и возвращает разобранные данные."""
    try:
        parsed = safe_parse_webapp_init_data(bot_token, raw)
    except (ValueError, TypeError):
        raise AuthError(AUTH_ERROR_TEXT) from None

    if parsed.user is None:
        raise AuthError(AUTH_ERROR_TEXT)

    auth_date = int(parsed.auth_date.timestamp())
    if now - auth_date > max_age or auth_date > now + 60:
        raise AuthError(AUTH_ERROR_TEXT)

    user = TgUser(
        id=parsed.user.id,
        first_name=parsed.user.first_name,
        last_name=parsed.user.last_name,
        username=parsed.user.username,
        photo_url=parsed.user.photo_url,
    )
    return InitData(user=user, start_param=parsed.start_param, auth_date=auth_date)


class TelegramUnavailable(Exception):
    """Telegram не ответил на проверку членства (сеть, перегрузка) — ответ неизвестен."""


class MembershipChecker:
    """Помнит ответ «состоит ли пользователь в чате» на ttl секунд.

    Ошибки (TelegramUnavailable) не запоминаются: при следующем запросе спросим снова.
    """

    def __init__(
        self,
        fetch: Callable[[int, int], Awaitable[bool]],
        ttl: float = 300,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._fetch = fetch
        self._ttl = ttl
        self._clock = clock
        self._cache: dict[tuple[int, int], tuple[bool, float]] = {}

    async def is_member(self, chat_id: int, user_id: int) -> bool:
        key = (chat_id, user_id)
        cached = self._cache.get(key)
        if cached is not None and self._clock() - cached[1] < self._ttl:
            return cached[0]
        result = await self._fetch(chat_id, user_id)
        self._cache[key] = (result, self._clock())
        return result
