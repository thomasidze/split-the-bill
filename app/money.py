import re

NBSP = " "
MINUS = "−"  # настоящий минус, а не дефис

_AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")


def format_rub(kopecks: int) -> str:
    """320000 -> '3 200 ₽', 135050 -> '1 350,50 ₽' (тысячи разделены неразрывным пробелом)."""
    sign = MINUS if kopecks < 0 else ""
    rubles, rest = divmod(abs(kopecks), 100)
    text = f"{rubles:,}".replace(",", NBSP)
    if rest:
        text += f",{rest:02d}"
    return f"{sign}{text} ₽"


def format_rub_input(kopecks: int) -> str:
    """65050 -> '650,50', 25000 -> '250': для поля формы, parse_rub читает это обратно."""
    rubles, rest = divmod(kopecks, 100)
    return f"{rubles},{rest:02d}" if rest else str(rubles)


def parse_rub(text: str) -> int:
    """Превращает ввод пользователя ('650,50', ' 1 200 ') в копейки."""
    cleaned = re.sub(r"\s", "", text).replace(",", ".")
    if not _AMOUNT_RE.match(cleaned):
        raise ValueError("Не похоже на сумму")
    rubles, _, fraction = cleaned.partition(".")
    kopecks = int(rubles) * 100 + int(fraction.ljust(2, "0") or 0)
    if kopecks <= 0:
        raise ValueError("Сумма должна быть больше нуля")
    return kopecks
