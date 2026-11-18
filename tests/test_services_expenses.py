import pytest
from sqlalchemy import select

from app import services
from app.auth import TgUser
from app.calc import ItemInput
from app.models import Chat, Claim
from app.services import ExpenseDraft, ItemDraft, ServiceError


def make_user(session, user_id, name="Имя"):
    return services.upsert_user(session, TgUser(user_id, name, None, None, None))


@pytest.fixture
def event(session):
    """Событие в чате; автор события (id 1) — единственный участник."""
    make_user(session, 1)
    services.register_chat(session, -100, "Чат")
    return services.create_event(session, -100, "Поездка", 1)


def draft(**changes) -> ExpenseDraft:
    values = dict(title="Ужин", payer_id=1, tip_percent=0, items=[ItemDraft("Пицца", 90000, 1, False)])
    values.update(changes)
    return ExpenseDraft(**values)


def participant_ids(event):
    return sorted(p.user_id for p in event.participants)


def test_upsert_user_creates_and_updates(session):
    services.upsert_user(session, TgUser(5, "Аня", None, "anya", None))
    user = services.upsert_user(session, TgUser(5, "Анна", "Ли", "anna", "http://p"))
    assert (user.first_name, user.last_name, user.username, user.photo_url) == ("Анна", "Ли", "anna", "http://p")


def test_register_chat_keeps_invite_code(session):
    chat = services.register_chat(session, -1, "A")
    code = chat.invite_code
    services.deactivate_chat(session, -1)
    assert chat.bot_active is False
    again = services.register_chat(session, -1, "B")
    assert again.bot_active is True and again.title == "B" and again.invite_code == code
    assert services.get_chat_by_code(session, code) is again
    assert services.get_chat_by_code(session, "nope") is None


def test_migrate_chat_moves_chat_and_events(session, event):
    chat = session.get(Chat, -100)
    code = chat.invite_code
    services.deactivate_chat(session, -100)

    services.migrate_chat(session, -100, -1009)

    assert session.get(Chat, -100) is None
    moved = session.get(Chat, -1009)
    assert (moved.title, moved.invite_code, moved.bot_active) == ("Чат", code, False)
    assert event.chat_id == -1009
    assert services.get_chat_by_code(session, code) is moved


def test_migrate_chat_merges_into_chat_created_meanwhile(session, event):
    code = session.get(Chat, -100).invite_code
    services.register_chat(session, -1009, "Чат 2.0")  # /app в новой супергруппе до переноса

    services.migrate_chat(session, -100, -1009)

    assert session.get(Chat, -100) is None
    merged = session.get(Chat, -1009)
    assert merged.invite_code == code  # закреплённые в чате кнопки со старым кодом работают
    assert merged.title == "Чат 2.0"
    assert event.chat_id == -1009


def test_migrate_unknown_chat_does_nothing(session):
    services.migrate_chat(session, -5, -6)
    assert session.get(Chat, -5) is None and session.get(Chat, -6) is None


def test_create_event_joins_creator_and_join_is_idempotent(session, event):
    make_user(session, 2)
    services.join_event(session, event.id, 2)
    services.join_event(session, event.id, 2)
    assert participant_ids(event) == [1, 2]
    with pytest.raises(ServiceError):
        services.create_event(session, -100, "  ", 1)
    with pytest.raises(ServiceError):
        services.create_event(session, -999, "Нет чата", 1)


def test_create_event_rejects_long_title(session, event):
    with pytest.raises(ServiceError, match="не длиннее 100"):
        services.create_event(session, -100, "я" * 101, 1)
    assert services.create_event(session, -100, " " + "я" * 100 + " ", 1).title == "я" * 100


def test_create_expense_joins_author_and_payer(session, event):
    make_user(session, 2)  # не участник события, создаёт расход сам и платит сам
    expense = services.create_expense(session, event.id, 2, draft(payer_id=2))
    assert expense.payer_id == 2 and expense.created_by == 2 and expense.status == "open"
    assert participant_ids(event) == [1, 2]
    make_user(session, 3)  # автор — не участник, платит участник
    services.create_expense(session, event.id, 3, draft(payer_id=1))
    assert participant_ids(event) == [1, 2, 3]


@pytest.mark.parametrize(
    "bad",
    [
        dict(title="   "),
        dict(items=[]),
        dict(items=[ItemDraft("  ", 100, 1, False)]),
        dict(items=[ItemDraft("Чай", 0, 1, False)]),
        dict(items=[ItemDraft("Чай", 100, 0, False)]),
        dict(tip_percent=-1),
        dict(tip_percent=101),
        dict(title="я" * 101),
        dict(items=[ItemDraft("я" * 101, 100, 1, False)]),
        dict(items=[ItemDraft("Чай", 10_000_000_00 + 1, 1, False)]),
        dict(items=[ItemDraft("Чай", 100, 1001, False)]),
        dict(payer_id=2),  # существует, но не участник и не автор
        dict(payer_id=777),  # такого пользователя нет
    ],
)
def test_create_expense_validation(session, event, bad):
    make_user(session, 2)
    with pytest.raises(ServiceError):
        services.create_expense(session, event.id, 1, draft(**bad))
    assert event.expenses == []


def test_set_claim_upsert_and_remove(session, event):
    make_user(session, 2)
    expense = services.create_expense(session, event.id, 1, draft(items=[ItemDraft("Пиво", 15000, 6, False)]))
    item = expense.items[0]
    services.set_claim(session, item.id, 2, 2)
    services.set_claim(session, item.id, 2, 2)
    claims = session.scalars(select(Claim)).all()
    assert [(c.user_id, c.units) for c in claims] == [(2, 2)]
    assert participant_ids(event) == [1, 2]
    services.set_claim(session, item.id, 2, 3)
    assert session.scalars(select(Claim)).one().units == 3
    services.set_claim(session, item.id, 2, 0)
    assert session.scalars(select(Claim)).all() == []


def test_set_claim_rejects(session, event):
    expense = services.create_expense(
        session, event.id, 1, draft(items=[ItemDraft("Пиво", 100, 2, False), ItemDraft("Торт", 500, 1, True)])
    )
    beer, cake = expense.items
    for item_id, units in [(beer.id, 3), (beer.id, -1), (cake.id, 1), (99999, 1)]:
        with pytest.raises(ServiceError):
            services.set_claim(session, item_id, 1, units)
    expense.status = "closed"
    with pytest.raises(ServiceError):
        services.set_claim(session, beer.id, 1, 1)
    assert session.scalars(select(Claim)).all() == []


def test_only_author_can_edit_or_delete(session, event):
    make_user(session, 2)
    services.join_event(session, event.id, 2)
    expense = services.create_expense(session, event.id, 1, draft())
    with pytest.raises(ServiceError):
        services.update_expense(session, expense.id, 2, draft(title="Чужое"))
    with pytest.raises(ServiceError):
        services.delete_expense(session, expense.id, 2)
    assert expense.title == "Ужин"
    services.delete_expense(session, expense.id, 1)
    assert event.expenses == []


def test_cannot_edit_closed(session, event):
    expense = services.create_expense(session, event.id, 1, draft())
    expense.status = "closed"
    with pytest.raises(ServiceError):
        services.update_expense(session, expense.id, 1, draft(title="Новое"))
    with pytest.raises(ServiceError):
        services.delete_expense(session, expense.id, 1)


def test_delete_expense_removes_items_and_claims(session, event):
    expense = services.create_expense(session, event.id, 1, draft())
    services.set_claim(session, expense.items[0].id, 1, 1)
    services.delete_expense(session, expense.id, 1)
    assert session.scalars(select(Claim)).all() == []


def test_update_expense_qty_below_claims_keeps_invariant(session, event):
    make_user(session, 2)
    expense = services.create_expense(
        session, event.id, 1, draft(items=[ItemDraft("Пиво", 100, 6, False), ItemDraft("Чипсы", 50, 1, False)])
    )
    beer, chips = expense.items
    services.set_claim(session, beer.id, 1, 3)
    services.set_claim(session, beer.id, 2, 3)
    services.set_claim(session, chips.id, 1, 1)

    services.update_expense(
        session, expense.id, 1,
        draft(items=[ItemDraft("Пиво", 100, 4, False, id=beer.id), ItemDraft("Чипсы", 50, 1, False, id=chips.id)]),
    )
    result = services.expense_view(session, expense.id).result
    assert result.unclaimed == 0
    assert sum(result.shares.values()) == result.total == 450
    assert services.expense_view(session, expense.id).item_unclaimed[beer.id] == 0

    # позиция убрана из draft -> удалена вместе с отметками
    services.update_expense(session, expense.id, 1, draft(items=[ItemDraft("Чипсы", 50, 1, False, id=chips.id)]))
    assert [c.item_id for c in session.scalars(select(Claim)).all()] == [chips.id]
    assert [i.id for i in expense.items] == [chips.id]


def test_update_expense_adds_updates_and_validates(session, event):
    expense = services.create_expense(session, event.id, 1, draft())
    old_id = expense.items[0].id
    services.set_claim(session, old_id, 1, 1)
    services.update_expense(
        session, expense.id, 1,
        draft(title=" Обед ", tip_percent=10,
              items=[ItemDraft("Паста", 70000, 1, True, id=old_id), ItemDraft("Кофе", 20000, 2, False)]),
    )
    assert expense.title == "Обед" and expense.tip_percent == 10
    assert [(i.name, i.for_all) for i in expense.items] == [("Паста", True), ("Кофе", False)]
    assert session.scalars(select(Claim)).all() == []  # у позиции «на всех» отметок быть не может
    other = services.create_expense(session, event.id, 1, draft())
    with pytest.raises(ServiceError):  # позиция чужого расхода
        services.update_expense(session, expense.id, 1, draft(items=[ItemDraft("X", 1, 1, False, id=other.items[0].id)]))


def test_update_expense_rejected_draft_leaves_expense_unchanged(session, event):
    expense = services.create_expense(session, event.id, 1, draft())
    other = services.create_expense(session, event.id, 1, draft())
    item = expense.items[0]
    services.set_claim(session, item.id, 1, 1)
    with pytest.raises(ServiceError):
        services.update_expense(
            session, expense.id, 1,
            draft(title="Новое", items=[ItemDraft("Пицца", 90000, 1, True, id=item.id),
                                        ItemDraft("X", 1, 1, False, id=other.items[0].id)]),
        )
    assert expense.title == "Ужин" and item.for_all is False
    assert [(c.user_id, c.units) for c in session.scalars(select(Claim)).all()] == [(1, 1)]


def test_expense_view_numbers(session, event):
    for user_id in (2, 3, 10):
        make_user(session, user_id)
        services.join_event(session, event.id, user_id)
    expense = services.create_expense(
        session, event.id, 10, draft(
            payer_id=10, tip_percent=10,
            items=[ItemDraft("A", 65000, 1, False), ItemDraft("B", 90000, 1, False), ItemDraft("C", 20000, 2, False)],
        ),
    )
    a, b, c = expense.items
    services.set_claim(session, a.id, 1, 1)
    services.set_claim(session, b.id, 1, 1)
    services.set_claim(session, b.id, 2, 1)
    view = services.expense_view(session, expense.id)
    assert view.result.unclaimed == 44000
    assert view.result.total == 214500
    assert view.item_unclaimed == {a.id: 0, b.id: 0, c.id: 40000}
    assert view.event is event and view.expense is expense
    inp = services.expense_input(expense)
    assert inp.participants == [1, 2, 3, 10] and inp.leftover_mode is None and inp.payer_id == 10
    assert inp.items[0] == ItemInput(65000, 1, False, {1: 1})
    with pytest.raises(ServiceError):
        services.expense_view(session, 99999)


def test_user_share_splits_tips_and_leftover(session, event):
    make_user(session, 2)
    services.join_event(session, event.id, 2)
    expense = services.create_expense(
        session, event.id, 1, draft(tip_percent=10, items=[ItemDraft("Пиво", 25000, 4, False)])
    )
    services.set_claim(session, expense.items[0].id, 1, 1)

    open_share = services.user_share(expense, 1)  # 250 ₽ и 25 ₽ чаевых; остаток 825 ₽ пока ни у кого
    assert (open_share.total, open_share.tips, open_share.leftover) == (27500, 2500, 0)
    assert services.user_share(expense, 2).total == 0

    services.close_expense(session, expense.id, 1, "split")
    closed_share = services.user_share(expense, 1)  # остаток 825 ₽ поровну между двумя участниками
    assert (closed_share.total, closed_share.tips, closed_share.leftover) == (68750, 2500, 41250)
    assert services.user_share(expense, 2).leftover == 41250
