import pytest

from app import services
from app.auth import TgUser
from app.models import Settlement
from app.money import format_rub
from app.services import ExpenseDraft, ItemDraft

CHAT_ID = -1001
ANYA = 1
PETYA = 2
DEBT = 132000  # Петя взял пиццу за 1 320 ₽, платила Аня


def create_event(db) -> int:
    with db() as s:
        services.upsert_user(s, TgUser(ANYA, "Аня", None, None, None))
        event = services.create_event(s, CHAT_ID, "Баня 21.06", ANYA)
        s.commit()
        return event.id


def create_expense(db, event_id: int, items: list[ItemDraft]) -> list[int]:
    """Расход, который оплатила Аня; возвращает id позиций."""
    with db() as s:
        expense = services.create_expense(s, event_id, ANYA, ExpenseDraft("Пицца", ANYA, 0, items))
        s.commit()
        return [item.id for item in expense.items]


def claim(db, item_id: int, user_id: int, units: int = 1) -> None:
    with db() as s:
        services.set_claim(s, item_id, user_id, units)
        s.commit()


@pytest.fixture
def setup(client, make_chat, auth_headers, db):
    """Петя должен Ане 1 320 ₽. Возвращает (event_id, заголовки Ани, заголовки Пети)."""
    code = make_chat(members=(ANYA, PETYA))
    event_id = create_event(db)
    anya = auth_headers(ANYA, code, first_name="Аня")
    petya = auth_headers(PETYA, code, first_name="Петя")
    client.post(f"/events/{event_id}/join", headers=petya)
    (item_id,) = create_expense(db, event_id, [ItemDraft("Пицца", DEBT, 1, False)])
    claim(db, item_id, PETYA)
    return event_id, anya, petya


def tab(client, event_id: int, headers):
    return client.get(f"/view/events/{event_id}?tab=transfers", headers=headers)


def pay(client, event_id: int, headers, amount: int = DEBT, to_user: int = ANYA):
    return client.post(
        f"/events/{event_id}/settlements", data={"to_user": to_user, "amount": amount}, headers=headers
    )


def test_transfers_tab_lists_transfers(client, setup):
    event_id, anya, petya = setup
    line = f"Петя → Аня: {format_rub(DEBT)}"

    as_debtor = tab(client, event_id, petya)
    assert as_debtor.status_code == 200
    assert line in as_debtor.text
    assert "Я перевёл" in as_debtor.text

    as_recipient = tab(client, event_id, anya)
    assert line in as_recipient.text
    assert "Я перевёл" not in as_recipient.text


def test_settlement_pending_then_confirmed(client, setup, db):
    event_id, anya, petya = setup

    response = pay(client, event_id, petya)
    assert response.status_code == 200
    assert "ожидает подтверждения" in response.text
    assert "Я перевёл" not in response.text  # по этой паре перевод уже заявлен
    assert "Подтвердить" not in response.text  # подтверждает не должник

    as_recipient = tab(client, event_id, anya)
    assert "ожидает подтверждения" in as_recipient.text
    assert "Подтвердить" in as_recipient.text
    with db() as s:
        settlement = s.query(Settlement).one()
        assert (settlement.from_user, settlement.to_user, settlement.amount) == (PETYA, ANYA, DEBT)
        settlement_id = settlement.id

    confirmed = client.post(f"/settlements/{settlement_id}/confirm", headers=anya)
    assert confirmed.status_code == 200
    assert "Петя → Аня" not in confirmed.text
    assert "ожидает подтверждения" not in confirmed.text
    assert "Все в расчёте 🎉" in confirmed.text


def test_pending_pair_shown_once(client, setup):
    event_id, anya, petya = setup
    pay(client, event_id, petya)
    line = f"Петя → Аня: {format_rub(DEBT)}"
    for headers in (anya, petya):
        text = tab(client, event_id, headers).text
        assert text.count(line) == 1  # только строка «ожидает подтверждения», без рекомендации
        assert "ожидает подтверждения" in text


def test_everyone_settled_message(client, make_chat, auth_headers, db):
    code = make_chat(members=(ANYA,))
    event_id = create_event(db)
    response = tab(client, event_id, auth_headers(ANYA, code))
    assert response.status_code == 200
    assert "Все в расчёте 🎉" in response.text


def test_hint_while_expenses_open(client, setup, db):
    event_id, anya, petya = setup
    create_expense(db, event_id, [ItemDraft("Суши", 50000, 1, False)])
    response = tab(client, event_id, petya)
    assert f"Пока не разобрано {format_rub(50000)} — суммы могут измениться" in response.text


def test_no_hint_when_everything_claimed(client, setup):
    event_id, anya, petya = setup
    assert "Пока не разобрано" not in tab(client, event_id, petya).text


def test_cannot_settle_as_someone_else(client, setup, db):
    event_id, anya, petya = setup
    # Аня не должна Пете, поэтому её «Я перевёл» отклоняется: from_user — всегда сам пользователь
    response = pay(client, event_id, anya, to_user=PETYA)
    assert response.status_code == 400
    assert response.text == "Этот перевод не нужен"


def test_only_recipient_confirms(client, setup, db):
    event_id, anya, petya = setup
    pay(client, event_id, petya)
    with db() as s:
        settlement_id = s.query(Settlement).one().id
    response = client.post(f"/settlements/{settlement_id}/confirm", headers=petya)
    assert response.status_code == 400
    assert response.text == "Подтвердить перевод может только получатель"


def test_amount_must_be_number(client, setup):
    event_id, anya, petya = setup
    for bad in ("abc", "-5", "", "9" * 19):
        response = pay(client, event_id, petya, amount=bad)
        assert response.status_code == 400
        assert response.text == "Проверь введённые данные"


def test_settlements_from_other_chat_404(client, setup, make_chat, auth_headers, db):
    event_id, anya, petya = setup
    pay(client, event_id, petya)
    with db() as s:
        settlement_id = s.query(Settlement).one().id
    other_code = make_chat(chat_id=-2002, title="Работа", members=(ANYA,))
    outsider = auth_headers(ANYA, other_code, first_name="Аня")

    created = pay(client, event_id, outsider)
    assert created.status_code == 404
    assert created.text == "Не найдено"
    confirmed = client.post(f"/settlements/{settlement_id}/confirm", headers=outsider)
    assert confirmed.status_code == 404
    assert confirmed.text == "Не найдено"
    with db() as s:
        assert s.query(Settlement).one().status == "pending"
