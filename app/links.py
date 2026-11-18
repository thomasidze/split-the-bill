def build_app_link(
    bot_username: str,
    app_short_name: str,
    invite_code: str,
    expense_id: int | None = None,
) -> str:
    """Ссылка на Mini App: чат целиком или конкретный расход."""
    start_param = invite_code if expense_id is None else f"{invite_code}_e{expense_id}"
    return f"https://t.me/{bot_username}/{app_short_name}?startapp={start_param}"


def parse_start_param(value: str | None) -> tuple[str | None, int | None]:
    """'ab12_e7' -> ('ab12', 7); 'ab12' -> ('ab12', None); None -> (None, None)."""
    if not value:
        return None, None
    invite_code, sep, tail = value.partition("_e")
    if sep and tail.isascii() and tail.isdigit():
        return invite_code, int(tail)
    return invite_code, None
