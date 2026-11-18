"""HTML-страницы: оболочка и фрагменты для htmx.

Каждый экран — фрагмент, который заменяет содержимое <main id="app">.
Корневой элемент фрагмента настраивает кнопки Telegram (см. static/app.js):
  data-back="<url>"                         — кнопка «Назад» ведёт на этот экран;
  data-main-text="..." + data-main-get="<url>"   — главная кнопка открывает экран;
  data-main-text="..." + data-main-submit="<id>" — главная кнопка отправляет форму.
"""

import logging
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.exc import IntegrityError

from app import services
from app.bot import expense_notification_text
from app.links import build_app_link, parse_start_param
from app.models import Event, Expense, User
from app.money import format_rub_input, parse_rub
from app.services import ExpenseDraft, ItemDraft, ServiceError
from app.web import (
    BAD_INPUT_TEXT,
    Ctx,
    chat_event,
    chat_expense,
    chat_item,
    chat_settlement,
    display_name,
    get_ctx,
    render,
)

log = logging.getLogger(__name__)

router = APIRouter()

EventTab = Literal["expenses", "transfers"]

MAX_PHOTO_BYTES = 10 * 1024 * 1024
TIP_PRESETS = (0, 5, 10, 15)
MAX_DIGITS = 18  # длиннее число уже не влезет в базу — это явно не настоящий ввод
PHOTO_UNAVAILABLE_TEXT = "Фото недоступно"


@router.get("/")
async def shell(request: Request) -> HTMLResponse:
    """Пустая оболочка: данные подгружает htmx уже с заголовком X-Init-Data."""
    return render(request, "base.html")


@router.get("/view/start")
async def view_start(request: Request, ctx: Ctx = Depends(get_ctx)) -> HTMLResponse:
    """Первый экран после открытия приложения: главная или, по ссылке из чата, экран расхода."""
    _, expense_id = parse_start_param(ctx.init.start_param)
    if expense_id is not None:
        try:
            expense = chat_expense(ctx, expense_id)
        except HTTPException:
            # Расход удалили (или он из другого чата) — открываем главную, а не тупик с ошибкой
            return home_page(request, ctx)
        return expense_page(request, ctx, expense.id)
    return home_page(request, ctx)


@router.get("/view/home")
async def view_home(request: Request, ctx: Ctx = Depends(get_ctx)) -> HTMLResponse:
    return home_page(request, ctx)


@router.get("/view/events/new")
async def view_event_new(request: Request, ctx: Ctx = Depends(get_ctx)) -> HTMLResponse:
    return render(request, "event_new.html")


@router.post("/events")
async def post_event(
    request: Request, title: str = Form(""), ctx: Ctx = Depends(get_ctx)
) -> HTMLResponse:
    event = services.create_event(ctx.session, ctx.chat.id, title, ctx.user.id)
    return event_page(request, ctx, event)


@router.get("/view/events/{event_id}")
async def view_event(
    request: Request, event_id: int, tab: EventTab = "expenses", ctx: Ctx = Depends(get_ctx)
) -> HTMLResponse:
    return event_page(request, ctx, chat_event(ctx, event_id), tab)


@router.post("/events/{event_id}/join")
async def join_event(request: Request, event_id: int, ctx: Ctx = Depends(get_ctx)) -> HTMLResponse:
    event = chat_event(ctx, event_id)
    services.join_event(ctx.session, event.id, ctx.user.id)
    return event_page(request, ctx, event)


# --- расходы ---


@dataclass
class ExpenseForm:
    """Разобранная форма расхода."""

    draft: ExpenseDraft
    photo: bytes | None


def is_number(text: str) -> bool:
    """Целое число без знака, не слишком длинное."""
    text = text.strip()
    return text.isascii() and text.isdigit() and len(text) <= MAX_DIGITS


def read_tip(tip_percent: str, tip_custom: str) -> int:
    """Чаевые: пресет 0/5/10/15 или «своё» (custom + целое число в tip_custom)."""
    if tip_percent == "custom":
        if not is_number(tip_custom):
            raise ServiceError("Чаевые — целое число от 0 до 100")
        return int(tip_custom)  # диапазон 0–100 проверяет services
    if tip_percent not in [str(preset) for preset in TIP_PRESETS]:
        raise HTTPException(400, BAD_INPUT_TEXT)
    return int(tip_percent)


def read_item(number: int, item_id: str, name: str, price: str, qty: str, for_all: bool) -> ItemDraft:
    """Одна строка формы -> позиция; number — номер строки с единицы, для текста ошибки."""
    try:
        unit_price = parse_rub(price)
    except ValueError as error:
        raise ServiceError(f"Позиция {number}: {error}") from None
    if not is_number(qty):
        raise ServiceError(f"Позиция {number}: Количество — целое число")
    if item_id.strip() and not is_number(item_id):
        raise HTTPException(400, BAD_INPUT_TEXT)
    return ItemDraft(
        name=name,
        unit_price=unit_price,
        qty=int(qty),
        for_all=for_all,
        id=int(item_id) if item_id.strip() else None,
    )


async def read_photo(photo: UploadFile | None) -> bytes | None:
    """Фото из формы; пустое поле «файл не выбран» — это None."""
    if photo is None:
        return None
    data = await photo.read(MAX_PHOTO_BYTES + 1)  # лишний байт нужен, чтобы заметить превышение
    if not data:
        return None
    if len(data) > MAX_PHOTO_BYTES:
        raise ServiceError("Фото больше 10 МБ")
    if not (photo.content_type or "").startswith("image/"):
        raise ServiceError("Фото чека — это должна быть картинка")
    return data


async def read_expense_form(
    title: str = Form(""),
    payer_id: str = Form(...),
    tip_percent: str = Form("0"),
    tip_custom: str = Form(""),
    # Позиции приходят параллельными списками: n-я цена относится к n-му названию.
    item_id: list[str] = Form([], alias="item_id[]"),
    item_name: list[str] = Form([], alias="item_name[]"),
    item_price: list[str] = Form([], alias="item_price[]"),
    item_qty: list[str] = Form([], alias="item_qty[]"),
    item_for_all: list[int] = Form([], alias="item_for_all[]"),  # индексы строк с галочкой
    photo: UploadFile | None = File(None),
) -> ExpenseForm:
    if not len(item_id) == len(item_name) == len(item_price) == len(item_qty):
        raise HTTPException(400, BAD_INPUT_TEXT)
    items = [
        read_item(index + 1, *row, for_all=index in item_for_all)
        for index, row in enumerate(zip(item_id, item_name, item_price, item_qty))
    ]
    if not is_number(payer_id):
        raise HTTPException(400, BAD_INPUT_TEXT)
    draft = ExpenseDraft(title, int(payer_id), read_tip(tip_percent, tip_custom), items)
    return ExpenseForm(draft, await read_photo(photo))


@router.get("/view/events/{event_id}/expenses/new")
async def view_expense_new(
    request: Request, event_id: int, ctx: Ctx = Depends(get_ctx)
) -> HTMLResponse:
    return expense_form_page(request, ctx, chat_event(ctx, event_id), None)


@router.get("/view/expenses/{expense_id}/edit")
async def view_expense_edit(
    request: Request, expense_id: int, ctx: Ctx = Depends(get_ctx)
) -> HTMLResponse:
    expense = chat_expense(ctx, expense_id)
    services.get_editable_expense(ctx.session, expense.id, ctx.user.id)  # чужой или закрытый — 400
    return expense_form_page(request, ctx, chat_event(ctx, expense.event_id), expense)


@router.post("/events/{event_id}/expenses")
async def post_expense(
    request: Request,
    event_id: int,
    ctx: Ctx = Depends(get_ctx),
    form: ExpenseForm = Depends(read_expense_form),
) -> HTMLResponse:
    event = chat_event(ctx, event_id)
    expense = services.create_expense(ctx.session, event.id, ctx.user.id, form.draft)

    # Всё нужное для уведомления собираем заранее: после commit объекты придётся перечитывать.
    total = services.expense_view(ctx.session, expense.id).result.total
    text = expense_notification_text(display_name(ctx.user), expense.title, event.title, total)
    settings = request.app.state.settings
    link = build_app_link(
        settings.bot_username, settings.app_short_name, ctx.chat.invite_code, expense.id
    )
    chat_id = ctx.chat.id

    # Расход сохраняем до отправки: пока ждём Telegram, незавершённая запись держала бы базу.
    ctx.session.commit()
    file_id = await request.app.state.gateway.notify_expense(chat_id, text, link, form.photo)
    if file_id:
        expense.photo_file_id = file_id  # сохранится при завершении запроса
    # Ошибка отправки (file_id None) расход не отменяет — её уже записал в лог gateway.
    # Но если фото было, а file_id нет — фото потеряно, и автору нужно об этом сказать.
    photo_lost = form.photo is not None and not file_id
    return expense_page(request, ctx, expense.id, photo_lost=photo_lost)


@router.post("/expenses/{expense_id}")
async def post_expense_update(
    request: Request,
    expense_id: int,
    ctx: Ctx = Depends(get_ctx),
    form: ExpenseForm = Depends(read_expense_form),
) -> HTMLResponse:
    expense = chat_expense(ctx, expense_id)
    services.update_expense(ctx.session, expense.id, ctx.user.id, form.draft)
    return expense_page(request, ctx, expense.id)


@router.post("/expenses/{expense_id}/delete")
async def post_expense_delete(
    request: Request, expense_id: int, ctx: Ctx = Depends(get_ctx)
) -> HTMLResponse:
    expense = chat_expense(ctx, expense_id)
    event = chat_event(ctx, expense.event_id)
    services.delete_expense(ctx.session, expense.id, ctx.user.id)
    return event_page(request, ctx, event)


# --- экран расхода: отметки и закрытие ---


@router.get("/view/expenses/{expense_id}")
async def view_expense(request: Request, expense_id: int, ctx: Ctx = Depends(get_ctx)) -> HTMLResponse:
    return expense_page(request, ctx, chat_expense(ctx, expense_id).id)


@router.get("/view/expenses/{expense_id}/live")
async def view_expense_live(
    request: Request, expense_id: int, ctx: Ctx = Depends(get_ctx)
) -> HTMLResponse:
    """Часть экрана, которую htmx обновляет опросом раз в 5 секунд."""
    return live_page(request, ctx, chat_expense(ctx, expense_id).id)


@router.post("/items/{item_id}/claim")
async def post_claim(
    request: Request, item_id: int, units: int = Form(...), ctx: Ctx = Depends(get_ctx)
) -> HTMLResponse:
    """Отметка «беру units штук» (0 — снять); в ответе обновлённый live."""
    item = chat_item(ctx, item_id)
    expense_id = item.expense_id
    # Двойное нажатие: два запроса одновременно вставляют одну и ту же отметку, и второй
    # упирается в первичный ключ (item_id, user_id). Откатываем и пробуем ещё раз —
    # теперь отметка уже есть, и она просто обновится.
    for attempt in range(2):
        try:
            services.set_claim(ctx.session, item_id, ctx.user.id, units)
            break
        except IntegrityError:
            ctx.session.rollback()
            if attempt == 1:
                raise HTTPException(409, "Попробуй ещё раз") from None
    return live_page(request, ctx, expense_id)


@router.post("/expenses/{expense_id}/close")
async def post_expense_close(
    request: Request,
    expense_id: int,
    leftover_mode: str = Form(""),
    ctx: Ctx = Depends(get_ctx),
) -> HTMLResponse:
    expense = chat_expense(ctx, expense_id)
    services.close_expense(ctx.session, expense.id, ctx.user.id, leftover_mode)
    return expense_page(request, ctx, expense.id)


@router.get("/photos/{expense_id}")
async def get_photo(expense_id: int, request: Request, ctx: Ctx = Depends(get_ctx)) -> Response:
    """Фото чека: файл лежит в Telegram, здесь он только проксируется."""
    file_id = chat_expense(ctx, expense_id).photo_file_id
    if file_id is None:
        raise HTTPException(404, services.NOT_FOUND)
    try:
        data = await request.app.state.gateway.download_photo(file_id)
    except Exception:
        log.exception("download_photo failed: expense=%s", expense_id)
        raise HTTPException(404, PHOTO_UNAVAILABLE_TEXT) from None
    return Response(data, media_type="image/jpeg")


# --- переводы («Кто кому») ---


@router.post("/events/{event_id}/settlements")
async def post_settlement(
    request: Request,
    event_id: int,
    to_user: str = Form(...),
    amount: str = Form(...),
    ctx: Ctx = Depends(get_ctx),
) -> HTMLResponse:
    """«Я перевёл»: отправитель — всегда сам пользователь, а не значение из формы."""
    event = chat_event(ctx, event_id)
    if not (is_number(to_user) and is_number(amount)):
        raise HTTPException(400, BAD_INPUT_TEXT)
    services.create_settlement(ctx.session, event.id, ctx.user.id, int(to_user), int(amount))
    return event_page(request, ctx, event, "transfers")


@router.post("/settlements/{settlement_id}/confirm")
async def post_settlement_confirm(
    request: Request, settlement_id: int, ctx: Ctx = Depends(get_ctx)
) -> HTMLResponse:
    settlement = chat_settlement(ctx, settlement_id)
    event = chat_event(ctx, settlement.event_id)
    services.confirm_settlement(ctx.session, settlement.id, ctx.user.id)
    return event_page(request, ctx, event, "transfers")


# --- сборка экранов ---


def home_page(request: Request, ctx: Ctx) -> HTMLResponse:
    cards = services.chat_home(ctx.session, ctx.chat.id, ctx.user.id)
    return render(request, "home.html", chat=ctx.chat, cards=cards)


def event_page(request: Request, ctx: Ctx, event: Event, tab: EventTab = "expenses") -> HTMLResponse:
    summary = services.event_summary(ctx.session, event.id)
    is_participant = any(p.user_id == ctx.user.id for p in event.participants)
    # Имена для вкладки «Кто кому»: в переводах только id пользователей.
    user_ids = {t.from_user for t in summary.transfers} | {t.to_user for t in summary.transfers}
    user_ids |= {p.from_user for p in summary.pending} | {p.to_user for p in summary.pending}
    users = {user_id: ctx.session.get(User, user_id) for user_id in user_ids}
    pending_pairs = {(p.from_user, p.to_user) for p in summary.pending}
    return render(
        request,
        "event.html",
        summary=summary,
        tab=tab,
        is_participant=is_participant,
        users=users,
        pending_pairs=pending_pairs,
        me=ctx.user.id,
    )


def expense_context(ctx: Ctx, expense_id: int) -> dict:
    """Всё, что нужно шаблонам expense.html и expense_live.html."""
    view = services.expense_view(ctx.session, expense_id)
    expense = view.expense
    rows = []
    for item in expense.items:
        claims = sorted(item.claims, key=lambda claim: claim.user_id)
        claimed = sum(claim.units for claim in claims)
        rows.append(
            {
                "item": item,
                "claims": claims,
                "mine": next((claim.units for claim in claims if claim.user_id == ctx.user.id), 0),
                "left": max(item.qty - claimed, 0),  # сколько штук никто не взял
            }
        )
    is_open = expense.status == "open"
    return {
        "view": view,
        "expense": expense,
        "event": view.event,
        "rows": rows,
        "share": services.user_share(expense, ctx.user.id),
        "is_open": is_open,
        "is_payer": expense.payer_id == ctx.user.id,
        "can_edit": is_open and expense.created_by == ctx.user.id,
        "participant_count": len(view.event.participants),
    }


def expense_page(request: Request, ctx: Ctx, expense_id: int, photo_lost: bool = False) -> HTMLResponse:
    """photo_lost=True — показать, что фото чека не сохранилось (только сразу после создания)."""
    return render(request, "expense.html", **expense_context(ctx, expense_id), photo_lost=photo_lost)


def live_page(request: Request, ctx: Ctx, expense_id: int) -> HTMLResponse:
    return render(request, "expense_live.html", **expense_context(ctx, expense_id))


def expense_form_page(
    request: Request, ctx: Ctx, event: Event, expense: Expense | None
) -> HTMLResponse:
    """Форма расхода: пустая или с данными expense."""
    payers: list[User] = [p.user for p in event.participants]
    if ctx.user not in payers:
        payers.append(ctx.user)  # платить может и тот, кто ещё не участник события
    if expense is None:
        rows = [{"id": "", "name": "", "price": "", "qty": 1, "for_all": False}]
    else:
        rows = [
            {
                "id": item.id,
                "name": item.name,
                "price": format_rub_input(item.unit_price),
                "qty": item.qty,
                "for_all": item.for_all,
            }
            for item in expense.items
        ]
    tip = expense.tip_percent if expense else 0
    return render(
        request,
        "expense_form.html",
        event=event,
        expense=expense,
        payers=payers,
        payer_id=expense.payer_id if expense else ctx.user.id,
        rows=rows,
        tip=tip,
        tip_presets=TIP_PRESETS,
        tip_is_custom=tip not in TIP_PRESETS,
    )
