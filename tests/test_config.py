import pytest
from pydantic import ValidationError

from app.config import Settings

VALID = dict(
    bot_token="1:T", bot_username="bot", app_short_name="app", app_url="https://x.test", webhook_secret="s"
)


def test_settings_accept_valid_values():
    assert Settings(**VALID).webhook_secret == "s"


def test_empty_webhook_secret_is_rejected():
    # Пустой секрет сделал бы вебхук открытым для любого, кто знает адрес
    with pytest.raises(ValidationError):
        Settings(**{**VALID, "webhook_secret": ""})


def test_trailing_slash_is_stripped_from_app_url():
    # Иначе адрес вебхука получился бы с двумя "/": https://x.test//tg/webhook
    assert Settings(**{**VALID, "app_url": "https://x.test/"}).app_url == "https://x.test"
