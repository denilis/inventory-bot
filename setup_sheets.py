"""
Скрипт одноразовой инициализации Google Sheets.

Запуск:
    python setup_sheets.py

Что делает:
  1. Открывает таблицу по SPREADSHEET_ID из .env
  2. Создаёт лист "Оборудование" с заголовками (или очищает первую строку)
  3. Создаёт лист "Перемещения" с заголовками
  4. Форматирует заголовки жирным
  5. Выводит ссылку на таблицу

Требования:
  - credentials.json (сервисный аккаунт Google)
  - .env с заполненными GOOGLE_CREDENTIALS_FILE и SPREADSHEET_ID
  - Таблица уже создана вручную и расшарена на email сервисного аккаунта
    (роль: Редактор)
"""

import os
import sys
from dotenv import load_dotenv

load_dotenv()

import gspread
from gspread.exceptions import WorksheetNotFound
from google.oauth2.service_account import Credentials

GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
SPREADSHEET_ID          = os.getenv("SPREADSHEET_ID", "")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

# Заголовки листов
HEADERS_EQUIPMENT = [
    "Дата",
    "Кто вносил",
    "Комплекс",
    "Инв. номер",
    "Категория",
    "Состояние",
    "Описание",
    "Фото наклейки",
    "Фото шильдика",
    "Фото общего вида",
]

HEADERS_MOVEMENTS = [
    "Дата",
    "Кто",
    "Инв. номер",
    "Откуда",
    "Куда",
    "Причина",
    "Примечание",
]

# Ширина столбцов (пиксели)
COL_WIDTHS_EQUIPMENT = [130, 150, 120, 120, 220, 200, 250, 250, 250, 250]
COL_WIDTHS_MOVEMENTS = [130, 150, 120, 120, 120, 200, 200]


def get_or_create_worksheet(sh: gspread.Spreadsheet, title: str, rows: int = 1000, cols: int = 20):
    """Возвращает лист по имени, создаёт если не существует."""
    try:
        ws = sh.worksheet(title)
        print(f"  Лист «{title}» найден (ID={ws.id})")
    except WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=rows, cols=cols)
        print(f"  Лист «{title}» создан (ID={ws.id})")
    return ws


def setup_worksheet(ws: gspread.Worksheet, headers: list[str], col_widths: list[int]):
    """Записывает заголовки в первую строку и форматирует их."""
    # Записываем заголовки
    ws.update("A1", [headers])

    # Жирный + фон заголовков
    last_col = chr(ord("A") + len(headers) - 1)
    header_range = f"A1:{last_col}1"
    ws.format(header_range, {
        "textFormat": {"bold": True},
        "backgroundColor": {"red": 0.9, "green": 0.9, "blue": 0.9},
        "borders": {
            "bottom": {"style": "SOLID", "width": 2}
        },
    })

    # Замораживаем первую строку
    ws.freeze(rows=1)

    print(f"  Заголовки записаны: {headers}")


def main():
    if not SPREADSHEET_ID:
        print("ОШИБКА: SPREADSHEET_ID не задан в .env")
        print("Создайте таблицу в Google Sheets и вставьте её ID в .env")
        sys.exit(1)

    if not os.path.isfile(GOOGLE_CREDENTIALS_FILE):
        print(f"ОШИБКА: файл '{GOOGLE_CREDENTIALS_FILE}' не найден")
        print("Создайте сервисный аккаунт Google и скачайте credentials.json")
        sys.exit(1)

    print("Подключаюсь к Google Sheets...")
    creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)

    try:
        sh = gc.open_by_key(SPREADSHEET_ID)
        print(f"Таблица открыта: «{sh.title}»")
    except Exception as e:
        print(f"ОШИБКА при открытии таблицы: {e}")
        print(
            "\nПроверьте:\n"
            "  1. SPREADSHEET_ID в .env — скопирован ли корректно?\n"
            "  2. Таблица расшарена на email сервисного аккаунта (Редактор)?\n"
            f"     Email аккаунта указан в {GOOGLE_CREDENTIALS_FILE}: поле 'client_email'"
        )
        sys.exit(1)

    # Лист "Оборудование"
    print("\nНастраиваю лист «Оборудование»...")
    eq_ws = get_or_create_worksheet(sh, "Оборудование")
    setup_worksheet(eq_ws, HEADERS_EQUIPMENT, COL_WIDTHS_EQUIPMENT)

    # Лист "Перемещения"
    print("\nНастраиваю лист «Перемещения»...")
    mv_ws = get_or_create_worksheet(sh, "Перемещения")
    setup_worksheet(mv_ws, HEADERS_MOVEMENTS, COL_WIDTHS_MOVEMENTS)

    url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit"
    print(f"\n✓ Готово!\nТаблица: {url}")
    print("\nДалее:")
    print("  1. Откройте таблицу по ссылке выше и убедитесь, что листы созданы")
    print("  2. Запустите бота: python bot.py")


if __name__ == "__main__":
    main()
