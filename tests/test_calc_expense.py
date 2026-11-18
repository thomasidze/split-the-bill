import pytest

from app.calc import ExpenseInput, ExpenseResult, ItemInput, allocate, expense_result, item_shares


def test_allocate_remainder_goes_to_lowest_key():
    assert allocate(1000, {1: 1, 2: 1, 3: 1}) == {1: 334, 2: 333, 3: 333}
    assert allocate(100, {5: 1, 2: 1, 9: 1}) == {2: 34, 5: 33, 9: 33}


def test_allocate_largest_remainder():
    assert allocate(10, {1: 1, 2: 2}) == {1: 3, 2: 7}  # 3.33 -> 3, 6.67 -> 7


def test_allocate_empty():
    assert allocate(500, {}) == {}


def test_item_partial_claims():  # пиво x6, взяли 2 и 2
    assert item_shares(ItemInput(15000, 6, False, {1: 2, 2: 2}), []) == ({1: 30000, 2: 30000}, 30000)


def test_item_overclaimed_shared():  # пицца x1 на троих
    assert item_shares(ItemInput(90000, 1, False, {1: 1, 2: 1, 3: 1}), []) == (
        {1: 30000, 2: 30000, 3: 30000},
        0,
    )


def test_item_overclaimed_proportional():
    assert item_shares(ItemInput(100, 6, False, {1: 4, 2: 4}), []) == ({1: 300, 2: 300}, 0)


def test_item_for_all():
    assert item_shares(ItemInput(500000, 1, True, {}), [1, 2, 3, 4, 5]) == ({u: 100000 for u in range(1, 6)}, 0)


def test_item_for_all_without_participants():
    assert item_shares(ItemInput(500000, 1, True, {}), []) == ({}, 500000)


ITEMS = [
    ItemInput(65000, 1, False, {1: 1}),
    ItemInput(90000, 1, False, {1: 1, 2: 1}),
    ItemInput(20000, 2, False, {}),
]


def test_expense_open_with_tips():
    r = expense_result(ExpenseInput(10, 10, ITEMS, [1, 2, 3, 10], None))
    assert r == ExpenseResult(total=214500, shares={1: 121000, 2: 49500}, unclaimed=44000)


def test_expense_closed_split():
    r = expense_result(ExpenseInput(10, 10, ITEMS, [1, 2, 3, 10], "split"))
    assert r.shares == {1: 132000, 2: 60500, 3: 11000, 10: 11000} and r.unclaimed == 0


def test_expense_closed_payer():
    r = expense_result(ExpenseInput(10, 10, ITEMS, [1, 2, 3, 10], "payer"))
    assert r.shares == {1: 121000, 2: 49500, 10: 44000} and r.unclaimed == 0


@pytest.mark.parametrize("mode", [None, "split", "payer"])
@pytest.mark.parametrize("tip", [0, 7, 15])
def test_invariant_sum(mode, tip):
    r = expense_result(ExpenseInput(10, tip, ITEMS, [1, 2, 3, 10], mode))
    assert sum(r.shares.values()) + r.unclaimed == r.total
