"""Цвета оформления: у каждого события и у каждого человека свой, всегда один и тот же.

Цвет выбирается по id, поэтому в базе его хранить не нужно.
"""

from dataclasses import dataclass

from app.models import Event, User


@dataclass(frozen=True)
class EventColor:
    main: str  # заливка шапки и кнопок, на ней белый текст
    soft: str  # полупрозрачная подложка: на светлой и тёмной теме видна одинаково мягко


@dataclass(frozen=True)
class PersonColor:
    bg: str  # кружок с буквой
    fg: str  # буква


def _event_color(hex_color: str) -> EventColor:
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    return EventColor(main=hex_color, soft=f"rgba({r}, {g}, {b}, 0.16)")


EVENT_COLORS = [
    _event_color("#7F77DD"),  # фиолетовый
    _event_color("#D85A30"),  # коралловый
    _event_color("#1A8F6B"),  # бирюзовый
    _event_color("#D4537E"),  # розовый
    _event_color("#378ADD"),  # синий
    _event_color("#C27A12"),  # янтарный
]

PERSON_COLORS = [
    PersonColor("#FAC775", "#633806"),  # янтарный
    PersonColor("#9FE1CB", "#085041"),  # бирюзовый
    PersonColor("#F4C0D1", "#72243E"),  # розовый
    PersonColor("#CECBF6", "#3C3489"),  # фиолетовый
    PersonColor("#B5D4F4", "#0C447C"),  # синий
    PersonColor("#C0DD97", "#27500A"),  # зелёный
    PersonColor("#F5C4B3", "#712B13"),  # коралловый
    PersonColor("#F7C1C1", "#791F1F"),  # красный
]


def event_color(event: Event) -> EventColor:
    return EVENT_COLORS[event.id % len(EVENT_COLORS)]


def person_color(user: User) -> PersonColor:
    return PERSON_COLORS[user.id % len(PERSON_COLORS)]


def initial(user: User) -> str:
    """Буква для кружка: из имени, иначе из username."""
    for part in (user.first_name, user.last_name, user.username):
        if part:
            return part[0].upper()
    return "?"
