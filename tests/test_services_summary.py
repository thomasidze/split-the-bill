import pytest

from app import services
from app.auth import TgUser
from app.calc import Transfer
from app.services import ExpenseDraft, ItemDraft, ServiceError


def make_user(session, user_id, name="Имя"):
    return services.upsert_user(session, TgUser(user_id, name, None, None, None))


@pytest.fixture
def setup(session):
    """Расход из примера Task 2: платит 10, чаевые 10 %, участники 1, 2, 3, 10; не разобрано 44000."""
    make_user(session, 1)
    services.register_chat(session, -100, "Чат")
    event = services.create_event(session, -100, "Поездка", 1)
    for user_id in (2, 3, 10):
        make_user(session, user_id)
        services.join_event(session, event.id, user_id)
    expense = services.create_expense(
        session, event.id, 10, ExpenseDraft(
            title="Ужин", payer_id=10, tip_percent=10,
            items=[ItemDraft("A", 65000, 1, False), ItemDraft("B", 90000, 1, False), ItemDraft("C", 20000, 2, False)],
        ),
    )
    a, b, _ = expense.items
    services.set_claim(session, a.id, 1, 1)
    services.set_claim(session, b.id, 1, 1)
    services.set_claim(session, b.id, 2, 1)
    return event, expense


def test_close_only_payer(session, setup):
    event, expense = setup
    with pytest.raises(ServiceError):
        services.close_expense(session, expense.id, 1, "split")
    assert expense.status == "open" and expense.leftover_mode is None and expense.closed_at is None
    with pytest.raises(ServiceError):
        services.close_expense(session, expense.id, 10, "nope")
    with pytest.raises(ServiceError):
        services.close_expense(session, 99999, 10, "split")
    assert expense.status == "open"
    services.close_expense(session, expense.id, 10, "payer")
    assert expense.status == "closed" and expense.leftover_mode == "payer" and expense.closed_at is not None
    with pytest.raises(ServiceError):  # уже закрыт
        services.close_expense(session, expense.id, 10, "split")
    assert expense.leftover_mode == "payer"


def test_close_split_moves_leftover(session, setup):
    _, expense = setup
    assert services.expense_view(session, expense.id).result.unclaimed == 44000
    services.close_expense(session, expense.id, 10, "split")
    result = services.expense_view(session, expense.id).result
    assert result.shares == {1: 132000, 2: 60500, 3: 11000, 10: 11000} and result.unclaimed == 0


def test_event_summary_open_expense_counts_only_claimed(session, setup):
    event, expense = setup
    summary = services.event_summary(session, event.id)
    assert summary.event is event
    assert [e for e, _ in summary.expenses] == [expense]
    assert summary.total == 214500 and summary.unclaimed == 44000
    assert summary.balances == {10: 170500, 1: -121000, 2: -49500}
    assert summary.pending == []


def test_event_summary_transfers(session, setup):
    event, expense = setup
    services.close_expense(session, expense.id, 10, "split")
    summary = services.event_summary(session, event.id)
    assert summary.unclaimed == 0
    assert summary.transfers == [Transfer(1, 10, 132000), Transfer(2, 10, 60500), Transfer(3, 10, 11000)]


def test_event_summary_expenses_newest_first(session, setup):
    event, first = setup
    second = services.create_expense(
        session, event.id, 1, ExpenseDraft("Такси", 1, 0, [ItemDraft("Поездка", 50000, 1, True)])
    )
    summary = services.event_summary(session, event.id)
    assert [e for e, _ in summary.expenses] == [second, first]
    assert summary.total == 214500 + 50000


def test_settlement_flow(session, setup):
    event, expense = setup
    services.close_expense(session, expense.id, 10, "split")
    settlement = services.create_settlement(session, event.id, 1, 10, 132000)
    assert (settlement.status, settlement.from_user, settlement.to_user, settlement.amount) == ("pending", 1, 10, 132000)
    summary = services.event_summary(session, event.id)
    assert summary.pending == [settlement]
    assert summary.balances[1] == -132000  # баланс не изменился
    assert Transfer(1, 10, 132000) in summary.transfers
    for wrong_user in (1, 2, 99):
        with pytest.raises(ServiceError):
            services.confirm_settlement(session, settlement.id, wrong_user)
    assert settlement.status == "pending" and settlement.confirmed_at is None
    services.confirm_settlement(session, settlement.id, 10)
    assert settlement.status == "confirmed" and settlement.confirmed_at is not None
    summary = services.event_summary(session, event.id)
    assert 1 not in summary.balances and summary.pending == []
    assert summary.transfers == [Transfer(2, 10, 60500), Transfer(3, 10, 11000)]
    with pytest.raises(ServiceError):  # повторное подтверждение
        services.confirm_settlement(session, settlement.id, 10)
    with pytest.raises(ServiceError):
        services.confirm_settlement(session, 99999, 10)


def test_pending_newest_first(session, setup):
    event, expense = setup
    services.close_expense(session, expense.id, 10, "split")
    first = services.create_settlement(session, event.id, 1, 10, 100)
    second = services.create_settlement(session, event.id, 2, 10, 100)
    assert services.event_summary(session, event.id).pending == [second, first]


@pytest.mark.parametrize("amount", [0, -5, 132001])
def test_settlement_amount_bounds(session, setup, amount):
    event, expense = setup
    services.close_expense(session, expense.id, 10, "split")
    with pytest.raises(ServiceError):
        services.create_settlement(session, event.id, 1, 10, amount)
    assert services.event_summary(session, event.id).pending == []


def test_settlement_without_recommended_transfer_rejected(session, setup):
    event, expense = setup
    services.close_expense(session, expense.id, 10, "split")
    with pytest.raises(ServiceError):  # 10 никому не должен
        services.create_settlement(session, event.id, 10, 1, 100)
    with pytest.raises(ServiceError):
        services.create_settlement(session, 99999, 1, 10, 100)
    services.create_settlement(session, event.id, 1, 10, 1)  # меньше рекомендованного — можно


def test_duplicate_pending_settlement_rejected(session, setup):
    event, expense = setup
    services.close_expense(session, expense.id, 10, "split")
    first = services.create_settlement(session, event.id, 1, 10, 132000)
    with pytest.raises(ServiceError, match="Перевод уже ждёт подтверждения"):
        services.create_settlement(session, event.id, 1, 10, 132000)
    assert services.event_summary(session, event.id).pending == [first]
    services.confirm_settlement(session, first.id, 10)
    with pytest.raises(ServiceError):  # долга больше нет
        services.create_settlement(session, event.id, 1, 10, 1)


def test_chat_home_my_balance(session, setup):
    event, expense = setup
    services.close_expense(session, expense.id, 10, "split")
    later = services.create_event(session, -100, "Выходные", 1)
    cards = services.chat_home(session, -100, 1)
    assert [c.event for c in cards] == [later, event]
    assert (cards[0].total, cards[0].unclaimed, cards[0].my_balance) == (0, 0, 0)
    assert (cards[1].total, cards[1].unclaimed, cards[1].my_balance) == (214500, 0, -132000)
    assert services.chat_home(session, -100, 10)[1].my_balance == 203500
    assert services.chat_home(session, -999, 1) == []
