"""Бизнес-логика: пользователи, чаты, события, расходы и отметки позиций.

Функции делают flush, но не commit — коммитит вызывающий (роут).
Проверки данных живут здесь: в базе нет CHECK-ограничений.
"""

from dataclasses import dataclass, replace

from sqlalchemy import select, update
from sqlalchemy.orm import Session, object_session

from app.auth import TgUser
from app.calc import (
    ExpenseInput,
    ExpenseResult,
    ItemInput,
    SettlementInput,
    Transfer,
    balances,
    expense_result,
    item_shares,
    transfers,
)
from app.models import Chat, Claim, Event, EventParticipant, Expense, Item, Settlement, User, utcnow

NOT_FOUND = "Не найдено"
MAX_NAME_LENGTH = 100
MAX_UNIT_PRICE = 10_000_000_00  # 10 млн ₽ в копейках
MAX_QTY = 1000


class ServiceError(Exception):
    """Ошибка, которую можно показать пользователю: сообщение — по-русски."""


@dataclass
class ItemDraft:
    name: str
    unit_price: int  # копейки
    qty: int
    for_all: bool
    id: int | None = None  # None — новая позиция


@dataclass
class ExpenseDraft:
    title: str
    payer_id: int
    tip_percent: int
    items: list[ItemDraft]


@dataclass
class ExpenseView:
    expense: Expense
    event: Event
    result: ExpenseResult
    item_unclaimed: dict[int, int]  # item_id -> «не разобрано» в копейках, без чаевых
    leftover: int = 0  # сколько было не разобрано в момент закрытия (с чаевыми); у открытого 0


@dataclass
class UserShare:
    total: int  # доля пользователя в расходе целиком
    tips: int  # из неё: чаевые с разобранного им
    leftover: int  # из неё: часть остатка, поделённого при закрытии (0 у открытого расхода)


@dataclass
class EventSummary:
    event: Event
    expenses: list[tuple[Expense, ExpenseResult]]  # новые сверху
    total: int
    unclaimed: int
    balances: dict[int, int]
    transfers: list[Transfer]  # рекомендованные переводы: учитывают только подтверждённые
    pending: list[Settlement]  # ожидают подтверждения, новые сверху


@dataclass
class EventCard:
    event: Event
    total: int
    unclaimed: int
    my_balance: int


# --- пользователи и чаты ---


def upsert_user(s: Session, tg: TgUser) -> User:
    user = s.get(User, tg.id)
    if user is None:
        user = User(id=tg.id)
        s.add(user)
    user.first_name = tg.first_name
    user.last_name = tg.last_name
    user.username = tg.username
    user.photo_url = tg.photo_url
    s.flush()
    return user


def register_chat(s: Session, chat_id: int, title: str) -> Chat:
    chat = s.get(Chat, chat_id)
    if chat is None:
        chat = Chat(id=chat_id, title=title)
        s.add(chat)
    chat.title = title
    chat.bot_active = True  # invite_code не меняем
    s.flush()
    return chat


def deactivate_chat(s: Session, chat_id: int) -> None:
    chat = s.get(Chat, chat_id)
    if chat is not None:
        chat.bot_active = False
        s.flush()


def migrate_chat(s: Session, old_id: int, new_id: int) -> None:
    """Группа стала супергруппой, и у чата сменился id: переносим чат и его события на new_id.

    invite_code остаётся старым — закреплённые в чате кнопки продолжают работать.
    Если строка с new_id уже есть (например, в новой супергруппе успели вызвать /app),
    события переезжают в неё, а её код заменяется старым; её название и bot_active свежее — их не трогаем.
    """
    old = s.get(Chat, old_id)
    if old is None or old_id == new_id:
        return  # про старый чат ничего не знаем — переносить нечего
    invite_code = old.invite_code

    new = s.get(Chat, new_id)
    if new is None:
        # Пока со своим (временным) кодом: два одинаковых кода запрещает unique
        new = Chat(id=new_id, title=old.title, bot_active=old.bot_active)
        s.add(new)
        s.flush()  # строка new_id должна появиться раньше, чем на неё сошлются события
    s.execute(update(Event).where(Event.chat_id == old_id).values(chat_id=new_id))
    s.delete(old)
    s.flush()  # старая строка удалена — её код освободился
    new.invite_code = invite_code
    s.flush()


def get_chat_by_code(s: Session, code: str) -> Chat | None:
    return s.scalar(select(Chat).where(Chat.invite_code == code))


# --- события ---


def _get_user(s: Session, user_id: int) -> User:
    user = s.get(User, user_id)
    if user is None:
        raise ServiceError(NOT_FOUND)
    return user


def _get_event(s: Session, event_id: int) -> Event:
    event = s.get(Event, event_id)
    if event is None:
        raise ServiceError(NOT_FOUND)
    return event


def _add_participant(event: Event, user_id: int) -> None:
    if all(p.user_id != user_id for p in event.participants):
        event.participants.append(EventParticipant(user_id=user_id))


def create_event(s: Session, chat_id: int, title: str, user_id: int) -> Event:
    title = title.strip()
    if not title:
        raise ServiceError("Введи название события")
    if len(title) > MAX_NAME_LENGTH:
        raise ServiceError(f"Название события — не длиннее {MAX_NAME_LENGTH} символов")
    if s.get(Chat, chat_id) is None:
        raise ServiceError(NOT_FOUND)
    _get_user(s, user_id)
    event = Event(chat_id=chat_id, title=title, created_by=user_id)
    event.participants.append(EventParticipant(user_id=user_id))  # создатель сразу участник
    s.add(event)
    s.flush()
    return event


def join_event(s: Session, event_id: int, user_id: int) -> None:
    event = _get_event(s, event_id)
    _get_user(s, user_id)
    _add_participant(event, user_id)
    s.flush()


# --- расходы ---


def _get_expense(s: Session, expense_id: int) -> Expense:
    expense = s.get(Expense, expense_id)
    if expense is None:
        raise ServiceError(NOT_FOUND)
    return expense


def get_editable_expense(s: Session, expense_id: int, user_id: int) -> Expense:
    """Расход, который может менять user_id (автор, пока расход открыт); иначе ServiceError."""
    expense = _get_expense(s, expense_id)
    if expense.created_by != user_id:
        raise ServiceError("Изменять расход может только его автор")
    if expense.status != "open":
        raise ServiceError("Расход уже закрыт")
    return expense


def _check_draft(s: Session, event: Event, user_id: int, draft: ExpenseDraft) -> None:
    if not draft.title.strip():
        raise ServiceError("Введи название расхода")
    if len(draft.title.strip()) > MAX_NAME_LENGTH:
        raise ServiceError(f"Название расхода — не длиннее {MAX_NAME_LENGTH} символов")
    if not draft.items:
        raise ServiceError("Добавь хотя бы одну позицию")
    for item in draft.items:
        if not item.name.strip():
            raise ServiceError("Введи название позиции")
        if len(item.name.strip()) > MAX_NAME_LENGTH:
            raise ServiceError(f"Название позиции — не длиннее {MAX_NAME_LENGTH} символов")
        if not 1 <= item.unit_price <= MAX_UNIT_PRICE:
            raise ServiceError("Цена — от 0,01 до 10 000 000 ₽")
        if not 1 <= item.qty <= MAX_QTY:
            raise ServiceError(f"Количество — от 1 до {MAX_QTY}")
    if not 0 <= draft.tip_percent <= 100:
        raise ServiceError("Чаевые — от 0 до 100 %")
    is_participant = any(p.user_id == draft.payer_id for p in event.participants)
    if not (is_participant or draft.payer_id == user_id):
        raise ServiceError("Платить может только участник события")
    _get_user(s, draft.payer_id)


def create_expense(s: Session, event_id: int, user_id: int, draft: ExpenseDraft) -> Expense:
    event = _get_event(s, event_id)
    _get_user(s, user_id)
    _check_draft(s, event, user_id, draft)
    expense = Expense(
        event_id=event.id,
        title=draft.title.strip(),
        payer_id=draft.payer_id,
        created_by=user_id,
        tip_percent=draft.tip_percent,
        status="open",
        items=[
            Item(name=i.name.strip(), unit_price=i.unit_price, qty=i.qty, for_all=i.for_all)
            for i in draft.items
        ],
    )
    s.add(expense)
    _add_participant(event, user_id)
    _add_participant(event, draft.payer_id)
    s.flush()
    return expense


def update_expense(s: Session, expense_id: int, user_id: int, draft: ExpenseDraft) -> Expense:
    expense = get_editable_expense(s, expense_id, user_id)
    event = _get_event(s, expense.event_id)
    _check_draft(s, event, user_id, draft)

    # Сначала только проверяем id позиций, ничего не меняя: ошибка не должна оставить расход наполовину изменённым
    old_items = {item.id: item for item in expense.items}
    matched: list[Item | None] = []
    for item_draft in draft.items:
        if item_draft.id is None:
            matched.append(None)  # новая позиция
        elif item_draft.id in old_items:
            matched.append(old_items.pop(item_draft.id))
        else:
            raise ServiceError(NOT_FOUND)  # чужая позиция или повтор одной и той же

    new_items: list[Item] = []
    for item_draft, item in zip(draft.items, matched):
        if item is None:
            item = Item()
        item.name = item_draft.name.strip()
        item.unit_price = item_draft.unit_price
        item.qty = item_draft.qty
        item.for_all = item_draft.for_all
        if item.for_all:
            item.claims.clear()  # у позиции «на всех» отметок не бывает
        new_items.append(item)

    expense.title = draft.title.strip()
    expense.payer_id = draft.payer_id
    expense.tip_percent = draft.tip_percent
    expense.items = new_items  # позиции, которых нет в draft, удалятся вместе с отметками
    _add_participant(event, draft.payer_id)
    s.flush()
    return expense


def delete_expense(s: Session, expense_id: int, user_id: int) -> None:
    expense = get_editable_expense(s, expense_id, user_id)
    s.delete(expense)
    s.flush()
    s.expire_all()  # списки event.expenses и т.п. перечитаются из базы


# --- отметки ---


def set_claim(s: Session, item_id: int, user_id: int, units: int) -> None:
    """Ставит отметку «беру units штук»; 0 — снимает отметку."""
    item = s.get(Item, item_id)
    if item is None:
        raise ServiceError(NOT_FOUND)
    expense = _get_expense(s, item.expense_id)
    if expense.status != "open":
        raise ServiceError("Расход уже закрыт")
    if item.for_all:
        raise ServiceError("Эта позиция делится на всех")
    if not 0 <= units <= item.qty:
        raise ServiceError(f"Можно отметить от 0 до {item.qty} шт.")
    _get_user(s, user_id)

    claim = next((c for c in item.claims if c.user_id == user_id), None)
    if units == 0:
        if claim is not None:
            item.claims.remove(claim)  # delete-orphan удалит строку
    else:
        if claim is None:
            item.claims.append(Claim(user_id=user_id, units=units))
        else:
            claim.units = units
        _add_participant(_get_event(s, expense.event_id), user_id)
    s.flush()


# --- расчёт ---


def _item_input(item: Item) -> ItemInput:
    claims = {c.user_id: c.units for c in item.claims}
    return ItemInput(item.unit_price, item.qty, item.for_all, claims)


def _event_of(expense: Expense) -> Event:
    # у Expense нет связи с событием, поэтому берём его через сессию
    event = object_session(expense).get(Event, expense.event_id)
    if event is None:
        raise ServiceError(NOT_FOUND)
    return event


def expense_input(expense: Expense) -> ExpenseInput:
    event = _event_of(expense)
    return ExpenseInput(
        payer_id=expense.payer_id,
        tip_percent=expense.tip_percent,
        items=[_item_input(item) for item in expense.items],
        participants=sorted(p.user_id for p in event.participants),
        leftover_mode=expense.leftover_mode if expense.status == "closed" else None,
    )


def expense_view(s: Session, expense_id: int) -> ExpenseView:
    expense = _get_expense(s, expense_id)
    event = _event_of(expense)
    data = expense_input(expense)
    item_unclaimed = {
        item.id: item_shares(item_input, data.participants)[1]
        for item, item_input in zip(expense.items, data.items)
    }
    leftover = 0
    if expense.status == "closed":  # у закрытого result.unclaimed уже 0 — считаем, как было до закрытия
        leftover = expense_result(replace(data, leftover_mode=None)).unclaimed
    return ExpenseView(expense, event, expense_result(data), item_unclaimed, leftover)


def user_share(expense: Expense, user_id: int) -> UserShare:
    """Из чего складывается доля пользователя: позиции + чаевые + часть остатка."""
    data = expense_input(expense)
    items_part = sum(item_shares(item, data.participants)[0].get(user_id, 0) for item in data.items)
    # доля до распределения остатка: именно с ней сравниваем, чтобы отделить чаевые от остатка
    marked = expense_result(replace(data, leftover_mode=None)).shares.get(user_id, 0)
    total = expense_result(data).shares.get(user_id, 0)
    return UserShare(total=total, tips=marked - items_part, leftover=total - marked)


# --- закрытие расхода ---


def close_expense(s: Session, expense_id: int, user_id: int, leftover_mode: str) -> None:
    """Закрывает расход; leftover_mode: «split» — поделить остаток, «payer» — оставить плательщику."""
    expense = _get_expense(s, expense_id)
    if expense.payer_id != user_id:
        raise ServiceError("Закрыть расход может только тот, кто платил")
    if expense.status != "open":
        raise ServiceError("Расход уже закрыт")
    if leftover_mode not in ("split", "payer"):
        raise ServiceError("Выбери, что делать с остатком")
    expense.status = "closed"
    expense.leftover_mode = leftover_mode
    expense.closed_at = utcnow()
    s.flush()


# --- сводки ---


def _event_expenses(s: Session, event_id: int) -> list[Expense]:
    # новые сверху; запрос, а не event.expenses — тот список может быть устаревшим
    query = select(Expense).where(Expense.event_id == event_id).order_by(Expense.created_at.desc(), Expense.id.desc())
    return list(s.scalars(query))


def _event_settlements(s: Session, event_id: int) -> list[Settlement]:
    query = select(Settlement).where(Settlement.event_id == event_id).order_by(Settlement.created_at.desc(), Settlement.id.desc())
    return list(s.scalars(query))


def _event_balances(
    expenses: list[tuple[Expense, ExpenseResult]], settlements: list[Settlement]
) -> dict[int, int]:
    return balances(
        [(expense.payer_id, result) for expense, result in expenses],
        [SettlementInput(x.from_user, x.to_user, x.amount, x.status == "confirmed") for x in settlements],
    )


def event_summary(s: Session, event_id: int) -> EventSummary:
    event = _get_event(s, event_id)
    expenses = [(e, expense_result(expense_input(e))) for e in _event_expenses(s, event_id)]
    settlements = _event_settlements(s, event_id)
    event_balances = _event_balances(expenses, settlements)
    return EventSummary(
        event=event,
        expenses=expenses,
        total=sum(result.total for _, result in expenses),
        unclaimed=sum(result.unclaimed for _, result in expenses),
        balances=event_balances,
        transfers=transfers(event_balances),
        pending=[x for x in settlements if x.status == "pending"],
    )


def chat_home(s: Session, chat_id: int, user_id: int) -> list[EventCard]:
    query = select(Event).where(Event.chat_id == chat_id).order_by(Event.created_at.desc(), Event.id.desc())
    cards = []
    for event in s.scalars(query):
        summary = event_summary(s, event.id)
        cards.append(EventCard(event, summary.total, summary.unclaimed, summary.balances.get(user_id, 0)))
    return cards


# --- переводы ---


def create_settlement(s: Session, event_id: int, from_user: int, to_user: int, amount: int) -> Settlement:
    """«Я перевёл»: from_user сообщает, что отправил to_user деньги; на баланс влияет после подтверждения."""
    _get_event(s, event_id)
    if amount <= 0:
        raise ServiceError("Сумма должна быть больше нуля")
    summary = event_summary(s, event_id)  # рекомендованные переводы считаются без учёта ожидающих
    recommended = next((t for t in summary.transfers if (t.from_user, t.to_user) == (from_user, to_user)), None)
    if recommended is None:
        raise ServiceError("Этот перевод не нужен")
    if amount > recommended.amount:
        raise ServiceError("Сумма больше долга")
    if any((x.from_user, x.to_user) == (from_user, to_user) for x in summary.pending):
        raise ServiceError("Перевод уже ждёт подтверждения")
    settlement = Settlement(event_id=event_id, from_user=from_user, to_user=to_user, amount=amount, status="pending")
    s.add(settlement)
    s.flush()
    return settlement


def confirm_settlement(s: Session, settlement_id: int, user_id: int) -> None:
    settlement = s.get(Settlement, settlement_id)
    if settlement is None:
        raise ServiceError(NOT_FOUND)
    if settlement.to_user != user_id:
        raise ServiceError("Подтвердить перевод может только получатель")
    if settlement.status != "pending":
        raise ServiceError("Перевод уже подтверждён")
    settlement.status = "confirmed"
    settlement.confirmed_at = utcnow()
    s.flush()
