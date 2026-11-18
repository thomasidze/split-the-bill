import hashlib
import hmac
import json
import time
from urllib.parse import urlencode


def sign_fields(bot_token: str, fields: dict[str, str]) -> str:
    """Подписывает поля по алгоритму Telegram и возвращает готовую строку запроса."""
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    hash_ = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": hash_})


def make_init_data(
    bot_token: str,
    user_id: int,
    first_name: str = "Тест",
    start_param: str | None = None,
    auth_date: int | None = None,
) -> str:
    """Собирает строку initData и подписывает её так же, как Telegram."""
    fields = {
        "user": json.dumps({"id": user_id, "first_name": first_name}, ensure_ascii=False, separators=(",", ":")),
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
    }
    if start_param is not None:
        fields["start_param"] = start_param
    return sign_fields(bot_token, fields)
