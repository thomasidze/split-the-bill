"""Денежная математика: только чистые функции, без базы данных и сети. Все суммы — в копейках."""

from dataclasses import dataclass

UNCLAIMED = 0  # ключ «не разобрано» при делении чаевых (настоящие user_id всегда больше нуля)


@dataclass(frozen=True)
class ItemInput:
    unit_price: int  # копейки
    qty: int
    for_all: bool
    claims: dict[int, int]  # user_id -> штуки


@dataclass(frozen=True)
class ExpenseInput:
    payer_id: int
    tip_percent: int
    items: list[ItemInput]
    participants: list[int]  # участники события
    leftover_mode: str | None  # None (открыт), "split", "payer"


@dataclass(frozen=True)
class ExpenseResult:
    total: int  # позиции + чаевые
    shares: dict[int, int]  # user_id -> доля с чаевыми (включая долю плательщика)
    unclaimed: int  # с чаевыми; 0 у закрытого


@dataclass(frozen=True)
class SettlementInput:
    from_user: int
    to_user: int
    amount: int  # копейки
    confirmed: bool


@dataclass(frozen=True)
class Transfer:
    from_user: int
    to_user: int
    amount: int  # копейки


def allocate(total: int, weights: dict[int, int]) -> dict[int, int]:
    """Делит total по весам без потери копеек.

    Каждому достаётся целая часть total * вес / сумма весов, а оставшиеся копейки
    раздаются по одной тем, у кого дробный остаток больше (при равенстве — меньшему ключу).
    """
    weights = {key: w for key, w in weights.items() if w > 0}
    weight_sum = sum(weights.values())
    if not weights:
        return {}
    result = {key: total * w // weight_sum for key, w in weights.items()}
    leftover = total - sum(result.values())
    by_remainder = sorted(weights, key=lambda key: (-(total * weights[key] % weight_sum), key))
    for key in by_remainder[:leftover]:
        result[key] += 1
    return result


def item_shares(item: ItemInput, participants: list[int]) -> tuple[dict[int, int], int]:
    """Возвращает (доли по позиции без чаевых, сумма «не разобрано»)."""
    price = item.unit_price * item.qty
    if item.for_all:
        if not participants:
            return {}, price
        return allocate(price, {user_id: 1 for user_id in participants}), 0

    claimed = sum(item.claims.values())
    if claimed > item.qty:  # разобрали больше, чем есть — делим цену позиции пропорционально
        return allocate(price, item.claims), 0
    shares = {user_id: item.unit_price * units for user_id, units in item.claims.items() if units > 0}
    return shares, item.unit_price * (item.qty - claimed)


def expense_result(expense: ExpenseInput) -> ExpenseResult:
    items_total = sum(item.unit_price * item.qty for item in expense.items)
    tip_total = (items_total * expense.tip_percent + 50) // 100

    item_part: dict[int, int] = {}
    unclaimed_items = 0
    for item in expense.items:
        shares, unclaimed = item_shares(item, expense.participants)
        for user_id, amount in shares.items():
            item_part[user_id] = item_part.get(user_id, 0) + amount
        unclaimed_items += unclaimed

    tips = allocate(tip_total, {**item_part, UNCLAIMED: unclaimed_items})
    shares = {user_id: amount + tips.get(user_id, 0) for user_id, amount in item_part.items()}
    unclaimed = unclaimed_items + tips.get(UNCLAIMED, 0)

    if expense.leftover_mode == "split":
        if expense.participants:
            extra = allocate(unclaimed, {user_id: 1 for user_id in expense.participants})
        else:
            extra = {expense.payer_id: unclaimed}
        for user_id, amount in extra.items():
            shares[user_id] = shares.get(user_id, 0) + amount
        unclaimed = 0
    elif expense.leftover_mode == "payer":
        shares[expense.payer_id] = shares.get(expense.payer_id, 0) + unclaimed
        unclaimed = 0

    shares = {user_id: amount for user_id, amount in shares.items() if amount > 0}
    return ExpenseResult(total=items_total + tip_total, shares=shares, unclaimed=unclaimed)


def balances(expenses: list[tuple[int, ExpenseResult]], settlements: list[SettlementInput]) -> dict[int, int]:
    """Баланс каждого: больше нуля — ему должны, меньше нуля — он должен. Нулевые не возвращаются.

    expenses — пары (кто заплатил, результат расхода). Учитываются только подтверждённые переводы.
    """
    result: dict[int, int] = {}
    for payer_id, expense in expenses:
        for user_id, share in expense.shares.items():
            if user_id != payer_id:  # своя доля плательщика и «не разобрано» баланса не создают
                result[payer_id] = result.get(payer_id, 0) + share
                result[user_id] = result.get(user_id, 0) - share
    for settlement in settlements:
        if settlement.confirmed:
            result[settlement.from_user] = result.get(settlement.from_user, 0) + settlement.amount
            result[settlement.to_user] = result.get(settlement.to_user, 0) - settlement.amount
    return {user_id: amount for user_id, amount in result.items() if amount != 0}


def transfers(balances: dict[int, int]) -> list[Transfer]:
    """Список переводов «кто кому»: каждый раз самый большой должник платит самому большому кредитору."""
    left = dict(balances)
    result: list[Transfer] = []
    while True:
        debtors = [user_id for user_id, amount in left.items() if amount < 0]
        creditors = [user_id for user_id, amount in left.items() if amount > 0]
        if not debtors or not creditors:
            return result
        debtor = min(debtors, key=lambda user_id: (left[user_id], user_id))
        creditor = min(creditors, key=lambda user_id: (-left[user_id], user_id))
        amount = min(-left[debtor], left[creditor])
        result.append(Transfer(debtor, creditor, amount))
        left[debtor] += amount
        left[creditor] -= amount
