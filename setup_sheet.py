"""
Скрипт первоначальной настройки Google-таблицы.
Запустите один раз: python setup_sheet.py
Создаёт заголовки на листе «Оборудование» и «Журнал перемещений».
"""

import os
import sys

# Фикс для Windows: чтобы emoji не вызывали UnicodeEncodeError
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

load_dotenv()

GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "YOUR_SPREADSHEET_ID")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

EQUIPMENT_HEADERS = [
    "Дата внесения",
    "Кто вносил",
    "Спортивный комплекс",
    "Инвентарный номер",
    "Категория",
    "Состояние",
    "Описание / назначение",
    "Расположение внутри объекта",
    "Фото наклейки (ссылка)",
    "Фото шильдика (ссылка)",
    "Фото общего вида (ссылка)",
    "Голосовой комментарий (ссылка)",
]

MOVEMENT_HEADERS = [
    "Дата перемещения",
    "Кто оформил",
    "Инвентарный номер",
    "Откуда (комплекс)",
    "Куда (комплекс)",
    "Причина перемещения",
    "Примечание",
]


def main():
    creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)
    spreadsheet = gc.open_by_key(SPREADSHEET_ID)

    # Лист 1: Оборудование
    ws1 = spreadsheet.sheet1
    ws1.update_title("Оборудование")
    ws1.update(range_name="A1", values=[EQUIPMENT_HEADERS])
    ws1.format("A1:L1", {"textFormat": {"bold": True}})
    ws1.freeze(rows=1)
    print("✅ Лист «Оборудование» настроен")

    # Лист 2: Журнал перемещений
    try:
        ws2 = spreadsheet.worksheet("Журнал перемещений")
    except gspread.exceptions.WorksheetNotFound:
        ws2 = spreadsheet.add_worksheet(title="Журнал перемещений", rows=1000, cols=10)

    ws2.update(range_name="A1", values=[MOVEMENT_HEADERS])
    ws2.format("A1:G1", {"textFormat": {"bold": True}})
    ws2.freeze(rows=1)
    print("✅ Лист «Журнал перемещений» настроен")

    print(f"\n🔗 Таблица: https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit")


if __name__ == "__main__":
    main()
