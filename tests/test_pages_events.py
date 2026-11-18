from app import services
from app.auth import TgUser
from app.models import Event, EventParticipant, User
from app.money import format_rub
from app.services import ExpenseDraft, ItemDraft

CHAT_ID = -1001
ANYA = 1
PETYA = 2


def create_event(db, title: str = "Баня 21.06", user_id: int = ANYA) -> int:
    with db() as s:
        services.upsert_user(s, TgUser(user_id, "Аня", None, None, None))
        event = services.create_event(s, CHAT_ID, title, user_id)
        s.commit()
        return event.id


# --- доступ ---


def test_no_init_data_401(client, make_chat):
    make_chat(members=(ANYA,))
    response = client.get("/view/home")
    assert response.status_code == 401
    assert response.text == "Открой приложение заново из чата"


def test_bad_init_data_401(client, make_chat, auth_headers):
    code = make_chat(members=(ANYA,))
    headers = auth_headers(ANYA, code)
    headers["X-Init-Data"] = headers["X-Init-Data"].replace("hash=", "hash=0")
    response = client.get("/view/home", headers=headers)
    assert response.status_code == 401
    assert response.text == "Открой приложение заново из чата"


def test_unknown_chat_404(client, make_chat, auth_headers):
    make_chat(members=(ANYA,))
    response = client.get("/view/home", headers=auth_headers(ANYA, "nosuchcode"))
    assert response.status_code == 404
    assert response.text == "Открой приложение по кнопке в чате"


def test_not_member_403(client, make_chat, auth_headers):
    code = make_chat(members=(ANYA,))
    response = client.get("/view/home", headers=auth_headers(PETYA, code))
    assert response.status_code == 403
    assert response.text == "Это доступно только участникам чата"


def test_telegram_down_503(client, make_chat, auth_headers, gateway):
    code = make_chat(members=(ANYA,))
    gateway.telegram_down = True
    response = client.get("/view/home", headers=auth_headers(ANYA, code))
    assert response.status_code == 503
    assert response.text == "Telegram не отвечает, попробуй ещё раз"


def test_inactive_bot_403(client, make_chat, auth_headers):
    code = make_chat(members=(ANYA,), bot_active=False)
    response = client.get("/view/home", headers=auth_headers(ANYA, code))
    assert response.status_code == 403
    assert response.text == "Бот удалён из чата — добавьте его обратно"


def test_shell_loads_start_view(client):
    response = client.get("/")
    assert response.status_code == 200
    assert 'hx-get="/view/start"' in response.text


# --- вебхук ---


def test_webhook_wrong_secret_403(client):
    response = client.post(
        "/tg/webhook",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert response.status_code == 403


def test_webhook_feeds_update_to_bot(client, gateway, db):
    update = {
        "update_id": 1,
        "my_chat_member": {
            "chat": {"id": -555, "type": "group", "title": "Пятница"},
            "from": {"id": ANYA, "is_bot": False, "first_name": "Аня"},
            "date": 1_790_000_000,
            "old_chat_member": {"status": "left", "user": {"id": 99, "is_bot": True, "first_name": "Bot"}},
            "new_chat_member": {"status": "member", "user": {"id": 99, "is_bot": True, "first_name": "Bot"}},
        },
    }
    response = client.post(
        "/tg/webhook",
        json=update,
        headers={"X-Telegram-Bot-Api-Secret-Token": client.app.state.settings.webhook_secret},
    )
    assert response.status_code == 200
    assert [chat_id for chat_id, _ in gateway.welcomed] == [-555]


# --- главная и события ---


def test_create_event_and_see_on_home(client, make_chat, auth_headers):
    code = make_chat(members=(ANYA,))
    headers = auth_headers(ANYA, code)

    response = client.post("/events", data={"title": "Баня 21.06"}, headers=headers)
    assert response.status_code == 200
    assert "Баня 21.06" in response.text

    home = client.get("/view/home", headers=headers)
    assert home.status_code == 200
    assert "Баня 21.06" in home.text
    assert "Друзья" in home.text


def test_create_event_empty_title_400(client, make_chat, auth_headers):
    code = make_chat(members=(ANYA,))
    response = client.post("/events", data={"title": "  "}, headers=auth_headers(ANYA, code))
    assert response.status_code == 400
    assert response.text == "Введи название события"


def test_event_title_is_escaped(client, make_chat, auth_headers):
    code = make_chat(members=(ANYA,))
    headers = auth_headers(ANYA, code)
    client.post("/events", data={"title": "<b>Пиво & чипсы</b>"}, headers=headers)
    home = client.get("/view/home", headers=headers)
    assert "&lt;b&gt;Пиво &amp; чипсы&lt;/b&gt;" in home.text


def test_start_routes_to_expense(client, make_chat, auth_headers, db):
    code = make_chat(members=(ANYA,))
    event_id = create_event(db)
    with db() as s:
        draft = ExpenseDraft("Пиво", ANYA, 0, [ItemDraft("Пиво", 25000, 4, False)])
        expense_id = services.create_expense(s, event_id, ANYA, draft).id
        s.commit()
    response = client.get("/view/start", headers=auth_headers(ANYA, f"{code}_e{expense_id}"))
    assert response.status_code == 200
    assert f'/view/expenses/{expense_id}/live' in response.text


def test_start_without_expense_shows_home(client, make_chat, auth_headers, db):
    code = make_chat(members=(ANYA,))
    create_event(db)
    response = client.get("/view/start", headers=auth_headers(ANYA, code))
    assert response.status_code == 200
    assert "Баня 21.06" in response.text


def test_join_event(client, make_chat, auth_headers, db):
    code = make_chat(members=(ANYA, PETYA))
    event_id = create_event(db)
    petya = auth_headers(PETYA, code, first_name="Петя")

    before = client.get(f"/view/events/{event_id}", headers=petya)
    assert "Я участвую" in before.text

    response = client.post(f"/events/{event_id}/join", headers=petya)
    assert response.status_code == 200
    assert "Петя" in response.text
    assert "Я участвую" not in response.text
    with db() as s:
        assert s.get(EventParticipant, (event_id, PETYA)) is not None


def test_event_page_shows_unclaimed(client, make_chat, auth_headers, db):
    code = make_chat(members=(ANYA,))
    event_id = create_event(db)
    with db() as s:
        draft = ExpenseDraft("Пиво", ANYA, 0, [ItemDraft("Пиво", 25000, 4, False)])
        expense = services.create_expense(s, event_id, ANYA, draft)
        services.set_claim(s, expense.items[0].id, ANYA, 1)  # одна кружка из четырёх разобрана
        s.commit()

    response = client.get(f"/view/events/{event_id}", headers=auth_headers(ANYA, code))
    assert response.status_code == 200
    assert "Пиво" in response.text
    unclaimed, total = 75000, 100000  # не разобрано 3 × 250 ₽ из 4 × 250 ₽
    assert f"не разобрано {format_rub(unclaimed)} из {format_rub(total)}" in response.text


def test_event_from_other_chat_404(client, make_chat, auth_headers, db):
    make_chat(members=(ANYA,))
    other_code = make_chat(chat_id=-2002, title="Работа", members=(PETYA,))
    event_id = create_event(db)
    petya = auth_headers(PETYA, other_code)

    assert client.get(f"/view/events/{event_id}", headers=petya).status_code == 404
    response = client.post(f"/events/{event_id}/join", headers=petya)
    assert response.status_code == 404
    assert response.text == "Не найдено"
    with db() as s:
        assert s.get(EventParticipant, (event_id, PETYA)) is None
        assert s.get(Event, event_id).chat_id == CHAT_ID


def test_service_error_rolls_back_route_changes(client, make_chat, auth_headers, db):
    newcomer = 3
    code = make_chat(members=(newcomer,))
    response = client.post("/events", data={"title": ""}, headers=auth_headers(newcomer, code))
    assert response.status_code == 400
    with db() as s:
        assert s.query(Event).count() == 0
        assert s.get(User, newcomer) is not None  # пользователя get_ctx сохраняет сразу, до роута
