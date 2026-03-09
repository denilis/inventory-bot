/**
 * Google Apps Script — веб-приложение для записи данных из дашборда.
 *
 * КАК РАЗВЕРНУТЬ:
 * 1. Откройте вашу Google Таблицу
 * 2. Расширения → Apps Script
 * 3. Вставьте этот код, замените SPREADSHEET_ID ниже на ID вашей таблицы
 * 4. Нажмите «Сохранить»
 * 5. Деплой → Управление публикациями → Создать новую публикацию
 *    - Тип: Веб-приложение
 *    - Выполнять как: Я (ваш аккаунт)
 *    - Доступ: Все (в т.ч. анонимные)
 * 6. Скопируйте URL веб-приложения → вставьте в dashboard.html: CFG.appsScriptUrl
 *
 * ВАЖНО: При каждом изменении скрипта нужно создавать НОВУЮ публикацию
 * (иначе старый задеплоенный код продолжит работать).
 */

// ── Настройки ─────────────────────────────────────────────────────────────────

var SPREADSHEET_ID  = '1FC8W38KwJ-zo9RbKDyZBPGfLMjRK6XFdkBVKEk7RPfU';  // ← ID таблицы
var SHEET_EQUIPMENT = 'Оборудование';        // лист с оборудованием
var SHEET_MOVEMENTS = 'Журнал перемещений';  // лист с перемещениями

// Индексы столбцов листа "Оборудование" (1-based для Sheets API)
// A=1 Дата, B=2 Кто вносил, C=3 Комплекс, D=4 Инв.номер,
// E=5 Категория, F=6 Состояние, G=7 Описание,
// H=8 Расположение (пусто), I=9 Фото наклейки, J=10 Шильдик, K=11 Общий вид
var COL_EQ_CONDITION = 6;   // столбец F (Состояние) — для операции writeoff
var COL_EQ_INV       = 4;   // столбец D (Инв.номер) — для поиска при writeoff

// ── Обработчик POST-запросов ──────────────────────────────────────────────────

function doPost(e) {
  var result;
  try {
    var payload = JSON.parse(e.postData.contents);
    var action  = payload.action;

    if      (action === 'add_equipment') result = addEquipment(payload);
    else if (action === 'move')          result = addMovement(payload);
    else if (action === 'writeoff')      result = writeoff(payload);
    else throw new Error('Неизвестное действие: ' + action);

  } catch (err) {
    result = { status: 'error', message: String(err) };
  }

  return ContentService
    .createTextOutput(JSON.stringify(result))
    .setMimeType(ContentService.MimeType.JSON);
}

// ── Обработчик GET (проверка работоспособности) ───────────────────────────────

function doGet(e) {
  var result = { status: 'ok', message: 'Apps Script работает' };
  return ContentService
    .createTextOutput(JSON.stringify(result))
    .setMimeType(ContentService.MimeType.JSON);
}

// ── Открытие таблицы ──────────────────────────────────────────────────────────

function getSpreadsheet() {
  if (SPREADSHEET_ID) {
    return SpreadsheetApp.openById(SPREADSHEET_ID);
  }
  // Если скрипт привязан к таблице — использовать её
  return SpreadsheetApp.getActiveSpreadsheet();
}

// ── Добавление оборудования ───────────────────────────────────────────────────
// payload: { action, complex, inv, category, condition, desc, ph1, ph2, ph3 }

function addEquipment(p) {
  var ss    = getSpreadsheet();
  var sheet = ss.getSheetByName(SHEET_EQUIPMENT);
  if (!sheet) throw new Error('Лист "' + SHEET_EQUIPMENT + '" не найден');

  // Проверка дубликата по инвентарному номеру
  var invNew = (p.inv || '').trim().toLowerCase();
  if (invNew && invNew !== '—') {
    var allData = sheet.getDataRange().getValues();
    for (var i = 1; i < allData.length; i++) {
      var rowInv = String(allData[i][COL_EQ_INV - 1]).trim().toLowerCase();
      if (rowInv === invNew) {
        throw new Error('Номер "' + p.inv + '" уже зарегистрирован в таблице');
      }
    }
  }

  var now  = Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyy-MM-dd HH:mm');
  var user = Session.getActiveUser().getEmail() || 'dashboard';

  var row = [
    now,
    user,
    p.complex   || '',
    p.inv       || '',
    p.category  || '',
    p.condition || '',
    p.desc      || '',
    '',              // H: Расположение внутри объекта — не собираем
    p.ph1       || '',
    p.ph2       || '',
    p.ph3       || '',
  ];

  sheet.appendRow(row);
  return { status: 'ok', message: 'Оборудование добавлено: ' + p.inv };
}

// ── Добавление перемещения ────────────────────────────────────────────────────
// payload: { action, inv, from, to, reason, note }

function addMovement(p) {
  var ss    = getSpreadsheet();
  var sheet = ss.getSheetByName(SHEET_MOVEMENTS);
  if (!sheet) throw new Error('Лист "' + SHEET_MOVEMENTS + '" не найден');

  var now  = Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyy-MM-dd HH:mm');
  var user = Session.getActiveUser().getEmail() || 'dashboard';

  var row = [
    now,
    user,
    p.inv    || '',
    p.from   || '',
    p.to     || '',
    p.reason || '',
    p.note   || '',
  ];

  sheet.appendRow(row);
  return { status: 'ok', message: 'Перемещение записано: ' + p.inv };
}

// ── Списание оборудования ─────────────────────────────────────────────────────
// Ищет строку по инв.номеру и обновляет столбец "Состояние"
// payload: { action, inv, note }

function writeoff(p) {
  var ss    = getSpreadsheet();
  var sheet = ss.getSheetByName(SHEET_EQUIPMENT);
  if (!sheet) throw new Error('Лист "' + SHEET_EQUIPMENT + '" не найден');

  var inv     = (p.inv || '').trim().toLowerCase();
  var data    = sheet.getDataRange().getValues();
  var updated = 0;

  // data[0] — заголовки, начинаем с data[1]
  for (var i = 1; i < data.length; i++) {
    var rowInv = String(data[i][COL_EQ_INV - 1]).trim().toLowerCase();
    if (rowInv === inv) {
      sheet.getRange(i + 1, COL_EQ_CONDITION).setValue('Не работает / списать');
      updated++;
    }
  }

  if (updated === 0) {
    throw new Error('Оборудование с номером "' + p.inv + '" не найдено');
  }

  return { status: 'ok', message: 'Списано записей: ' + updated + ' (' + p.inv + ')' };
}
