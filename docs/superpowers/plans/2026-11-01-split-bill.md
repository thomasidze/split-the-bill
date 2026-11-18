# Сплит счёта в Telegram — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Telegram Mini App для группового чата: события, расходы с позициями, отметка своих позиций, чаевые, «кто кому» с подтверждением переводов, уведомления бота.

**Architecture:** Один Python-процесс: FastAPI отдаёт HTML-фрагменты (Jinja2 + htmx) и принимает вебхук бота (aiogram 3). Вся математика — чистые функции в `app/calc.py`; `app/services.py` грузит данные из SQLite и вызывает `calc.py`; всё общение с Telegram API спрятано за `TelegramGateway`, чтобы в тестах его подменять.

**Tech Stack:** Python 3.12, uv, FastAPI, Uvicorn, aiogram 3, SQLAlchemy 2, SQLite, Jinja2, htmx 2.0.4, pydantic-settings, pytest, pytest-asyncio, httpx.

**Spec:** `docs/superpowers/specs/2026-11-01-split-bill-design.md`

## Global Constraints

- Python ≥ 3.12 (системный — 3.9, поэтому всё запускается через `uv`: `uv run ...`).
- Все деньги — `int` в копейках. Только рубли.
- Весь пользовательский текст — на русском.
- `initData` старше 86400 секунд — отклоняется.
- Кеш проверки членства в чате — 300 секунд. Члены чата: статусы `member`, `administrator`, `creator`, а `restricted` — только если `is_member == True`.
- `invite_code = secrets.token_hex(8)`; `start_param` = `<invite_code>` или `<invite_code>_e<expense_id>`.
- Ссылка на приложение: `https://t.me/<BOT_USERNAME>/<APP_SHORT_NAME>?startapp=<start_param>`.
- Чаевые: варианты 0 / 5 / 10 / 15 % + своё целое значение 0–100.
- Открытый экран расхода обновляется раз в 5 секунд.
- Деление сумм — только через `calc.allocate` (наибольший остаток, при равенстве остатков — меньший ключ первым).
- Сообщения бота — `parse_mode=HTML`, все пользовательские строки экранируются `html.escape`.
- Сообщения коммитов заканчиваются строкой `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. Ввод цены в разных форматах (`650`, `650,50`, `650.5`, ` 1 200 `) → парсится в копейки; `0`, `-5`, `abc`, `1,234` → понятная ошибка, а не 500. *(Task 1, `test_parse_rub_*`)*
2. Название расхода/события с `<b>` или `&` → в сообщении бота показывается как текст, не ломает HTML. *(Task 8, `test_notification_text_escapes_html`)*
3. Автор уменьшил количество позиции ниже уже отмеченных штук или удалил позицию с отметками → суммы по-прежнему сходятся. *(Task 6, `test_update_expense_qty_below_claims_keeps_invariant`)*
4. Пользователь чата A открывает URL расхода/события/фото чата B → 404, данных не видно. *(Task 11, `test_expense_from_other_chat_is_404`)*
5. Двойное нажатие «Я перевёл» → второй перевод не создаётся, пока первый ожидает подтверждения. *(Task 7, `test_duplicate_pending_settlement_rejected`)*

---

### Task 1: Каркас проекта, деньги и ссылки

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `.env.example`, `app/__init__.py`, `app/config.py`, `app/money.py`, `app/links.py`, `tests/__init__.py`, `tests/test_money.py`, `tests/test_links.py`

**Interfaces:**
- Produces:
  - `app.config.Settings` (pydantic-settings, читает `.env`): `bot_token: str`, `bot_username: str`, `app_short_name: str`, `app_url: str` (публичный HTTPS-адрес без `/` на конце), `webhook_secret: str`, `db_path: str = "data/app.db"`.
  - `app.money.format_rub(kopecks: int) -> str` — `320000 → "3 200 ₽"`, `135050 → "1 350,50 ₽"`, `0 → "0 ₽"`, `-45000 → "−450 ₽"` (разделитель тысяч — неразрывный пробел ` `).
  - `app.money.parse_rub(text: str) -> int` — копейки; `ValueError` с русским сообщением на мусор/≤0.
  - `app.links.build_app_link(bot_username: str, app_short_name: str, invite_code: str, expense_id: int | None = None) -> str`
  - `app.links.parse_start_param(value: str | None) -> tuple[str | None, int | None]` — `(invite_code, expense_id)`.

- [ ] **Step 1: Установить uv и инициализировать репозиторий**

uv ставится официальным установщиком (`curl -LsSf https://astral.sh/uv/install.sh | sh`) — **спросить пользователя перед запуском**. Затем `git init`. `pyproject.toml`: `requires-python = ">=3.12"`, зависимости `fastapi`, `uvicorn[standard]`, `aiogram>=3.13`, `sqlalchemy>=2.0`, `jinja2`, `python-multipart`, `pydantic-settings`; dev-группа `pytest`, `pytest-asyncio`, `httpx`; `[tool.pytest.ini_options] asyncio_mode = "auto"`. `.gitignore`: `.venv/`, `.env`, `data/`, `__pycache__/`. `.env.example` — все поля `Settings` с пустыми значениями и комментариями.

Run: `uv sync` → создаётся `.venv` с Python 3.12.

- [ ] **Step 2: Написать падающие тесты**

```python
# tests/test_money.py
NBSP = " "
def test_format_rub_whole(): assert format_rub(320000) == f"3{NBSP}200 ₽"
def test_format_rub_kopecks(): assert format_rub(135050) == f"1{NBSP}350,50 ₽"
def test_format_rub_zero(): assert format_rub(0) == "0 ₽"
def test_format_rub_negative(): assert format_rub(-45000) == "−450 ₽"
@pytest.mark.parametrize("text,expected", [("650", 65000), ("650,50", 65050), ("650.5", 65050), (" 1 200 ", 120000), ("0,01", 1)])
def test_parse_rub_ok(text, expected): assert parse_rub(text) == expected
@pytest.mark.parametrize("text", ["", "0", "-5", "abc", "1,234", "1.2.3"])
def test_parse_rub_rejects(text):
    with pytest.raises(ValueError): parse_rub(text)

# tests/test_links.py
def test_build_link_chat():
    assert build_app_link("splitbot", "app", "ab12") == "https://t.me/splitbot/app?startapp=ab12"
def test_build_link_expense():
    assert build_app_link("splitbot", "app", "ab12", 7) == "https://t.me/splitbot/app?startapp=ab12_e7"
def test_parse_start_param():
    assert parse_start_param("ab12_e7") == ("ab12", 7)
    assert parse_start_param("ab12") == ("ab12", None)
    assert parse_start_param(None) == (None, None)
    assert parse_start_param("ab12_eX") == ("ab12", None)
```

- [ ] **Step 3: Запустить — падают**

Run: `uv run pytest tests/test_money.py tests/test_links.py -v` → FAIL (ImportError).

- [ ] **Step 4: Реализовать `config.py`, `money.py`, `links.py`**

`parse_rub`: убрать все пробельные символы, `,` → `.`, регэксп `^\d+(\.\d{1,2})?$`, иначе `ValueError("Не похоже на сумму")`; 0 → `ValueError("Сумма должна быть больше нуля")`.

- [ ] **Step 5: Запустить — проходят**

Run: `uv run pytest -v` → все PASS.

- [ ] **Step 6: Commit** — `git add -A && git commit -m "chore: scaffold project, money and link helpers"`

---

### Task 2: Расчёт доли по расходу (`calc.py`, часть 1)

**Files:**
- Create: `app/calc.py`, `tests/test_calc_expense.py`

**Interfaces:**
- Produces (все dataclass — `frozen=True`):
  ```python
  @dataclass(frozen=True)
  class ItemInput:
      unit_price: int            # копейки
      qty: int
      for_all: bool
      claims: dict[int, int]     # user_id -> units

  @dataclass(frozen=True)
  class ExpenseInput:
      payer_id: int
      tip_percent: int
      items: list[ItemInput]
      participants: list[int]    # участники события
      leftover_mode: str | None  # None (открыт), "split", "payer"

  @dataclass(frozen=True)
  class ExpenseResult:
      total: int                 # позиции + чаевые
      shares: dict[int, int]     # user_id -> доля с чаевыми (включая долю плательщика)
      unclaimed: int             # с чаевыми; 0 у закрытого

  def allocate(total: int, weights: dict[int, int]) -> dict[int, int]
  def item_shares(item: ItemInput, participants: list[int]) -> tuple[dict[int, int], int]  # (доли без чаевых, не разобрано)
  def expense_result(expense: ExpenseInput) -> ExpenseResult
  ```

- [ ] **Step 1: Написать падающие тесты**

```python
def test_allocate_remainder_goes_to_lowest_key():
    assert allocate(1000, {1: 1, 2: 1, 3: 1}) == {1: 334, 2: 333, 3: 333}
    assert allocate(100, {5: 1, 2: 1, 9: 1}) == {2: 34, 5: 33, 9: 33}
def test_allocate_largest_remainder():
    assert allocate(10, {1: 1, 2: 2}) == {1: 3, 2: 7}   # 3.33→3, 6.67→7
def test_allocate_empty(): assert allocate(500, {}) == {}

def test_item_partial_claims():   # пиво ×6, взяли 2 и 2
    assert item_shares(ItemInput(15000, 6, False, {1: 2, 2: 2}), []) == ({1: 30000, 2: 30000}, 30000)
def test_item_overclaimed_shared():   # пицца ×1 на троих
    assert item_shares(ItemInput(90000, 1, False, {1: 1, 2: 1, 3: 1}), []) == ({1: 30000, 2: 30000, 3: 30000}, 0)
def test_item_overclaimed_proportional():
    assert item_shares(ItemInput(100, 6, False, {1: 4, 2: 4}), []) == ({1: 300, 2: 300}, 0)
def test_item_for_all():
    assert item_shares(ItemInput(500000, 1, True, {}), [1, 2, 3, 4, 5]) == ({u: 100000 for u in range(1, 6)}, 0)
def test_item_for_all_without_participants():
    assert item_shares(ItemInput(500000, 1, True, {}), []) == ({}, 500000)

ITEMS = [ItemInput(65000, 1, False, {1: 1}), ItemInput(90000, 1, False, {1: 1, 2: 1}), ItemInput(20000, 2, False, {})]
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
```

- [ ] **Step 2: Запустить — падают.** `uv run pytest tests/test_calc_expense.py -v` → FAIL.

- [ ] **Step 3: Реализовать `calc.py`**

Алгоритм `expense_result` (именно в этом порядке, иначе не сойдутся числа из тестов):
```
items_total = Σ unit_price*qty
tip_total   = (items_total * tip_percent + 50) // 100
s, unclaimed_items = сумма item_shares по всем позициям
tips = allocate(tip_total, {**s, 0: unclaimed_items})   # ключ 0 = «не разобрано», нулевые веса выкинуть
shares[u] = s[u] + tips[u];  unclaimed = unclaimed_items + tips.get(0, 0)
leftover_mode == "split":  allocate(unclaimed, {p: 1 for p in participants}) добавить к shares (нет участников → плательщику)
leftover_mode == "payer":  unclaimed добавить к shares[payer_id]
закрытый → unclaimed = 0;  ключи с нулевой долей не включать в shares
```
`allocate`: `floor(total*w/W)` каждому, остаток по одной копейке по убыванию дробного остатка `(total*w) % W`, при равенстве — по возрастанию ключа.

- [ ] **Step 4: Запустить — проходят.**

- [ ] **Step 5: Commit** — `feat: expense share calculation`

---

### Task 3: Балансы и «кто кому» (`calc.py`, часть 2)

**Files:**
- Modify: `app/calc.py`
- Create: `tests/test_calc_balances.py`

**Interfaces:**
- Consumes: `ExpenseResult` (Task 2).
- Produces:
  ```python
  @dataclass(frozen=True)
  class SettlementInput:
      from_user: int; to_user: int; amount: int; confirmed: bool

  @dataclass(frozen=True)
  class Transfer:
      from_user: int; to_user: int; amount: int

  def balances(expenses: list[tuple[int, ExpenseResult]], settlements: list[SettlementInput]) -> dict[int, int]
      # (payer_id, result); >0 — должны ему; нулевые балансы не возвращать
  def transfers(balances: dict[int, int]) -> list[Transfer]
  ```

- [ ] **Step 1: Написать падающие тесты**

```python
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
def test_transfers_empty(): assert transfers({}) == []
```

- [ ] **Step 2: Запустить — падают.**
- [ ] **Step 3: Реализовать.** `transfers`: на каждом шаге наибольший по модулю должник и наибольший кредитор (при равенстве — меньший `user_id`), сумма `min`, повторять до нуля.
- [ ] **Step 4: Запустить — проходят.**
- [ ] **Step 5: Commit** — `feat: balances and minimal transfers`

---

### Task 4: База данных

**Files:**
- Create: `app/db.py`, `app/models.py`, `tests/conftest.py`, `tests/test_models.py`

**Interfaces:**
- Produces:
  - `app.db.make_engine(db_path: str) -> Engine` (создаёт папку; `":memory:"` → `StaticPool`), `app.db.make_session_factory(engine) -> sessionmaker[Session]`, `app.db.init_db(engine) -> None` (`create_all`, миграций нет).
  - `app.models`: `Base`, `Chat`, `User`, `Event`, `EventParticipant`, `Expense`, `Item`, `Claim`, `Settlement` — поля и типы ровно как в §3.1 спецификации. Связи: `Event.participants`, `Event.expenses`, `Expense.items` (cascade delete-orphan), `Item.claims` (cascade delete-orphan), `Event.settlements`. `Chat.invite_code` — уникальный, по умолчанию `secrets.token_hex(8)`. `Expense.status` по умолчанию `"open"`.
  - `tests/conftest.py`: фикстура `session` — in-memory SQLite с созданной схемой.

- [ ] **Step 1: Тест** `test_cascade_deletes_items_and_claims`: создать чат → событие → расход с позицией и отметкой; удалить расход; `session.query(Claim).count() == 0` и `Item` тоже 0. И `test_invite_code_generated`: `len(chat.invite_code) == 16`.
- [ ] **Step 2: Запустить — падает.**
- [ ] **Step 3: Реализовать** на SQLAlchemy 2 (`Mapped[...]`, `mapped_column`), `PRAGMA foreign_keys=ON` через event listener.
- [ ] **Step 4: Запустить — проходит.**
- [ ] **Step 5: Commit** — `feat: database models`

---

### Task 5: Аутентификация и членство

**Files:**
- Create: `app/auth.py`, `tests/helpers.py`, `tests/test_auth.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class TgUser: id: int; first_name: str; last_name: str | None; username: str | None; photo_url: str | None
  @dataclass(frozen=True)
  class InitData: user: TgUser; start_param: str | None; auth_date: int
  class AuthError(Exception): ...
  def validate_init_data(raw: str, bot_token: str, now: int, max_age: int = 86400) -> InitData
  class MembershipChecker:
      def __init__(self, fetch: Callable[[int, int], Awaitable[bool]], ttl: float = 300, clock: Callable[[], float] = time.monotonic)
      async def is_member(self, chat_id: int, user_id: int) -> bool
  ```
  - `tests/helpers.py`: `make_init_data(bot_token: str, user_id: int, first_name: str = "Тест", start_param: str | None = None, auth_date: int | None = None) -> str` — собирает строку и подписывает по алгоритму Telegram (secret = HMAC_SHA256(key=b"WebAppData", msg=bot_token); hash = HMAC_SHA256(secret, data_check_string).hexdigest()).

- [ ] **Step 1: Тесты**
  - `test_valid_init_data` → `user.id`, `start_param` разобраны.
  - `test_tampered_hash_rejected` (поменять user в строке) → `AuthError`.
  - `test_expired_rejected` (`auth_date = now - 86401`) → `AuthError`.
  - `test_missing_user_rejected` → `AuthError`.
  - `test_membership_cached`: fake fetch считает вызовы; два `is_member` подряд → 1 вызов; сдвинуть часы на 301 → 2 вызова.
- [ ] **Step 2: Запустить — падают.**
- [ ] **Step 3: Реализовать.** Подпись проверять через `aiogram.utils.web_app.safe_parse_webapp_init_data` (ловит `ValueError` → `AuthError("Открой приложение заново из чата")`), затем проверка возраста. Фото и имя брать из `user`.
- [ ] **Step 4: Запустить — проходят.**
- [ ] **Step 5: Commit** — `feat: initData validation and membership cache`

---

### Task 6: Сервисы — пользователи, события, расходы, отметки

**Files:**
- Create: `app/services.py`, `tests/test_services_expenses.py`

**Interfaces:**
- Consumes: модели (Task 4), `calc.ItemInput/ExpenseInput/ExpenseResult/expense_result` (Task 2), `auth.TgUser` (Task 5).
- Produces:
  ```python
  class ServiceError(Exception): ...          # message — русский текст для пользователя
  @dataclass
  class ItemDraft: name: str; unit_price: int; qty: int; for_all: bool; id: int | None = None
  @dataclass
  class ExpenseDraft: title: str; payer_id: int; tip_percent: int; items: list[ItemDraft]
  @dataclass
  class ExpenseView: expense: Expense; event: Event; result: ExpenseResult; item_unclaimed: dict[int, int]  # item_id -> копейки без чаевых

  def upsert_user(s: Session, tg: TgUser) -> User
  def register_chat(s: Session, chat_id: int, title: str) -> Chat      # создать или bot_active=True, invite_code не менять
  def deactivate_chat(s: Session, chat_id: int) -> None
  def get_chat_by_code(s: Session, code: str) -> Chat | None
  def create_event(s: Session, chat_id: int, title: str, user_id: int) -> Event
  def join_event(s: Session, event_id: int, user_id: int) -> None         # идемпотентно
  def create_expense(s: Session, event_id: int, user_id: int, draft: ExpenseDraft) -> Expense
  def update_expense(s: Session, expense_id: int, user_id: int, draft: ExpenseDraft) -> Expense
  def delete_expense(s: Session, expense_id: int, user_id: int) -> None
  def set_claim(s: Session, item_id: int, user_id: int, units: int) -> None  # 0 — снять отметку
  def expense_input(expense: Expense) -> ExpenseInput
  def expense_view(s: Session, expense_id: int) -> ExpenseView
  ```
  Сервисы делают `s.flush()`, коммит — на стороне вызывающего (роута).

- [ ] **Step 1: Тесты**
  - `test_create_expense_joins_author_and_payer`.
  - `test_create_expense_validation`: пустое название / нет позиций / `qty < 1` / `tip_percent` вне 0–100 / payer не участник и не автор → `ServiceError`.
  - `test_set_claim_upsert_and_remove`: `set_claim(…, 2)` дважды → одна строка `units=2`; `set_claim(…, 0)` → строки нет; отметка добавляет пользователя в участники.
  - `test_set_claim_rejects`: `units > item.qty`, позиция `for_all`, закрытый расход → `ServiceError`.
  - `test_only_author_can_edit_or_delete` и `test_cannot_edit_closed`.
  - `test_update_expense_qty_below_claims_keeps_invariant`: позиция qty=6, отметки 3+3, обновить qty до 4 → `result.unclaimed == 0`, `sum(shares) == total`; затем удалить позицию из draft → её отметок нет в базе.
  - `test_expense_view_numbers`: расход из Task 2 (`ITEMS`, tip 10) → `view.result.unclaimed == 44000`.
- [ ] **Step 2: Запустить — падают.**
- [ ] **Step 3: Реализовать.** `update_expense`: позиции из draft с `id` — обновить, без `id` — создать, отсутствующие в draft — удалить.
- [ ] **Step 4: Запустить — проходят.**
- [ ] **Step 5: Commit** — `feat: expense and claim services`

---

### Task 7: Сервисы — закрытие, сводки, переводы

**Files:**
- Modify: `app/services.py`
- Create: `tests/test_services_summary.py`

**Interfaces:**
- Consumes: Task 6, `calc.balances/transfers/SettlementInput/Transfer` (Task 3).
- Produces:
  ```python
  @dataclass
  class EventSummary:
      event: Event
      expenses: list[tuple[Expense, ExpenseResult]]   # новые сверху
      total: int; unclaimed: int
      balances: dict[int, int]
      transfers: list[Transfer]
      pending: list[Settlement]
  @dataclass
  class EventCard: event: Event; total: int; unclaimed: int; my_balance: int

  def close_expense(s: Session, expense_id: int, user_id: int, leftover_mode: str) -> None   # "split" | "payer"
  def event_summary(s: Session, event_id: int) -> EventSummary
  def chat_home(s: Session, chat_id: int, user_id: int) -> list[EventCard]               # новые сверху
  def create_settlement(s: Session, event_id: int, from_user: int, to_user: int, amount: int) -> Settlement
  def confirm_settlement(s: Session, settlement_id: int, user_id: int) -> None
  ```

- [ ] **Step 1: Тесты**
  - `test_close_only_payer` и `test_close_split_moves_leftover` (числа из `test_expense_closed_split`).
  - `test_event_summary_transfers`: событие с расходом из Task 2, закрыт `split` → `transfers == [Transfer(1,10,132000), Transfer(2,10,60500), Transfer(3,10,11000)]`.
  - `test_settlement_flow`: `create_settlement(1→10, 132000)` → в `pending`, баланс не изменился; `confirm_settlement` не получателем → `ServiceError`; получателем → `balances[1]` пропал.
  - `test_settlement_amount_bounds`: сумма ≤ 0 или больше рекомендованного перевода `from→to` → `ServiceError`.
  - `test_duplicate_pending_settlement_rejected`: второй `create_settlement` той же пары, пока первый `pending` → `ServiceError("Перевод уже ждёт подтверждения")`.
  - `test_chat_home_my_balance`: для пользователя 1 → `my_balance == -132000`.
- [ ] **Step 2: Запустить — падают.**
- [ ] **Step 3: Реализовать.** Рекомендованный перевод считать по балансам **без** учёта pending.
- [ ] **Step 4: Запустить — проходят.**
- [ ] **Step 5: Commit** — `feat: event summary and settlements`

---

### Task 8: Бот и шлюз Telegram

**Files:**
- Create: `app/bot.py`, `tests/test_bot.py`

**Interfaces:**
- Consumes: `services.register_chat/deactivate_chat` (Task 6), `links.build_app_link` (Task 1), `money.format_rub`.
- Produces:
  ```python
  class TelegramGateway:                           # настоящий; в тестах — FakeGateway с теми же методами
      def __init__(self, bot: Bot, settings: Settings)
      async def is_member(self, chat_id: int, user_id: int) -> bool
      async def send_welcome(self, chat_id: int, invite_code: str) -> None
      async def notify_expense(self, chat_id: int, text: str, link: str, photo: bytes | None) -> str | None   # file_id фото
      async def download_photo(self, file_id: str) -> bytes
  WELCOME_TEXT: str
  def expense_notification_text(author: str, expense_title: str, event_title: str, total: int) -> str
  def build_router(session_factory: sessionmaker, gateway: TelegramGateway) -> Router
  ```
  Тексты:
  - `WELCOME_TEXT = "Привет! Я помогу поделить расходы.\nСоздавайте события, добавляйте чеки и отмечайте, кто что взял.\nЗакрепите это сообщение, чтобы приложение было под рукой 👇"`, кнопка «Открыть».
  - уведомление: `"<b>{author}</b> добавил(а) расход «{expense_title}» в «{event_title}»: {format_rub(total)}\nОтметьте свои позиции 👇"`, кнопка «Отметиться».

- [ ] **Step 1: Тесты**
  - `test_notification_text_escapes_html`: `expense_title="<b>Пиво & чипсы</b>"` → в тексте `&lt;b&gt;Пиво &amp; чипсы&lt;/b&gt;`.
  - `test_notification_text_amount`: total `320000` → содержит `3 200 ₽`.
  - `test_bot_added_registers_chat_and_welcomes`: вызвать обработчик `my_chat_member` напрямую с собранным `ChatMemberUpdated` (group, new status `member`) и FakeGateway → чат в базе, `send_welcome` вызван с его `invite_code`.
  - `test_bot_removed_deactivates_chat` (new status `left`).
- [ ] **Step 2: Запустить — падают.**
- [ ] **Step 3: Реализовать.** `is_member` — `get_chat_member` + правила членства из Global Constraints; ошибки Telegram → `False`. `notify_expense` — `send_photo` (`BufferedInputFile`) или `send_message`, inline URL-кнопка; любые `TelegramAPIError` логировать и возвращать `None`. `/app` в группе — повторить приветствие.
- [ ] **Step 4: Запустить — проходят.**
- [ ] **Step 5: Commit** — `feat: telegram bot and gateway`

---

### Task 9: Веб-основа: оболочка, главная чата, события

**Files:**
- Create: `app/main.py`, `app/web.py`, `app/routes/__init__.py`, `app/routes/pages.py`, `templates/base.html`, `templates/home.html`, `templates/event_new.html`, `templates/event.html`, `static/app.css`, `static/app.js`, `tests/test_pages_events.py`
- Modify: `tests/conftest.py` (фикстуры `client`, `gateway`)

**Interfaces:**
- Consumes: всё выше.
- Produces:
  - `app.main.create_app(settings: Settings, gateway: TelegramGateway | None = None, *, setup_webhook: bool = True) -> FastAPI` — при `gateway=None` создаёт настоящий `Bot` и `TelegramGateway`; при `setup_webhook` в lifespan ставит вебхук `{app_url}/tg/webhook` с `secret_token=webhook_secret`. `app = create_app(Settings())` на уровне модуля **не** создавать — запуск через `uvicorn --factory app.main:build`, где `build() -> FastAPI` читает `Settings()`.
  - `POST /tg/webhook` — проверка заголовка `X-Telegram-Bot-Api-Secret-Token`, иначе 403; передать update в aiogram `Dispatcher`.
  - `app.web.Ctx` (dataclass: `session: Session`, `user: User`, `chat: Chat`, `init: InitData`) и зависимость `get_ctx(request) -> Ctx`: заголовок `X-Init-Data` → `validate_init_data` → `upsert_user` → `get_chat_by_code(parse_start_param(init.start_param)[0])` → `MembershipChecker(gateway.is_member)` (один экземпляр на приложение, в `app.state`). Ошибки — `PlainTextResponse` с текстом: нет/плохой initData → 401 «Открой приложение заново из чата»; чат не найден → 404 «Открой приложение по кнопке в чате»; `bot_active=False` → 403 «Бот удалён из чата — добавьте его обратно»; не член → 403 «Это доступно только участникам чата». `ServiceError` → 400 с её текстом. После обработчика — `commit`.
  - Роуты: `GET /` (оболочка), `GET /view/start`, `GET /view/home`, `GET /view/events/new`, `POST /events` (`title`), `GET /view/events/{id}?tab=expenses|transfers`, `POST /events/{id}/join`. Каждый объект проверяется на принадлежность `ctx.chat`, иначе 404.
  - Соглашение для фрагментов: корневой элемент фрагмента несёт `data-back="<url>"` (показать BackButton) и/или `data-main-text="..."` + `data-main-get="<url>"` либо `data-main-submit="<form id>"` (MainButton).
  - `tests/conftest.py`: `FakeGateway` (`members: set[tuple[int,int]]`, `notified: list`, `welcomed: list`, `photos: dict[str, bytes]`), фикстура `client` = `TestClient(create_app(test_settings, FakeGateway(), setup_webhook=False))` с базой `":memory:"`, хелпер `auth_headers(user_id, start_param)` через `tests.helpers.make_init_data`.

- [ ] **Step 1: Тесты**
  - `test_no_init_data_401`, `test_unknown_chat_404`, `test_not_member_403`, `test_inactive_bot_403` — проверить и код, и текст ответа.
  - `test_webhook_wrong_secret_403`.
  - `test_create_event_and_see_on_home`: `POST /events title=Баня 21.06` → `GET /view/home` содержит «Баня 21.06».
  - `test_start_routes_to_expense`: `start_param="<code>_e<id>"` → `/view/start` отдаёт экран расхода (после Task 11; до того — `pytest.mark.skip` с причиной, снять в Task 11).
  - `test_join_event`: второй пользователь `POST /events/{id}/join` → он в участниках, кнопки «Я участвую» больше нет.
  - `test_event_page_shows_unclaimed`: событие с открытым расходом → на странице «не разобрано» и сумма через `format_rub`.
- [ ] **Step 2: Запустить — падают.**
- [ ] **Step 3: Реализовать.**
  - `base.html`: `<script src="https://telegram.org/js/telegram-web-app.js">` (официальный скрипт, Telegram обновляет его по тому же адресу — без SRI), `<script src="https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js" integrity="sha384-…" crossorigin="anonymous">` (хеш посчитать при реализации: `curl -s <url> | openssl dgst -sha384 -binary | openssl base64 -A`), `#app` с `hx-get="/view/start" hx-trigger="load"`.
  - `app.js`: `Telegram.WebApp.ready(); expand()`; на `htmx:configRequest` добавить `X-Init-Data`; на `htmx:afterSwap` прочитать `data-back`/`data-main-*` и настроить `BackButton`/`MainButton` (снимать старые обработчики); на `htmx:responseError` — `Telegram.WebApp.showAlert(xhr.responseText)`; `HapticFeedback.selectionChanged()` на элементах с `data-haptic`.
  - `app.css`: только `var(--tg-theme-*)` цвета, мобильная вёрстка.
  - Вне Telegram (`initData` пустой) оболочка показывает «Открой приложение из Telegram».
- [ ] **Step 4: Запустить — проходят.** `uv run pytest -v`
- [ ] **Step 5: Commit** — `feat: web shell, chat home and events`

---

### Task 10: Добавление/редактирование/удаление расхода, фото, уведомление

**Files:**
- Create: `templates/expense_form.html`, `tests/test_pages_expense_form.py`
- Modify: `app/routes/pages.py`, `static/app.js` (добавление/удаление строк позиций)

**Interfaces:**
- Consumes: `create_expense/update_expense/delete_expense` (Task 6), `TelegramGateway.notify_expense/download_photo`, `expense_notification_text` (Task 8), `parse_rub`, `build_app_link`.
- Produces роуты:
  - `GET /view/events/{id}/expenses/new`, `GET /view/expenses/{id}/edit`
  - `POST /events/{id}/expenses`, `POST /expenses/{id}` — multipart: `title`, `payer_id`, `tip_percent` (0/5/10/15/`custom`) + `tip_custom`, параллельные списки `item_id[]` (пусто у новых), `item_name[]`, `item_price[]`, `item_qty[]`, `item_for_all[]` (значения — индексы строк с галочкой), `photo` (необязательный файл, ≤ 10 МБ, `image/*`).
  - `POST /expenses/{id}/delete` → экран события.
  - `GET /photos/{expense_id}` → байты фото через `download_photo` (кеш в памяти не нужен).

- [ ] **Step 1: Тесты**
  - `test_create_expense_notifies_chat`: после POST в `gateway.notified` одна запись, ссылка оканчивается `_e<id>`, ответ — экран расхода.
  - `test_create_expense_with_photo_saves_file_id`: FakeGateway возвращает `"F1"` → `expense.photo_file_id == "F1"`; `GET /photos/{id}` отдаёт байты.
  - `test_bad_price_shows_error`: `item_price[]="abc"` → 400 «Не похоже на сумму» (строка позиции в тексте ошибки: «Позиция 1: …»).
  - `test_notify_failure_does_not_break_save`: FakeGateway.notify_expense возвращает `None` → расход сохранён, 200.
  - `test_edit_and_delete_by_author_only`: чужой пользователь → 400 с текстом `ServiceError`.
  - `test_custom_tip`: `tip_percent=custom`, `tip_custom=12` → `expense.tip_percent == 12`.
- [ ] **Step 2: Запустить — падают.**
- [ ] **Step 3: Реализовать.** Плательщик — `<select>` из участников события + текущий пользователь, по умолчанию текущий. Уведомление — после `commit`, затем при наличии `file_id` — второй `commit`.
- [ ] **Step 4: Запустить — проходят.**
- [ ] **Step 5: Commit** — `feat: expense form, photo and chat notification`

---

### Task 11: Экран расхода: отметки, живое обновление, закрытие

**Files:**
- Create: `templates/expense.html`, `templates/expense_live.html`, `tests/test_pages_expense.py`
- Modify: `app/routes/pages.py`, `tests/test_pages_events.py` (снять skip с `test_start_routes_to_expense`)

**Interfaces:**
- Consumes: `expense_view`, `set_claim`, `close_expense`.
- Produces роуты:
  - `GET /view/expenses/{id}` — весь экран; внутри `#live` с `hx-get="/view/expenses/{id}/live" hx-trigger="every 5s"` (только пока открыт).
  - `GET /view/expenses/{id}/live` — «Не разобрано: N из M», позиции с отметками, «Твоя доля: X (включая чаевые Y)».
  - `POST /items/{id}/claim` (`units`) → фрагмент `live`.
  - `POST /expenses/{id}/close` (`leftover_mode`) → экран расхода (закрытый). Если не разобрано > 0, кнопка «Закрыть расход» открывает выбор «Поделить поровну между участниками» / «Оставить на мне» (`Telegram.WebApp.showPopup`); если 0 — закрывает с `split`.

- [ ] **Step 1: Тесты**
  - `test_claim_toggle_single`: позиция qty=1 → `units=1` → в live имя пользователя и «Твоя доля»; `units=0` → снято.
  - `test_claim_stepper_qty`: позиция «Пиво ×6» → `units=2` → «Не разобрано» уменьшилось на 2 × цену (+ чаевые).
  - `test_for_all_item_has_no_toggle` → в разметке «делится на всех (n чел.)», без кнопки.
  - `test_close_by_payer_split` → статус «Закрыт», `every 5s` в разметке отсутствует, отметка после закрытия → 400.
  - `test_close_by_non_payer_400`.
  - `test_expense_from_other_chat_is_404`: второй чат, его участник запрашивает `/view/expenses/{id}`, `/view/events/{event_id}`, `/photos/{id}` и `POST /items/{item_id}/claim` первого чата → везде 404.
- [ ] **Step 2: Запустить — падают.**
- [ ] **Step 3: Реализовать.** Элементы отметки — `data-haptic`. Шапка: название, плательщик, итог с чаевыми, миниатюра фото чека (`/photos/{id}`), по нажатию — на весь экран.
- [ ] **Step 4: Запустить — проходят.**
- [ ] **Step 5: Commit** — `feat: expense screen with claims and closing`

---

### Task 12: Вкладка «Кто кому»

**Files:**
- Create: `templates/event_transfers.html`, `tests/test_pages_transfers.py`
- Modify: `app/routes/pages.py`, `templates/event.html`

**Interfaces:**
- Consumes: `event_summary`, `create_settlement`, `confirm_settlement`.
- Produces роуты: `POST /events/{id}/settlements` (`to_user`, `amount`), `POST /settlements/{id}/confirm`; оба → фрагмент вкладки `transfers`.

- [ ] **Step 1: Тесты**
  - `test_transfers_tab_lists_transfers`: строка «Петя → Аня: 1 320 ₽»; у должника кнопка «Я перевёл», у остальных нет.
  - `test_settlement_pending_then_confirmed`: после «Я перевёл» — пометка «ожидает подтверждения» и у получателя кнопка «Подтвердить»; после подтверждения перевод исчезает из списка.
  - `test_everyone_settled_message`: нет переводов → «Все в расчёте 🎉».
  - Расходы ещё открыты → над списком подсказка «Пока не разобрано N — суммы могут измениться».
- [ ] **Step 2: Запустить — падают.**
- [ ] **Step 3: Реализовать.**
- [ ] **Step 4: Запустить — проходят.**
- [ ] **Step 5: Commit** — `feat: settle-up tab`

---

### Task 13: Запуск, README, проверка в настоящем Telegram

**Files:**
- Create: `README.md`, `Dockerfile`

- [ ] **Step 1: README (по-русски, для новичка)**
  1. Установка `uv`, `uv sync`.
  2. @BotFather: `/newbot` → токен; `/setjoingroups` → Enable; `/setprivacy` → Disable не нужен (бот слушает только команды и `my_chat_member`); `/newapp` → короткое имя приложения и URL = `APP_URL`.
  3. `.env` по `.env.example`.
  4. Локальный запуск: `uv run uvicorn --factory app.main:build --port 8000` + туннель `cloudflared tunnel --url http://localhost:8000` (cloudflared скачать с GitHub releases — спросить пользователя перед скачиванием); адрес туннеля → `APP_URL` и в BotFather (`/myapps` → Edit Web App URL); перезапуск.
  5. Деплой: `Dockerfile` (python:3.12-slim + uv, `CMD uvicorn --factory app.main:build --host 0.0.0.0 --port 8000`), `DB_PATH` — на смонтированный том; любой хостинг с HTTPS и постоянным диском.
  6. Как запускать тесты: `uv run pytest`.
- [ ] **Step 2: Полный прогон тестов** — `uv run pytest -v` → все PASS.
- [ ] **Step 3: Ручная проверка с пользователем** (нужны его бот и тестовый чат): добавить бота в группу → приветствие с кнопкой → создать событие → расход с 3 позициями, фото и чаевыми 10 % → уведомление в чате ведёт прямо на расход → со второго аккаунта отметить позицию, у первого через ≤5 с обновилось «не разобрано» → закрыть с «поделить поровну» → «Кто кому» → «Я перевёл» → «Подтвердить». Проверить светлую и тёмную тему.
- [ ] **Step 4: Commit** — `docs: setup guide and Dockerfile`
