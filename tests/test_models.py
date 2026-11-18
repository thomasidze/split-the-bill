from app.models import Chat, Claim, Event, Expense, Item, User


def make_expense_with_claim(session):
    chat = Chat(id=-100, title="Друзья")
    user = User(id=1, first_name="Аня")
    session.add_all([chat, user])
    session.flush()

    event = Event(chat_id=chat.id, title="Поход", created_by=user.id)
    session.add(event)
    session.flush()

    expense = Expense(
        event_id=event.id, title="Ужин", payer_id=user.id, created_by=user.id, tip_percent=10
    )
    expense.items.append(Item(name="Пицца", unit_price=65000, qty=2))
    session.add(expense)
    session.flush()

    expense.items[0].claims.append(Claim(user_id=user.id, units=1))
    session.commit()
    return expense


def test_cascade_deletes_items_and_claims(session):
    expense = make_expense_with_claim(session)
    assert session.query(Item).count() == 1
    assert session.query(Claim).count() == 1

    session.delete(expense)
    session.commit()

    assert session.query(Claim).count() == 0
    assert session.query(Item).count() == 0


def test_invite_code_generated(session):
    chat = Chat(id=-1, title="Чат")
    session.add(chat)
    session.commit()
    assert len(chat.invite_code) == 16


def test_invite_codes_are_unique(session):
    a, b = Chat(id=-1, title="А"), Chat(id=-2, title="Б")
    session.add_all([a, b])
    session.commit()
    assert a.invite_code != b.invite_code


def test_expense_defaults(session):
    expense = make_expense_with_claim(session)
    assert expense.status == "open"
    assert expense.leftover_mode is None
    assert expense.closed_at is None
    assert expense.photo_file_id is None
    assert expense.created_at is not None


def test_foreign_keys_enforced(session):
    import pytest
    from sqlalchemy.exc import IntegrityError

    session.add(Event(chat_id=999, title="Без чата", created_by=1))
    with pytest.raises(IntegrityError):
        session.commit()
