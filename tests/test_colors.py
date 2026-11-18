from app.colors import EVENT_COLORS, PERSON_COLORS, event_color, initial, person_color
from app.models import Event, User


def test_event_color_is_stable_and_cycles():
    first = event_color(Event(id=1))
    assert event_color(Event(id=1)) == first  # у события всегда один цвет
    assert event_color(Event(id=1 + len(EVENT_COLORS))) == first
    assert len({event_color(Event(id=i)) for i in range(len(EVENT_COLORS))}) == len(EVENT_COLORS)


def test_event_color_has_soft_variant():
    color = event_color(Event(id=1))
    assert color.main.startswith("#")
    assert color.soft.startswith("rgba(")


def test_person_color_is_stable_and_cycles():
    first = person_color(User(id=10))
    assert person_color(User(id=10)) == first
    assert person_color(User(id=10 + len(PERSON_COLORS))) == first
    assert len({person_color(User(id=i)) for i in range(len(PERSON_COLORS))}) == len(PERSON_COLORS)


def test_initial_from_name():
    assert initial(User(id=1, first_name="аня")) == "А"


def test_initial_from_username():
    assert initial(User(id=1, username="petya")) == "P"


def test_initial_without_name():
    assert initial(User(id=1)) == "?"
