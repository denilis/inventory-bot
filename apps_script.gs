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
var SHEET_WRITEOFFS = 'Акты списания';       // лист с актами списания

// Индексы столбцов листа "Оборудование" (1-based для Sheets API)
// A=1 Дата, B=2 Кто вносил, C=3 Комплекс, D=4 Инв.номер,
// E=5 Категория, F=6 Состояние, G=7 Описание,
// H=8 Расположение (пусто), I=9 Фото наклейки, J=10 Шильдик, K=11 Общий вид,
// L=12 Подкатегория, M=13 Тип ТМЦ, N=14 Модель
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
// payload: { action, user, complex, inv, category, subcat, type_tmc, model,
//            condition, desc, ph1, ph2, ph3 }

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
  var user = p.user || Session.getActiveUser().getEmail() || 'dashboard';

  var row = [
    now,                   // A: Дата
    user,                  // B: Кто вносил
    p.complex   || '',     // C: Комплекс
    p.inv       || '',     // D: Инв.номер
    p.category  || '',     // E: Категория
    p.condition || '',     // F: Состояние
    p.desc      || '',     // G: Описание
    '',                    // H: Расположение (не собираем)
    p.ph1       || '',     // I: Фото наклейки
    p.ph2       || '',     // J: Фото шильдика
    p.ph3       || '',     // K: Фото общего вида
    p.subcat    || '',     // L: Подкатегория
    p.type_tmc  || '',     // M: Тип ТМЦ
    p.model     || '',     // N: Модель
  ];

  sheet.appendRow(row);
  return { status: 'ok', message: 'Оборудование добавлено: ' + p.inv };
}

// ── Добавление перемещения ────────────────────────────────────────────────────
// payload: { action, user, inv, from, to, reason, note }

function addMovement(p) {
  var ss    = getSpreadsheet();
  var sheet = ss.getSheetByName(SHEET_MOVEMENTS);
  if (!sheet) throw new Error('Лист "' + SHEET_MOVEMENTS + '" не найден');

  var now  = Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyy-MM-dd HH:mm');
  var user = p.user || Session.getActiveUser().getEmail() || 'dashboard';

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
// Создаёт акт в листе "Акты списания" и обновляет статус в "Оборудование" → "Списано"
// payload: { action, inv, reason, desc, who, photo }

function writeoff(p) {
  var ss      = getSpreadsheet();
  var eqSheet = ss.getSheetByName(SHEET_EQUIPMENT);
  if (!eqSheet) throw new Error('Лист "' + SHEET_EQUIPMENT + '" не найден');

  var inv   = (p.inv || '').trim().toLowerCase();
  var eqData = eqSheet.getDataRange().getValues();

  // Ищем оборудование
  var found    = false;
  var eqRow    = -1;
  var complex  = '';
  var category = '';

  for (var i = 1; i < eqData.length; i++) {
    var rowInv = String(eqData[i][COL_EQ_INV - 1]).trim().toLowerCase();
    if (rowInv === inv) {
      found    = true;
      eqRow    = i + 1;  // 1-based для Sheets
      complex  = eqData[i][2] || '';  // C = Комплекс
      category = eqData[i][4] || '';  // E = Категория
      break;
    }
  }

  if (!found) {
    throw new Error('Оборудование с номером "' + p.inv + '" не найдено');
  }

  // Лист "Акты списания" — создаём если не существует
  var woSheet = ss.getSheetByName(SHEET_WRITEOFFS);
  if (!woSheet) {
    woSheet = ss.insertSheet(SHEET_WRITEOFFS);
    woSheet.appendRow([
      'Дата', '№ Акта', 'Инв.номер', 'Комплекс',
      'Категория', 'Причина списания', 'Описание',
      'Кто списывает', 'Фото оборудования'
    ]);
  }

  // Генерируем номер акта
  var woData   = woSheet.getDataRange().getValues();
  var nextNum  = woData.length;  // строка 1 = заголовок
  var actNum   = 'АКТ-' + ('000' + nextNum).slice(-3);

  var now  = Utilities.formatDate(new Date(), 'Europe/Moscow', 'yyyy-MM-dd HH:mm');

  // Пишем акт списания
  woSheet.appendRow([
    now,
    actNum,
    p.inv      || '',
    complex,
    category,
    p.reason   || '',
    p.desc     || '',
    p.who      || '',
    p.photo    || '',
  ]);

  // Обновляем статус в "Оборудование" → "Списано"
  eqSheet.getRange(eqRow, COL_EQ_CONDITION).setValue('Списано');

  return {
    status: 'ok',
    actNumber: actNum,
    message: 'Акт ' + actNum + ' оформлен. Оборудование ' + p.inv + ' списано.'
  };
}
