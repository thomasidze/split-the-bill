import pytest
from sqlalchemy.exc import IntegrityError

from app import services
from app.auth import TgUser
from app.models import Chat, Claim, Expense
from app.money import format_rub
from app.services import ExpenseDraft, ItemDraft

CHAT_ID = -1001
ANYA = 1
PETYA = 2
PHOTO = b"\xff\xd8\xff-fake-jpeg"


def create_event(db, title: str = "Баня 21.06", user_id: int = ANYA) -> int:
    with db() as s:
        services.upsert_user(s, TgUser(user_id, "Аня", None, None, None))
        event = services.create_event(s, CHAT_ID, title, user_id)
        s.commit()
        return event.id


def create_expense(db, event_id: int, items: list[ItemDraft], tip: int = 0, payer: int = ANYA) -> tuple[int, list[int]]:
    """Расход «Пиво» с заданными позициями; возвращает (id расхода, id позиций)."""
    with db() as s:
        draft = ExpenseDraft("Пиво", payer, tip, items)
        expense = services.create_expense(s, event_id, ANYA, draft)
        s.commit()
        return expense.id, [item.id for item in expense.items]


@pytest.fixture
def setup(client, make_chat, auth_headers, db):
    """Чат с Аней и Петей, событие Ани. Возвращает (event_id, заголовки Ани, заголовки Пети)."""
    code = make_chat(members=(ANYA, PETYA))
    event_id = create_event(db)
    anya = auth_headers(ANYA, code)
    petya = auth_headers(PETYA, code, first_name="Петя")
    client.post(f"/events/{event_id}/join", headers=petya)
    return event_id, anya, petya


def claim(client, item_id: int, units: int, headers):
    return client.post(f"/items/{item_id}/claim", data={"units": units}, headers=headers)


# --- экран и живое обновление ---


def test_expense_screen_header_and_polling(client, setup, db):
    event_id, anya, petya = setup
    expense_id, _ = create_expense(db, event_id, [ItemDraft("Лагер", 25000, 4, False)], tip=10)
    response = client.get(f"/view/expenses/{expense_id}", headers=petya)

    assert response.status_code == 200
    text = response.text
    assert "Пиво" in text and "Аня" in text  # название и плательщик
    assert format_rub(110000) in text  # 4 × 250 ₽ и 10 % чаевых
    assert f'data-back="/view/events/{event_id}"' in text
    assert f'hx-get="/view/expenses/{expense_id}/live"' in text
    assert 'hx-trigger="every 5s"' in text
    assert "data-photo-url" not in text  # фото нет — нет и заглушки
    assert "Закрыть расход" not in text  # закрывает только плательщик
    assert ">Открыт<" in text and ">Закрыт<" not in text


def test_live_shows_unclaimed_and_my_share(client, setup, db):
    event_id, anya, petya = setup
    expense_id, (item_id,) = create_expense(db, event_id, [ItemDraft("Лагер", 25000, 6, False)], tip=10)

    live = client.get(f"/view/expenses/{expense_id}/live", headers=petya)
    assert live.status_code == 200
    assert f"Не разобрано: {format_rub(165000)} из {format_rub(165000)}" in live.text
    assert "Твоя доля: 0 ₽" in live.text

    # Петя берёт 2 из 6: 500 ₽ + чаевые 10 % от всего чека, распределённые пропорционально разобранному и остатку
    live = claim(client, item_id, 2, petya)
    assert f"Не разобрано: {format_rub(110000)} из {format_rub(165000)}" in live.text
    assert f"Твоя доля: {format_rub(55000)} (включая чаевые {format_rub(5000)})" in live.text


def test_photo_placeholder_has_url(client, setup, db):
    event_id, anya, petya = setup
    expense_id, _ = create_expense(db, event_id, [ItemDraft("Лагер", 25000, 4, False)])
    with db() as s:
        s.get(Expense, expense_id).photo_file_id = "F1"
        s.commit()
    response = client.get(f"/view/expenses/{expense_id}", headers=petya)
    assert f'data-photo-url="/photos/{expense_id}"' in response.text
    assert 'src="/photos/' not in response.text  # <img src> не передаст заголовок X-Init-Data


# --- отметки ---


def test_claim_toggle_single(client, setup, db):
    event_id, anya, petya = setup
    expense_id, (item_id,) = create_expense(db, event_id, [ItemDraft("Пицца", 90000, 1, False)])

    on = claim(client, item_id, 1, petya)
    assert on.status_code == 200
    assert 'id="live"' in on.text  # в ответе фрагмент live
    assert "Петя" in on.text
    assert f"Твоя доля: {format_rub(90000)}" in on.text
    assert '"units": 0' in on.text  # повторное нажатие снимает отметку

    off = claim(client, item_id, 0, petya)
    assert off.status_code == 200
    assert "Петя" not in off.text
    assert "Твоя доля: 0 ₽" in off.text
    with db() as s:
        assert s.get(Claim, (item_id, PETYA)) is None


def test_claim_stepper_qty(client, setup, db):
    event_id, anya, petya = setup
    expense_id, (item_id,) = create_expense(db, event_id, [ItemDraft("Пиво", 25000, 6, False)], tip=10)

    live = claim(client, item_id, 2, petya)
    assert live.status_code == 200
    # 6 × 250 ₽ + 10 % = 1 650 ₽; Петя взял 2 штуки — не разобрано на 2 × 250 ₽ (+ чаевые) меньше
    assert f"Не разобрано: {format_rub(110000)} из {format_rub(165000)}" in live.text
    assert "Петя ×2" in live.text
    with db() as s:
        assert s.get(Claim, (item_id, PETYA)).units == 2

    live = claim(client, item_id, 3, petya)
    assert "Петя ×3" in live.text and "Петя ×2" not in live.text


def test_claim_adds_user_to_event(client, setup, db, make_chat, auth_headers):
    event_id, _, _ = setup
    _, (item_id,) = create_expense(db, event_id, [ItemDraft("Пицца", 90000, 1, False)])
    newcomer = 3
    code = make_chat(members=(ANYA, PETYA, newcomer))  # тот же чат: invite_code не меняется
    # новичок отмечается, не нажимая «Я участвую»
    assert claim(client, item_id, 1, auth_headers(newcomer, code, first_name="Вася")).status_code == 200
    with db() as s:
        event = services._get_event(s, event_id)
        assert newcomer in [p.user_id for p in event.participants]


def test_for_all_item_has_no_toggle(client, setup, db):
    event_id, anya, petya = setup
    expense_id, (item_id,) = create_expense(db, event_id, [ItemDraft("Чипсы", 65050, 1, True)])
    live = client.get(f"/view/expenses/{expense_id}/live", headers=petya)
    assert "делится на всех (2 чел.)" in live.text
    assert f"/items/{item_id}/claim" not in live.text
    assert claim(client, item_id, 1, petya).status_code == 400


@pytest.mark.parametrize("units", ["9", "-1", "abc"])
def test_bad_units_400(client, setup, db, units):
    event_id, anya, petya = setup
    _, (item_id,) = create_expense(db, event_id, [ItemDraft("Пицца", 90000, 1, False)])
    response = client.post(f"/items/{item_id}/claim", data={"units": units}, headers=petya)
    assert response.status_code == 400


def test_claim_race_is_retried(client, setup, db, monkeypatch):
    """Двойное нажатие: первая вставка отметки упирается в первичный ключ — пробуем ещё раз."""
    event_id, anya, petya = setup
    _, (item_id,) = create_expense(db, event_id, [ItemDraft("Пицца", 90000, 1, False)])
    real_set_claim = services.set_claim
    calls = []

    def flaky(s, *args):
        calls.append(args)
        if len(calls) == 1:
            raise IntegrityError("INSERT INTO claims", {}, Exception("UNIQUE constraint failed"))
        real_set_claim(s, *args)

    monkeypatch.setattr(services, "set_claim", flaky)
    response = claim(client, item_id, 1, petya)
    assert response.status_code == 200
    assert len(calls) == 2
    with db() as s:
        assert s.get(Claim, (item_id, PETYA)).units == 1


def test_claim_race_twice_gives_409(client, setup, db, monkeypatch):
    event_id, anya, petya = setup
    _, (item_id,) = create_expense(db, event_id, [ItemDraft("Пицца", 90000, 1, False)])

    def always_fails(s, *args):
        raise IntegrityError("INSERT INTO claims", {}, Exception("UNIQUE constraint failed"))

    monkeypatch.setattr(services, "set_claim", always_fails)
    response = claim(client, item_id, 1, petya)
    assert response.status_code == 409
    assert response.text == "Попробуй ещё раз"


# --- закрытие ---


def test_payer_sees_close_button_with_unclaimed(client, setup, db):
    event_id, anya, _ = setup
    expense_id, _ = create_expense(db, event_id, [ItemDraft("Лагер", 25000, 4, False)])
    response = client.get(f"/view/expenses/{expense_id}", headers=anya)
    assert "Закрыть расход" in response.text
    assert f'data-close-expense="/expenses/{expense_id}/close"' in response.text
    assert f'data-unclaimed="{100000}"' in response.text


def test_close_by_payer_split(client, setup, db):
    event_id, anya, petya = setup
    expense_id, (item_id,) = create_expense(db, event_id, [ItemDraft("Лагер", 25000, 4, False)])
    claim(client, item_id, 1, petya)

    response = client.post(f"/expenses/{expense_id}/close", data={"leftover_mode": "split"}, headers=anya)
    assert response.status_code == 200
    assert ">Закрыт<" in response.text  # статус; в открытом расходе здесь «Открыт»
    assert "Остаток поделён поровну между участниками" in response.text
    assert "every 5s" not in response.text
    assert "Закрыть расход" not in response.text
    assert f"/items/{item_id}/claim" not in response.text
    with db() as s:
        expense = s.get(Expense, expense_id)
        assert (expense.status, expense.leftover_mode) == ("closed", "split")

    live = client.get(f"/view/expenses/{expense_id}/live", headers=petya)
    assert ">Закрыт<" in live.text and "every 5s" not in live.text  # опрос прекращается
    # остаток 750 ₽ делится между двумя участниками: у Пети 250 + 375
    assert f"Твоя доля: {format_rub(62500)}" in live.text
    assert f"в том числе часть остатка: {format_rub(37500)}" in live.text

    after = claim(client, item_id, 0, petya)
    assert after.status_code == 400
    assert after.text == "Расход уже закрыт"


def test_close_leftover_stays_with_payer(client, setup, db):
    event_id, anya, petya = setup
    expense_id, _ = create_expense(db, event_id, [ItemDraft("Лагер", 25000, 4, False)])
    client.post(f"/expenses/{expense_id}/close", data={"leftover_mode": "payer"}, headers=anya)
    with db() as s:
        assert s.get(Expense, expense_id).leftover_mode == "payer"
    live = client.get(f"/view/expenses/{expense_id}/live", headers=petya)
    assert "Твоя доля: 0 ₽" in live.text


def test_close_without_unclaimed(client, setup, db):
    event_id, anya, petya = setup
    expense_id, (item_id,) = create_expense(db, event_id, [ItemDraft("Пицца", 90000, 1, False)])
    claim(client, item_id, 1, petya)
    page = client.get(f"/view/expenses/{expense_id}", headers=anya)
    assert 'data-unclaimed="0"' in page.text  # app.js спросит только «Закрыть расход?», без выбора режима остатка
    assert client.post(f"/expenses/{expense_id}/close", data={"leftover_mode": "split"}, headers=anya).status_code == 200


def test_closed_without_leftover_has_no_leftover_note(client, setup, db):
    event_id, anya, petya = setup
    expense_id, (item_id,) = create_expense(db, event_id, [ItemDraft("Пицца", 90000, 1, False)])
    claim(client, item_id, 1, petya)
    response = client.post(f"/expenses/{expense_id}/close", data={"leftover_mode": "split"}, headers=anya)
    assert ">Закрыт<" in response.text
    assert "Остаток" not in response.text and "часть остатка" not in response.text


def test_close_by_non_payer_400(client, setup, db):
    event_id, anya, petya = setup
    expense_id, _ = create_expense(db, event_id, [ItemDraft("Лагер", 25000, 4, False)])
    response = client.post(f"/expenses/{expense_id}/close", data={"leftover_mode": "split"}, headers=petya)
    assert response.status_code == 400
    assert response.text == "Закрыть расход может только тот, кто платил"
    with db() as s:
        assert s.get(Expense, expense_id).status == "open"


def test_poll_does_not_cancel_claims(client, setup, db):
    event_id, anya, petya = setup
    expense_id, _ = create_expense(
        db, event_id, [ItemDraft("Лагер", 25000, 4, False), ItemDraft("Чипсы", 15000, 1, False)]
    )
    text = client.get(f"/view/expenses/{expense_id}/live", headers=petya).text
    # Опрос пропускается, пока идёт другой запрос; нажатие «Моё» / «−» / «+» заменяет опрос
    assert 'id="live" hx-target="#live" hx-swap="outerHTML" hx-sync="this:drop"' in text
    assert text.count('hx-sync="#live:replace"') == 3
    assert text.count('hx-post="/items/') == 3


def test_close_without_mode_400(client, setup, db):
    event_id, anya, _ = setup
    expense_id, _ = create_expense(db, event_id, [ItemDraft("Лагер", 25000, 4, False)])
    assert client.post(f"/expenses/{expense_id}/close", headers=anya).status_code == 400
    assert client.post(f"/expenses/{expense_id}/close", data={"leftover_mode": "x"}, headers=anya).status_code == 400


# --- вход по ссылке ---


def test_start_param_with_expense_opens_expense_screen(client, setup, db, make_chat, auth_headers):
    event_id, anya, petya = setup
    expense_id, _ = create_expense(db, event_id, [ItemDraft("Лагер", 25000, 4, False)])
    with db() as s:
        code = s.get(Chat, CHAT_ID).invite_code
    response = client.get("/view/start", headers=auth_headers(PETYA, f"{code}_e{expense_id}"))
    assert response.status_code == 200
    assert f"/view/expenses/{expense_id}/live" in response.text


def test_start_param_with_deleted_expense_opens_home(client, setup, db, auth_headers):
    event_id, anya, petya = setup
    expense_id, _ = create_expense(db, event_id, [ItemDraft("Лагер", 25000, 4, False)])
    with db() as s:
        code = s.get(Chat, CHAT_ID).invite_code
    client.post(f"/expenses/{expense_id}/delete", headers=anya)

    response = client.get("/view/start", headers=auth_headers(PETYA, f"{code}_e{expense_id}"))
    assert response.status_code == 200
    assert "Баня 21.06" in response.text  # главная со списком событий
    assert "/live" not in response.text


# --- чужой чат ---


def test_expense_from_other_chat_is_404(client, setup, db, make_chat, auth_headers, gateway):
    event_id, _, _ = setup
    expense_id, (item_id,) = create_expense(db, event_id, [ItemDraft("Лагер", 25000, 4, False)])
    with db() as s:
        s.get(Expense, expense_id).photo_file_id = "F1"
        s.commit()
    gateway.photos["F1"] = PHOTO
    other_code = make_chat(chat_id=-2002, title="Работа", members=(PETYA,))
    other = auth_headers(PETYA, other_code)

    for url in (
        f"/view/expenses/{expense_id}",
        f"/view/expenses/{expense_id}/live",
        f"/view/events/{event_id}",
        f"/photos/{expense_id}",
    ):
        response = client.get(url, headers=other)
        assert response.status_code == 404, url
        assert response.content != PHOTO and "Пиво" not in response.text
    # Ссылка на чужой расход открывает главную своего чата, без данных чужого
    start = client.get("/view/start", headers=auth_headers(PETYA, f"{other_code}_e{expense_id}"))
    assert start.status_code == 200
    assert "Работа" in start.text
    assert "Пиво" not in start.text and "Баня 21.06" not in start.text
    assert claim(client, item_id, 1, other).status_code == 404
    close = client.post(f"/expenses/{expense_id}/close", data={"leftover_mode": "split"}, headers=other)
    assert close.status_code == 404
    with db() as s:
        assert s.get(Claim, (item_id, PETYA)) is None
        assert s.get(Expense, expense_id).status == "open"
