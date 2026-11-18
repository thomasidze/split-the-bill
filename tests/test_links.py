from app.links import build_app_link, parse_start_param


def test_build_link_chat():
    assert build_app_link("splitbot", "app", "ab12") == "https://t.me/splitbot/app?startapp=ab12"


def test_build_link_expense():
    assert build_app_link("splitbot", "app", "ab12", 7) == "https://t.me/splitbot/app?startapp=ab12_e7"


def test_parse_start_param():
    assert parse_start_param("ab12_e7") == ("ab12", 7)
    assert parse_start_param("ab12") == ("ab12", None)
    assert parse_start_param(None) == (None, None)
    assert parse_start_param("ab12_eX") == ("ab12", None)
