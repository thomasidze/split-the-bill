from app.calc import ExpenseResult, SettlementInput, Transfer, balances, transfers

R = ExpenseResult(total=214500, shares={1: 132000, 2: 60500, 3: 11000, 10: 11000}, unclaimed=0)


def test_balances_basic():
    assert balances([(10, R)], []) == {10: 203500, 1: -132000, 2: -60500, 3: -11000}


def test_balances_confirmed_settlement_counts_pending_not():
    s = [SettlementInput(1, 10, 132000, True), SettlementInput(2, 10, 60500, False)]
    assert balances([(10, R)], s) == {10: 71500, 2: -60500, 3: -11000}


def test_transfers_single_creditor():
    assert transfers({10: 203500, 1: -132000, 2: -60500, 3: -11000}) == [
        Transfer(1, 10, 132000), Transfer(2, 10, 60500), Transfer(3, 10, 11000)]


def test_transfers_greedy_multi():
    assert transfers({1: 500, 2: 300, 3: -600, 4: -200}) == [
        Transfer(3, 1, 500), Transfer(4, 2, 200), Transfer(3, 2, 100)]


def test_transfers_empty():
    assert transfers({}) == []


def test_balances_open_expense_ignores_unclaimed():
    open_expense = ExpenseResult(total=214500, shares={1: 121000, 2: 49500}, unclaimed=44000)
    result = balances([(10, open_expense)], [])
    assert result == {10: 170500, 1: -121000, 2: -49500}
    assert sum(result.values()) == 0


def test_transfers_tie_goes_to_lower_user_id():
    assert transfers({2: 100, 1: 100, 3: -200}) == [Transfer(3, 1, 100), Transfer(3, 2, 100)]
