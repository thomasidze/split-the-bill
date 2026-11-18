import pytest

from app.auth import AuthError, MembershipChecker, TelegramUnavailable, validate_init_data
from tests.helpers import make_init_data, sign_fields

TOKEN = "123456:TEST-TOKEN"
NOW = 1_800_000_000


def test_valid_init_data():
    raw = make_init_data(TOKEN, user_id=42, first_name="Аня", start_param="ab12_e7", auth_date=NOW - 10)
    data = validate_init_data(raw, TOKEN, now=NOW)
    assert data.user.id == 42
    assert data.user.first_name == "Аня"
    assert data.start_param == "ab12_e7"
    assert data.auth_date == NOW - 10


def test_tampered_hash_rejected():
    raw = make_init_data(TOKEN, user_id=42, auth_date=NOW)
    tampered = raw.replace("%22id%22%3A42", "%22id%22%3A43")
    assert tampered != raw
    with pytest.raises(AuthError):
        validate_init_data(tampered, TOKEN, now=NOW)


def test_expired_rejected():
    raw = make_init_data(TOKEN, user_id=42, auth_date=NOW - 86401)
    with pytest.raises(AuthError):
        validate_init_data(raw, TOKEN, now=NOW)


def test_missing_user_rejected():
    # Подпись верная, но поля user нет.
    raw = sign_fields(TOKEN, {"auth_date": str(NOW)})
    with pytest.raises(AuthError):
        validate_init_data(raw, TOKEN, now=NOW)


def test_non_ascii_hash_rejected():
    raw = make_init_data(TOKEN, user_id=42, auth_date=NOW)
    broken = raw.rsplit("hash=", 1)[0] + "hash=%C3%A9%C3%A9"
    with pytest.raises(AuthError):
        validate_init_data(broken, TOKEN, now=NOW)


def test_future_auth_date_rejected():
    raw = make_init_data(TOKEN, user_id=42, auth_date=NOW + 61)
    with pytest.raises(AuthError):
        validate_init_data(raw, TOKEN, now=NOW)


async def test_membership_cached():
    calls = []
    now = [1000.0]

    async def fetch(chat_id: int, user_id: int) -> bool:
        calls.append((chat_id, user_id))
        return True

    checker = MembershipChecker(fetch, ttl=300, clock=lambda: now[0])
    assert await checker.is_member(1, 2) is True
    assert await checker.is_member(1, 2) is True
    assert len(calls) == 1
    now[0] += 301
    assert await checker.is_member(1, 2) is True
    assert len(calls) == 2


async def test_membership_error_is_not_cached():
    calls = []

    async def fetch(chat_id: int, user_id: int) -> bool:
        calls.append((chat_id, user_id))
        if len(calls) == 1:
            raise TelegramUnavailable()
        return True

    checker = MembershipChecker(fetch, ttl=300, clock=lambda: 1000.0)
    with pytest.raises(TelegramUnavailable):
        await checker.is_member(1, 2)
    assert await checker.is_member(1, 2) is True  # ошибку не запомнили — спросили снова
    assert len(calls) == 2
