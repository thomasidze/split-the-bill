// Связка htmx и Telegram Mini App.
//
// Экраны приходят с сервера HTML-фрагментами и подставляются в <main id="app">.
// Корневой элемент экрана может настроить кнопки Telegram:
//   data-back="/view/home"            — показать «Назад», она откроет этот адрес;
//   data-main-text="Создать"          — показать главную кнопку с этим текстом;
//   data-main-get="/view/events/new"  — главная кнопка открывает этот адрес;
//   data-main-submit="event-form"     — или отправляет форму с таким id.
// Элементы с data-haptic дают лёгкую вибрацию при нажатии.
//   data-accent="#D85A30"             — цвет главной кнопки (цвет события).
// Экран расхода (фото чека, закрытие расхода) — в разделе «Экран расхода» ниже.

const tg = window.Telegram.WebApp;
const app = document.getElementById("app");

tg.ready();
tg.expand();
// Шапка и фон Telegram того же цвета, что и фон страницы (в app.css это --page).
if (tg.isVersionAtLeast && tg.isVersionAtLeast("6.1")) {
  tg.setHeaderColor("secondary_bg_color");
  tg.setBackgroundColor("secondary_bg_color");
}

const NO_CONNECTION_TEXT = "Нет связи, попробуй ещё раз";

// Сообщение пользователю. tg.showAlert падает, если уже открыто другое окно Telegram
// (или клиент Telegram слишком старый) — тогда показываем обычный alert.
function showAlert(text) {
  try {
    tg.showAlert(text);
  } catch (error) {
    window.alert(text);
  }
}

// Вопрос «да/нет»: onYes вызовется, только если ответили «да».
// showConfirm есть только с версии 6.2 (в старых он падает) — тогда спрашиваем обычным confirm.
function askConfirm(question, onYes) {
  if (tg.isVersionAtLeast && tg.isVersionAtLeast("6.2")) {
    tg.showConfirm(question, (ok) => {
      if (ok) onYes();
    });
  } else if (window.confirm(question)) {
    onYes();
  }
}

// Открыть экран: загрузить фрагмент и подставить его в #app.
function openScreen(url) {
  htmx.ajax("GET", url, { target: "#app" });
}

// Обработчики кнопок текущего экрана — их нужно снимать перед сменой экрана,
// иначе одно нажатие сработает несколько раз.
let onBack = null;
let onMain = null;

function setupButtons(screen) {
  if (onBack) tg.BackButton.offClick(onBack);
  if (onMain) tg.MainButton.offClick(onMain);
  onBack = null;
  onMain = null;

  const data = screen ? screen.dataset : {};

  if (data.back) {
    onBack = () => openScreen(data.back);
    tg.BackButton.onClick(onBack);
    tg.BackButton.show();
  } else {
    tg.BackButton.hide();
  }

  if (data.mainText && data.mainGet) {
    onMain = () => openScreen(data.mainGet);
  } else if (data.mainText && data.mainSubmit) {
    onMain = () => {
      const form = document.getElementById(data.mainSubmit);
      if (form.requestSubmit) {
        form.requestSubmit(); // проверяет поля формы и запускает её hx-post
      } else if (form.reportValidity()) {
        // В старых браузерах requestSubmit нет: сами проверяем поля и посылаем событие submit,
        // которое htmx перехватывает так же, как обычную отправку формы.
        htmx.trigger(form, "submit");
      }
    };
  }
  if (onMain) {
    // Главная кнопка в цвете события (data-accent), без события — в цветах темы Telegram.
    const colors = data.accent
      ? { color: data.accent, text_color: "#ffffff" }
      : { color: tg.themeParams.button_color, text_color: tg.themeParams.button_text_color };
    const params = { text: data.mainText };
    if (colors.color) params.color = colors.color; // у старых клиентов цветов темы может не быть
    if (colors.text_color) params.text_color = colors.text_color;
    tg.MainButton.setParams(params);
    tg.MainButton.onClick(onMain);
    tg.MainButton.show();
  } else {
    tg.MainButton.hide();
  }
}

// Каждый запрос htmx несёт подписанную initData — по ней сервер узнаёт пользователя и чат.
document.addEventListener("htmx:configRequest", (event) => {
  if (!tg.initData) {
    // Страницу открыли не из Telegram: на сервер не ходим
    event.preventDefault();
    app.textContent = "Открой приложение из Telegram";
    return;
  }
  event.detail.headers["X-Init-Data"] = tg.initData;
});

// Пришёл новый экран — настраиваем кнопки по его корневому элементу.
let screenShown = false;
document.addEventListener("htmx:afterSwap", (event) => {
  if (event.detail.target !== app) return; // обновился лишь кусок экрана
  screenShown = true;
  document.querySelectorAll(".photo-overlay").forEach((overlay) => overlay.remove()); // фото на весь экран не переживает смену экрана
  setupButtons(app.firstElementChild);
  loadPhotos();
});

// Сервер ответил ошибкой — в ответе простой текст для пользователя.
document.addEventListener("htmx:responseError", (event) => {
  const text = event.detail.xhr.responseText;
  const source = event.detail.elt;
  if (source && source.id === "live") {
    // Опрос экрана расхода (запрос от самого #live) не удался: расход удалили или доступ пропал.
    // Не показываем окно каждые 5 секунд, а заменяем #live сообщением без опроса —
    // убранный из страницы элемент htmx больше не опрашивает.
    const message = document.createElement("p");
    message.id = "live";
    message.className = "empty";
    message.textContent = text;
    source.replaceWith(message);
  } else if (screenShown) {
    showAlert(text);
  } else {
    app.textContent = text; // первый экран не открылся — показываем причину вместо него
  }
});

// Запрос вообще не дошёл до сервера (нет интернета).
document.addEventListener("htmx:sendError", (event) => {
  const source = event.detail.elt;
  if (source && source.id === "live") return; // опрос экрана расхода: через 5 секунд попробует сам
  if (screenShown) {
    showAlert(NO_CONNECTION_TEXT);
  } else {
    app.textContent = NO_CONNECTION_TEXT; // вместо вечного «Загрузка…»
  }
});

// Лёгкая вибрация при нажатии на элементы с data-haptic.
document.addEventListener("click", (event) => {
  if (event.target.closest("[data-haptic]")) {
    tg.HapticFeedback.selectionChanged();
  }
});

// --- Форма расхода: строки позиций ---

// Значение галочки «на всех» — номер строки с нуля. Пересчитываем после добавления и удаления,
// чтобы сервер понял, на каких строках галочка стоит.
function renumberItems() {
  document.querySelectorAll("#items .item-row").forEach((row, index) => {
    row.querySelector('[name="item_for_all[]"]').value = index;
  });
}

document.addEventListener("click", (event) => {
  if (event.target.closest("[data-add-item]")) {
    const template = document.getElementById("item-row-template");
    document.getElementById("items").append(template.content.cloneNode(true));
    renumberItems();
  }

  const remove = event.target.closest("[data-remove-item]");
  if (remove && document.querySelectorAll("#items .item-row").length > 1) {
    remove.closest(".item-row").remove(); // последнюю строку не удаляем: нужна хотя бы одна позиция
    renumberItems();
  }
});

// Начали вводить своё значение чаевых — выбираем пункт «своё».
document.addEventListener("input", (event) => {
  if (event.target.name === "tip_custom") {
    document.querySelector('input[name="tip_percent"][value="custom"]').checked = true;
  }
});

// --- Экран расхода ---

// Фото чека. <img src="/photos/..."> не подходит: обычная загрузка картинки не несёт заголовок
// X-Init-Data, и сервер ответил бы 401. Поэтому берём фото через fetch с заголовком
// и показываем его по blob-ссылке. Заглушка в разметке: <div class="receipt" data-photo-url="...">.
let photoBlobUrls = [];

async function loadPhotos() {
  // Прошлые blob-ссылки больше не нужны (экран сменился) — освобождаем память.
  photoBlobUrls.forEach((url) => URL.revokeObjectURL(url));
  photoBlobUrls = [];

  for (const box of app.querySelectorAll("[data-photo-url]")) {
    try {
      const response = await fetch(box.dataset.photoUrl, { headers: { "X-Init-Data": tg.initData } });
      if (!response.ok) throw new Error(response.status);
      const blobUrl = URL.createObjectURL(await response.blob());
      if (!box.isConnected) {
        URL.revokeObjectURL(blobUrl); // пока грузилось, экран уже сменился
        continue;
      }
      photoBlobUrls.push(blobUrl);
      const image = document.createElement("img");
      image.src = blobUrl;
      image.alt = "Фото чека";
      box.replaceChildren(image);
    } catch (error) {
      if (box.isConnected) box.textContent = "Фото недоступно";
    }
  }
}

// Нажали на миниатюру — показываем фото на весь экран; нажатие закрывает его.
document.addEventListener("click", (event) => {
  const image = event.target.closest(".receipt img");
  if (!image) return;
  const overlay = document.createElement("div");
  overlay.className = "photo-overlay";
  overlay.append(image.cloneNode());
  overlay.addEventListener("click", () => overlay.remove());
  document.body.append(overlay);
});

// «Закрыть расход». Если что-то не разобрано, спрашиваем, что делать с остатком,
// а если всё разобрано — просто переспрашиваем: закрытый расход уже не изменить.
// showPopup есть с версии 6.2 (в старых он падает) — тогда спрашиваем обычным confirm.
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-close-expense]");
  if (!button) return;

  const close = (mode) =>
    htmx.ajax("POST", button.dataset.closeExpense, { target: "#app", values: { leftover_mode: mode } });
  // Сумма в data-unclaimed могла устареть до 5 секунд: сервер принимает выбор как есть,
  // остаток он всё равно считает заново при закрытии.
  const unclaimed = button.dataset.unclaimedText;

  if (button.dataset.unclaimed === "0") {
    // Остатка нет — режим ни на что не влияет, но закрытие не отменить: переспрашиваем.
    askConfirm("Закрыть расход? Изменить его будет нельзя.", () => close("split"));
  } else if (tg.isVersionAtLeast && tg.isVersionAtLeast("6.2")) {
    tg.showPopup(
      {
        title: "Закрыть расход",
        message: `Не разобрано ${unclaimed}. Что сделать с остатком?`,
        buttons: [
          { id: "split", type: "default", text: "Поделить поровну между участниками" },
          { id: "payer", type: "default", text: "Оставить на мне" },
          { type: "cancel" },
        ],
      },
      (buttonId) => {
        if (buttonId === "split" || buttonId === "payer") close(buttonId);
      }
    );
  } else if (window.confirm(`Не разобрано ${unclaimed}. Поделить остаток поровну между участниками?`)) {
    close("split"); // «Отмена» ничего не делает
  }
});

// Подтверждения htmx (hx-confirm) показываем средствами Telegram: window.confirm там неудобен.
document.addEventListener("htmx:confirm", (event) => {
  if (!event.detail.question) return;
  event.preventDefault();
  // true — пропустить встроенное подтверждение htmx
  askConfirm(event.detail.question, () => event.detail.issueRequest(true));
});
