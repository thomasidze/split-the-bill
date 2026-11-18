import pytest

from app.money import format_rub, format_rub_input, parse_rub

NBSP = " "


def test_format_rub_whole():
    assert format_rub(320000) == f"3{NBSP}200 ₽"


def test_format_rub_kopecks():
    assert format_rub(135050) == f"1{NBSP}350,50 ₽"


def test_format_rub_zero():
    assert format_rub(0) == "0 ₽"


def test_format_rub_negative():
    assert format_rub(-45000) == "−450 ₽"


@pytest.mark.parametrize(
    "text,expected",
    [("650", 65000), ("650,50", 65050), ("650.5", 65050), (" 1 200 ", 120000), ("0,01", 1)],
)
def test_parse_rub_ok(text, expected):
    assert parse_rub(text) == expected


@pytest.mark.parametrize("text", ["", "0", "-5", "abc", "1,234", "1.2.3"])
def test_parse_rub_rejects(text):
    with pytest.raises(ValueError):
        parse_rub(text)


def test_format_rub_input_for_form_fields():
    assert format_rub_input(25000) == "250"
    assert format_rub_input(65050) == "650,50"
    assert format_rub_input(5) == "0,05"
    assert parse_rub(format_rub_input(65050)) == 65050
