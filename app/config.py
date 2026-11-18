from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки приложения. Читаются из переменных окружения и файла .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str
    bot_username: str
    app_short_name: str
    app_url: str  # публичный HTTPS-адрес; "/" на конце убирается сам
    webhook_secret: str = Field(min_length=1)  # пустой секрет оставил бы вебхук открытым
    db_path: str = "data/app.db"

    @field_validator("app_url")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        """https://x.test/ -> https://x.test: иначе адреса склеились бы с двумя "/"."""
        return value.rstrip("/")
