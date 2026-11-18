import pytest

from app import services
from app.auth import TgUser
from app.models import Chat, Event, Expense, User
from app.services import ExpenseDraft, ItemDraft
from tests.conftest import TEST_SETTINGS
from tests.helpers import make_init_data

CHAT_ID = -1001
ANYA = 1
PETYA = 2
PHOTO = b"\xff\xd8\xff-fake-jpeg"
PHOTO_LOST_TEXT = "Фото не удалось сохранить в Telegram"


def create_event(db, title: str = "Баня 21.06", user_id: int = ANYA) -> int:
    with db() as s:
        services.upsert_user(s, TgUser(user_id, "Аня", None, None, None))
        event = services.create_event(s, CHAT_ID, title, user_id)
        s.commit()
        return event.id


def create_expense(db, event_id: int, user_id: int = ANYA) -> int:
    """Расход «Пиво» с двумя позициями; возвращает id."""
    with db() as s:
        draft = ExpenseDraft(
            "Пиво",
            user_id,
            0,
            [ItemDraft("Лагер", 25000, 4, False), ItemDraft("Чипсы", 65050, 1, True)],
        )
        expense_id = services.create_expense(s, event_id, user_id, draft).id
        s.commit()
        return expense_id


def form(**changes) -> dict:
    """Поля формы с одной позицией; changes заменяют любое поле."""
    fields = {
        "title": "Ужин",
        "payer_id": str(ANYA),
        "tip_percent": "0",
        "item_id[]": [""],
        "item_name[]": ["Пицца"],
        "item_price[]": ["900"],
        "item_qty[]": ["1"],
    }
    fields.update(changes)
    return fields


@pytest.fixture
def setup(client, make_chat, auth_headers, db):
    """Чат с двумя участниками и событие Ани. Возвращает (event_id, заголовки Ани, заголовки Пети)."""
    code = make_chat(members=(ANYA, PETYA))
    event_id = create_event(db)
    return event_id, auth_headers(ANYA, code), auth_headers(PETYA, code, first_name="Петя")


def has_pending_write(db) -> bool:
    """Открыта ли в базе незавершённая запись (она держит блокировку SQLite).

    Тестовая база — одно общее соединение (StaticPool), поэтому «прочитать из другой сессии»
    ничего не докажет: смотрим на само соединение. Соединение не закрываем и не отпускаем: возврат
    в пул откатил бы ту самую запись, которую мы проверяем.
    """
    conn = db.kw["bind"].raw_connection()  # держим обёртку: иначе пул откатит запись
    return conn.driver_connection.in_transaction


def only_expense(db) -> Expense:
    with db() as s:
        expenses = s.query(Expense).all()
        assert len(expenses) == 1
        s.expunge_all()
        return expenses[0]


# --- формы ---


def test_new_form_lists_payers_and_defaults_to_me(client, setup):
    event_id, anya, petya = setup
    response = client.get(f"/view/events/{event_id}/expenses/new", headers=petya)
    assert response.status_code == 200
    # Петя ещё не участник события, но платить за него может — он в списке и выбран
    assert f'<option value="{PETYA}" selected>' in response.text
    assert f'<option value="{ANYA}">' in response.text
    assert f'hx-post="/events/{event_id}/expenses"' in response.text
    assert 'name="photo"' in response.text
    assert f'data-back="/view/events/{event_id}"' in response.text  # «Назад» — к событию


def test_edit_form_is_prefilled(client, setup, db):
    event_id, anya, _ = setup
    expense_id = create_expense(db, event_id)
    with db() as s:
        item_ids = [item.id for item in s.get(Expense, expense_id).items]

    response = client.get(f"/view/expenses/{expense_id}/edit", headers=anya)
    assert response.status_code == 200
    assert f'hx-post="/expenses/{expense_id}"' in response.text
    assert 'value="Лагер"' in response.text
    assert 'value="650,50"' in response.text  # цена в формате, который принимает parse_rub
    assert all(f'name="item_id[]" value="{i}"' in response.text for i in item_ids)
    assert f'hx-post="/expenses/{expense_id}/delete"' in response.text
    assert 'name="photo"' not in response.text  # фото отправляется только вместе с новым расходом
    assert f'data-back="/view/expenses/{expense_id}"' in response.text  # «Назад» — к расходу


def test_edit_form_from_other_chat_404(client, setup, make_chat, auth_headers, db):
    event_id, _, _ = setup
    expense_id = create_expense(db, event_id)
    other = auth_headers(PETYA, make_chat(chat_id=-2002, title="Работа", members=(PETYA,)))
    assert client.get(f"/view/expenses/{expense_id}/edit", headers=other).status_code == 404
    assert client.get(f"/view/events/{event_id}/expenses/new", headers=other).status_code == 404


# --- создание ---


def test_create_expense_notifies_chat(client, setup, gateway, db):
    event_id, anya, _ = setup
    response = client.post(f"/events/{event_id}/expenses", data=form(title="Пиво"), headers=anya)

    assert response.status_code == 200
    expense = only_expense(db)
    assert len(gateway.notified) == 1
    note = gateway.notified[0]
    assert note["chat_id"] == CHAT_ID
    assert note["link"].startswith("https://t.me/split_test_bot/split?startapp=")
    assert note["link"].endswith(f"_e{expense.id}")
    assert "Пиво" in note["text"] and "Баня 21.06" in note["text"] and "900 ₽" in note["text"]
    assert note["photo"] is None
    # в ответе — экран только что созданного расхода
    assert f"/view/expenses/{expense.id}/live" in response.text
    assert "Пиво" in response.text


def test_expense_is_committed_before_notification(client, setup, gateway, db):
    event_id, anya, _ = setup
    seen = []
    original = gateway.notify_expense

    async def notify(chat_id, text, link, photo):
        seen.append(has_pending_write(db))  # во время отправки записи в базу быть не должно
        return await original(chat_id, text, link, photo)

    gateway.notify_expense = notify
    client.post(f"/events/{event_id}/expenses", data=form(), headers=anya)
    assert seen == [False]
    assert only_expense(db).title == "Ужин"  # и расход при этом сохранён


def test_create_expense_with_photo_saves_file_id(client, setup, gateway, db):
    event_id, anya, _ = setup
    gateway.notify_file_id = "F1"
    response = client.post(
        f"/events/{event_id}/expenses",
        data=form(),
        files={"photo": ("check.jpg", PHOTO, "image/jpeg")},
        headers=anya,
    )
    assert response.status_code == 200
    assert PHOTO_LOST_TEXT not in response.text
    expense = only_expense(db)
    assert expense.photo_file_id == "F1"
    assert gateway.notified[0]["photo"] == PHOTO

    photo = client.get(f"/photos/{expense.id}", headers=anya)
    assert photo.status_code == 200
    assert photo.content == PHOTO
    assert photo.headers["content-type"] == "image/jpeg"


def test_empty_file_part_means_no_photo(client, setup, gateway, db):
    event_id, anya, _ = setup
    # так браузер отправляет форму, когда файл не выбран
    response = client.post(
        f"/events/{event_id}/expenses",
        data=form(),
        files={"photo": ("", b"", "application/octet-stream")},
        headers=anya,
    )
    assert response.status_code == 200
    assert gateway.notified[0]["photo"] is None


@pytest.mark.parametrize(
    ("content_type", "size", "message"),
    [
        ("application/pdf", 10, "Фото чека — это должна быть картинка"),
        ("image/jpeg", 10 * 1024 * 1024 + 1, "Фото больше 10 МБ"),
    ],
)
def test_bad_photo_400(client, setup, gateway, db, content_type, size, message):
    event_id, anya, _ = setup
    response = client.post(
        f"/events/{event_id}/expenses",
        data=form(),
        files={"photo": ("check", b"x" * size, content_type)},
        headers=anya,
    )
    assert response.status_code == 400
    assert response.text == message
    assert gateway.notified == []
    with db() as s:
        assert s.query(Expense).count() == 0


def test_photo_of_exactly_10_mb_is_accepted(client, setup, gateway):
    event_id, anya, _ = setup
    response = client.post(
        f"/events/{event_id}/expenses",
        data=form(),
        files={"photo": ("check", b"x" * (10 * 1024 * 1024), "image/jpeg")},
        headers=anya,
    )
    assert response.status_code == 200
    assert len(gateway.notified) == 1


def test_bad_price_shows_error(client, setup, gateway, db):
    event_id, anya, _ = setup
    response = client.post(
        f"/events/{event_id}/expenses", data=form(**{"item_price[]": ["abc"]}), headers=anya
    )
    assert response.status_code == 400
    assert response.text == "Позиция 1: Не похоже на сумму"
    assert gateway.notified == []
    with db() as s:
        assert s.query(Expense).count() == 0


def test_error_names_the_row(client, setup):
    event_id, anya, _ = setup
    data = form(
        **{
            "item_id[]": ["", ""],
            "item_name[]": ["Пицца", "Кола"],
            "item_price[]": ["900", "0"],
            "item_qty[]": ["1", "1"],
        }
    )
    response = client.post(f"/events/{event_id}/expenses", data=data, headers=anya)
    assert response.status_code == 400
    assert response.text == "Позиция 2: Сумма должна быть больше нуля"


def test_bad_qty_shows_error(client, setup):
    event_id, anya, _ = setup
    response = client.post(f"/events/{event_id}/expenses", data=form(**{"item_qty[]": ["1.5"]}), headers=anya)
    assert response.status_code == 400
    assert response.text == "Позиция 1: Количество — целое число"


def test_mismatched_item_lists_400(client, setup):
    event_id, anya, _ = setup
    response = client.post(f"/events/{event_id}/expenses", data=form(**{"item_qty[]": []}), headers=anya)
    assert response.status_code == 400
    assert response.text == "Проверь введённые данные"


def test_for_all_checkbox_uses_row_indexes(client, setup, db):
    event_id, anya, _ = setup
    data = form(
        **{
            "item_id[]": ["", ""],
            "item_name[]": ["Пицца", "Чаевые на чай"],
            "item_price[]": ["900", "100"],
            "item_qty[]": ["1", "2"],
            "item_for_all[]": ["1"],
        }
    )
    assert client.post(f"/events/{event_id}/expenses", data=data, headers=anya).status_code == 200
    with db() as s:
        items = s.query(Expense).one().items
        assert [(i.name, i.qty, i.for_all) for i in items] == [("Пицца", 1, False), ("Чаевые на чай", 2, True)]


def test_notify_failure_does_not_break_save(client, setup, gateway, db):
    event_id, anya, _ = setup
    gateway.notify_file_id = None  # FakeGateway вернёт None, как при ошибке отправки
    response = client.post(
        f"/events/{event_id}/expenses",
        data=form(),
        files={"photo": ("check.jpg", PHOTO, "image/jpeg")},
        headers=anya,
    )
    assert response.status_code == 200
    assert only_expense(db).photo_file_id is None
    assert PHOTO_LOST_TEXT in response.text  # автор знает, что фото не сохранилось


def test_no_photo_no_photo_note(client, setup, db):
    event_id, anya, _ = setup
    response = client.post(f"/events/{event_id}/expenses", data=form(), headers=anya)
    assert response.status_code == 200
    assert PHOTO_LOST_TEXT not in response.text


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"tip_percent": "10"}, 10),
        ({"tip_percent": "custom", "tip_custom": "12"}, 12),
        ({"tip_percent": "custom", "tip_custom": "0"}, 0),
        ({"tip_percent": "5", "tip_custom": "99"}, 5),  # своё значение игнорируется, пока выбран пресет
    ],
)
def test_tip(client, setup, db, fields, expected):
    event_id, anya, _ = setup
    assert client.post(f"/events/{event_id}/expenses", data=form(**fields), headers=anya).status_code == 200
    assert only_expense(db).tip_percent == expected


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"tip_percent": "custom", "tip_custom": ""}, "Чаевые — целое число от 0 до 100"),
        ({"tip_percent": "custom", "tip_custom": "abc"}, "Чаевые — целое число от 0 до 100"),
        ({"tip_percent": "custom", "tip_custom": "101"}, "Чаевые — от 0 до 100 %"),
        ({"tip_percent": "7"}, "Проверь введённые данные"),
    ],
)
def test_bad_tip_400(client, setup, fields, message):
    event_id, anya, _ = setup
    response = client.post(f"/events/{event_id}/expenses", data=form(**fields), headers=anya)
    assert response.status_code == 400
    assert response.text == message


def test_create_expense_in_other_chat_event_404(client, setup, make_chat, auth_headers, gateway, db):
    event_id, _, _ = setup
    other = auth_headers(PETYA, make_chat(chat_id=-2002, title="Работа", members=(PETYA,)))
    response = client.post(f"/events/{event_id}/expenses", data=form(), headers=other)
    assert response.status_code == 404
    assert gateway.notified == []
    with db() as s:
        assert s.query(Expense).count() == 0


# --- изменение и удаление ---


def test_edit_expense_keeps_item_ids(client, setup, db):
    event_id, anya, petya = setup
    client.post(f"/events/{event_id}/join", headers=petya)  # платить может только участник
    expense_id = create_expense(db, event_id)
    with db() as s:
        first_id = s.get(Expense, expense_id).items[0].id
    data = form(
        title="Пиво и чипсы",
        payer_id=str(PETYA),
        **{
            "item_id[]": [str(first_id), ""],
            "item_name[]": ["Лагер 0,5", "Орешки"],
            "item_price[]": ["300", "150,5"],
            "item_qty[]": ["3", "1"],
        },
    )
    response = client.post(f"/expenses/{expense_id}", data=data, headers=anya)

    assert response.status_code == 200
    assert "Пиво и чипсы" in response.text  # в ответе — экран расхода
    assert f"/view/expenses/{expense_id}/live" in response.text
    with db() as s:
        expense = s.get(Expense, expense_id)
        assert (expense.title, expense.payer_id) == ("Пиво и чипсы", PETYA)
        assert [(i.name, i.unit_price, i.qty) for i in expense.items] == [
            ("Лагер 0,5", 30000, 3),
            ("Орешки", 15050, 1),
        ]
        assert expense.items[0].id == first_id


def test_edit_does_not_notify_again(client, setup, gateway, db):
    event_id, anya, _ = setup
    expense_id = create_expense(db, event_id)
    client.post(f"/expenses/{expense_id}", data=form(), headers=anya)
    assert gateway.notified == []


def test_edit_and_delete_by_author_only(client, setup, db):
    event_id, anya, petya = setup
    expense_id = create_expense(db, event_id)

    edit = client.post(f"/expenses/{expense_id}", data=form(title="Взлом"), headers=petya)
    assert edit.status_code == 400
    assert edit.text == "Изменять расход может только его автор"
    delete = client.post(f"/expenses/{expense_id}/delete", headers=petya)
    assert delete.status_code == 400
    assert delete.text == "Изменять расход может только его автор"
    with db() as s:
        assert s.get(Expense, expense_id).title == "Пиво"


def test_author_deletes_expense_and_sees_event(client, setup, db):
    event_id, anya, _ = setup
    expense_id = create_expense(db, event_id)
    response = client.post(f"/expenses/{expense_id}/delete", headers=anya)
    assert response.status_code == 200
    assert "Баня 21.06" in response.text and "Расходов пока нет" in response.text
    with db() as s:
        assert s.get(Expense, expense_id) is None
        assert s.get(Event, event_id) is not None


def test_edit_and_delete_in_other_chat_404(client, setup, make_chat, auth_headers, db):
    event_id, _, _ = setup
    expense_id = create_expense(db, event_id)
    other = auth_headers(PETYA, make_chat(chat_id=-2002, title="Работа", members=(PETYA,)))
    assert client.post(f"/expenses/{expense_id}", data=form(), headers=other).status_code == 404
    assert client.post(f"/expenses/{expense_id}/delete", headers=other).status_code == 404
    with db() as s:
        assert s.get(Expense, expense_id) is not None


# --- фото ---


def test_photo_without_file_404(client, setup, db):
    event_id, anya, _ = setup
    expense_id = create_expense(db, event_id)
    response = client.get(f"/photos/{expense_id}", headers=anya)
    assert response.status_code == 404


def test_photo_download_failure_404(client, setup, db):
    event_id, anya, _ = setup
    expense_id = create_expense(db, event_id)
    with db() as s:
        s.get(Expense, expense_id).photo_file_id = "gone"  # FakeGateway про такой file_id не знает
        s.commit()
    response = client.get(f"/photos/{expense_id}", headers=anya)
    assert response.status_code == 404
    assert response.text == "Фото недоступно"


def test_photo_from_other_chat_404(client, setup, make_chat, auth_headers, gateway, db):
    event_id, _, _ = setup
    expense_id = create_expense(db, event_id)
    gateway.photos["F1"] = PHOTO
    with db() as s:
        s.get(Expense, expense_id).photo_file_id = "F1"
        s.commit()
    other = auth_headers(PETYA, make_chat(chat_id=-2002, title="Работа", members=(PETYA,)))
    response = client.get(f"/photos/{expense_id}", headers=other)
    assert response.status_code == 404
    assert response.content != PHOTO


# --- не держим запись в базе, пока ждём Telegram ---


def test_no_pending_write_while_downloading_photo(client, setup, gateway, db):
    event_id, _, _ = setup
    expense_id = create_expense(db, event_id)
    with db() as s:
        s.get(Expense, expense_id).photo_file_id = "F1"
        s.commit()
    newcomer = 3  # новичок: get_ctx запишет его в базу при этом запросе
    seen = []

    async def download(file_id):
        seen.append(has_pending_write(db))
        return PHOTO

    gateway.download_photo = download
    with db() as s:
        invite_code = s.query(Chat).one().invite_code
    gateway.members.add((CHAT_ID, newcomer))
    headers = {"X-Init-Data": make_init_data(TEST_SETTINGS.bot_token, newcomer, start_param=invite_code)}
    assert client.get(f"/photos/{expense_id}", headers=headers).status_code == 200
    assert seen == [False]
    with db() as s:
        assert s.get(User, newcomer) is not None


# --- слишком длинные числа ---

HUGE = "9" * 30


@pytest.mark.parametrize(
    "fields",
    [
        {"tip_percent": "custom", "tip_custom": HUGE},
        {"item_qty[]": [HUGE]},
        {"item_id[]": [HUGE]},
        {"payer_id": HUGE},
    ],
)
def test_huge_numbers_give_400(client, setup, fields):
    event_id, anya, _ = setup
    response = client.post(f"/events/{event_id}/expenses", data=form(**fields), headers=anya)
    assert response.status_code == 400


def test_huge_item_id_on_edit_400(client, setup, db):
    event_id, anya, _ = setup
    expense_id = create_expense(db, event_id)
    response = client.post(f"/expenses/{expense_id}", data=form(**{"item_id[]": [HUGE]}), headers=anya)
    assert response.status_code == 400


# --- форма изменения: те же правила, что и у POST ---


def test_edit_form_by_non_author_400(client, setup, db):
    event_id, _, petya = setup
    expense_id = create_expense(db, event_id)
    response = client.get(f"/view/expenses/{expense_id}/edit", headers=petya)
    assert response.status_code == 400
    assert response.text == "Изменять расход может только его автор"


def test_edit_form_of_closed_expense_400(client, setup, db):
    event_id, anya, _ = setup
    expense_id = create_expense(db, event_id)
    with db() as s:
        services.close_expense(s, expense_id, ANYA, "payer")
        s.commit()
    response = client.get(f"/view/expenses/{expense_id}/edit", headers=anya)
    assert response.status_code == 400
    assert response.text == "Расход уже закрыт"
