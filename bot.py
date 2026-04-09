"""
Telegram-бот для инвентаризации оборудования спортивных комплексов.
Хранение: Битрикс24 Диск — фото, Google Sheets — данные.
"""

import asyncio
import base64
import io
import logging
import os
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardRemove,
    BotCommand,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import json
import gspread
from google.oauth2.service_account import Credentials
from PIL import Image
from pyzbar import pyzbar as pyzbar_lib
from openai import AsyncOpenAI

from bitrix24 import Bitrix24Client

# ─── Настройки ────────────────────────────────────────────────────────────────

BOT_TOKEN             = os.getenv("BOT_TOKEN", "")
OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY", "")

BITRIX_WEBHOOK_URL    = os.getenv("BITRIX_WEBHOOK_URL", "")
BITRIX_FOLDER_ID      = int(os.getenv("BITRIX_FOLDER_ID", "0"))

GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
SPREADSHEET_ID          = os.getenv("SPREADSHEET_ID", "")
DASHBOARD_URL           = os.getenv("DASHBOARD_URL", "")

# Листы Google Sheets
SHEET_EQUIPMENT  = "Оборудование"        # A=Дата, B=Кто вносил, C=Комплекс,
                                          # D=Инв.номер, E=Категория, F=Состояние,
                                          # G=Описание, H=Расположение (пусто),
                                          # I=Фото наклейки, J=Фото шильдика,
                                          # K=Фото общего вида,
                                          # L=Подкатегория, M=Тип ТМЦ, N=Модель
SHEET_MOVEMENTS  = "Журнал перемещений"  # A=Дата, B=Кто, C=Инв.номер,
                                          # D=Откуда, E=Куда, F=Причина, G=Примечание
SHEET_WRITEOFFS  = "Акты списания"       # A=Дата, B=№ акта, C=Инв.номер,
                                          # D=Комплекс, E=Категория, F=Причина,
                                          # G=Описание, H=Кто списывает, I=Фото,
                                          # J=Статус, K=Дата подтв.1, L=Дата подтв.2,
                                          # M=Комм.отклонения, N=TG ID инициатора
SHEET_USERS      = os.getenv("SHEET_USERS", "Пользователи")
                                          # A=TG ID, B=ФИО, C=B24 ID,
                                          # D=Комплекс, E=Дата регистрации

# Утверждающие (согласование актов списания)
APPROVER_1_TG_ID  = int(os.getenv("APPROVER_1_TG_ID",  "0"))
APPROVER_1_B24_ID = int(os.getenv("APPROVER_1_B24_ID", "0"))
APPROVER_1_NAME   = os.getenv("APPROVER_1_NAME",  "Утверждающий 1")

APPROVER_2_TG_ID  = int(os.getenv("APPROVER_2_TG_ID",  "0"))
APPROVER_2_B24_ID = int(os.getenv("APPROVER_2_B24_ID", "0"))
APPROVER_2_NAME   = os.getenv("APPROVER_2_NAME",  "Утверждающий 2")

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

# Справочники
SPORT_COMPLEXES = [
    "Остров", "Маяк", "Юрловский", "Косино", "Арктика", "Янтарь", "Офис",
]

# Иерархические категории: категория → [подкатегории]
CATEGORIES_TREE = {
    "Климатическое": [
        "Чиллер", "Тепловая пушка", "Кондиционер", "Обогреватель",
        "Вентилятор", "Другое",
    ],
    "Строительное": [
        "Перфоратор", "Болгарка (УШМ)", "Шуруповёрт", "Строительный фен",
        "Дрель", "Бетономешалка", "Другое",
    ],
    "Уборочное": [
        "Пылесос", "Мойка высокого давления", "Поломоечная машина",
        "Снегоуборщик", "Другое",
    ],
    "Электрика": [
        "Генератор", "Удлинитель", "Электрощиток", "Светильник", "Другое",
    ],
    "Спортивное оборудование": [
        "Тренажёр", "Спортивный инвентарь", "Другое",
    ],
    "Офисная техника": [
        "Компьютер", "Ноутбук", "Монитор", "МФУ", "Принтер", "Сканер",
        "IP-телефон", "Проектор", "Телевизор", "ИБП",
        "Сетевое оборудование", "Шредер", "Другое",
    ],
    "Мебель": [
        "Шкаф", "Стол", "Стул", "Стеллаж", "Тумба", "Скамейка", "Диван", "Другое",
    ],
    "Инвентарь": [
        "Лопата", "Метла", "Ведро", "Тележка", "Стремянка", "Другое",
    ],
    "Другое": ["Другое"],
}

CATEGORIES = list(CATEGORIES_TREE.keys())

# Категории БЕЗ шильдика (не электроприборы).
# Для них нужны только 2 фото: общий вид + наклейка/QR-код.
# Для остальных — 3 фото: наклейка/QR + шильдик + общий вид.
CATEGORIES_WITHOUT_NAMEPLATE = {"Мебель", "Инвентарь", "Другое"}


def has_nameplate(category: str) -> bool:
    """True если у категории ожидается шильдик (заводская табличка)."""
    return category not in CATEGORIES_WITHOUT_NAMEPLATE


def photos_needed(category: str) -> int:
    """Сколько фото должен сделать пользователь для этой категории."""
    return 3 if has_nameplate(category) else 2


def photo_instructions(category: str) -> str:
    """Текст инструкции по фото под конкретную категорию."""
    if has_nameplate(category):
        return (
            "Нужно 3 фото:\n"
            "  1. Наклейка / QR-код\n"
            "  2. Шильдик (заводская табличка)\n"
            "  3. Общий вид\n"
        )
    return (
        "Нужно 2 фото:\n"
        "  1. Наклейка / QR-код\n"
        "  2. Общий вид\n"
    )

CONDITIONS = [
    "Отличное — работает без замечаний",
    "Рабочее — есть мелкие замечания",
    "Требует ремонта",
    "Не работает / списать",
    "Не проверялось",
]

MOVE_REASONS = [
    "Перевод на другой объект",
    "Временная аренда",
    "На ремонт",
    "После ремонта",
    "Другое",
]

WRITEOFF_REASONS = [
    "Износ",
    "Поломка",
    "Утеря",
    "Моральное устаревание",
    "Другое",
]

# Количество фото зависит от категории — см. photos_needed() выше.

# ─── Логирование ──────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ─── Клиенты ──────────────────────────────────────────────────────────────────

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
bx = Bitrix24Client(BITRIX_WEBHOOK_URL) if BITRIX_WEBHOOK_URL else None

# ─── Google Sheets ─────────────────────────────────────────────────────────────

GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")


def _get_google_creds() -> Credentials:
    """Создаёт Google Credentials из env-переменной JSON или из файла."""
    if GOOGLE_CREDENTIALS_JSON:
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        return Credentials.from_service_account_info(info, scopes=GOOGLE_SCOPES)
    return Credentials.from_service_account_file(
        GOOGLE_CREDENTIALS_FILE, scopes=GOOGLE_SCOPES
    )


def _open_sheets() -> tuple[gspread.Worksheet, gspread.Worksheet]:
    """Открывает листы Google Sheets. Запускать через asyncio.to_thread."""
    gc = gspread.authorize(_get_google_creds())
    sh = gc.open_by_key(SPREADSHEET_ID)
    eq_sheet = sh.worksheet(SHEET_EQUIPMENT)
    mv_sheet = sh.worksheet(SHEET_MOVEMENTS)
    return eq_sheet, mv_sheet


def _open_eq_sheet() -> gspread.Worksheet:
    """Открывает только лист оборудования. Запускать через asyncio.to_thread."""
    gc = gspread.authorize(_get_google_creds())
    sh = gc.open_by_key(SPREADSHEET_ID)
    return sh.worksheet(SHEET_EQUIPMENT)


def _open_writeoff_sheet() -> gspread.Worksheet:
    """Открывает лист актов списания. Создаёт с заголовками если не существует."""
    gc = gspread.authorize(_get_google_creds())
    sh = gc.open_by_key(SPREADSHEET_ID)
    try:
        return sh.worksheet(SHEET_WRITEOFFS)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_WRITEOFFS, rows=1000, cols=14)
        ws.append_row([
            "Дата", "№ Акта", "Инв.номер", "Комплекс",
            "Категория", "Причина списания", "Описание",
            "Кто списывает", "Фото оборудования",
            "Статус", "Дата подтв.1", "Дата подтв.2",
            "Комм.отклонения", "TG ID инициатора",
        ])
        return ws


def _open_users_sheet() -> gspread.Worksheet:
    """Открывает или создаёт лист «Пользователи»."""
    gc = gspread.authorize(_get_google_creds())
    sh = gc.open_by_key(SPREADSHEET_ID)
    try:
        return sh.worksheet(SHEET_USERS)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_USERS, rows=500, cols=5)
        ws.append_row(["TG ID", "ФИО", "B24 ID", "Комплекс", "Дата регистрации"])
        return ws


def _get_user_by_tg_id(tg_id: int) -> dict | None:
    """Ищет пользователя в листе «Пользователи» по TG ID. None если не найден."""
    ws = _open_users_sheet()
    all_rows = ws.get_all_values()
    for row in all_rows[1:]:
        if row and str(row[0]).strip() == str(tg_id):
            return {
                "tg_id": row[0],
                "name": row[1] if len(row) > 1 else "",
                "b24_id": row[2] if len(row) > 2 else "",
                "complex": row[3] if len(row) > 3 else "",
            }
    return None


def _save_user(tg_id: int, name: str, b24_id: str, complex_name: str = "") -> None:
    """Сохраняет нового пользователя в лист «Пользователи»."""
    ws = _open_users_sheet()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    ws.append_row([str(tg_id), name, b24_id, complex_name, now])


# ─── FSM ──────────────────────────────────────────────────────────────────────

class Form(StatesGroup):
    choose_complex      = State()   # 1. Выбор комплекса
    choose_category     = State()   # 2. Категория (сначала, чтобы подобрать фото)
    choose_subcategory  = State()   # 3. Подкатегория
    photos              = State()   # 4. Сбор фото (2 или 3 шт. в зависимости от категории)
    inv_number          = State()   # 5. Инвентарный номер (обязательный)
    type_tmc            = State()   # 6. Тип ТМЦ (только для категорий с шильдиком)
    model               = State()   # 7. Модель (только для категорий с шильдиком)
    choose_condition    = State()   # 8. Состояние
    description         = State()   # 9. Описание
    confirm             = State()   # Подтверждение


class FindForm(StatesGroup):
    inv_number = State()


class MoveForm(StatesGroup):
    inv_number   = State()
    from_complex = State()
    to_complex   = State()
    reason       = State()
    note         = State()
    confirm      = State()


class WriteoffForm(StatesGroup):
    inv_number  = State()
    reason      = State()
    description = State()
    photo       = State()
    who         = State()
    confirm     = State()


class RegisterForm(StatesGroup):
    enter_name = State()   # Пользователь вводит имя/фамилию для поиска в Б24
    pick_user  = State()   # Выбирает из найденных кандидатов


class RejectForm(StatesGroup):
    comment = State()      # Комментарий при отклонении акта


# ─── Вспомогательные функции ──────────────────────────────────────────────────

def inline_kb(items: list[str], prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=item, callback_data=f"{prefix}:{i}")]
            for i, item in enumerate(items)
        ]
    )


def decode_qr(image_bytes: bytes) -> str | None:
    try:
        img = Image.open(io.BytesIO(image_bytes))
        results = pyzbar_lib.decode(img)
        if results:
            return results[0].data.decode("utf-8", errors="replace").strip()
    except Exception as e:
        log.warning(f"QR decode: {e}")
    return None


async def ocr_with_gpt(image_bytes: bytes) -> str | None:
    if not openai_client:
        return None
    try:
        b64 = base64.b64encode(image_bytes).decode()
        resp = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "На фото наклейка с инвентарным номером. "
                            "Верни ТОЛЬКО инвентарный номер (например INV-001, А-123). "
                            "Если номер не виден — ответь НЕТ."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                ],
            }],
            max_tokens=30,
        )
        result = resp.choices[0].message.content.strip()
        return None if result.upper().startswith("НЕТ") or not result else result
    except Exception as e:
        log.error(f"GPT OCR: {e}")
    return None


async def ocr_nameplate(image_bytes: bytes) -> dict:
    """Считывает тип и модель с фото шильдика через GPT-4o Vision.
    Возвращает {'type_tmc': ..., 'model': ...}."""
    if not openai_client:
        return {}
    try:
        b64 = base64.b64encode(image_bytes).decode()
        resp = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "На фото заводская табличка (шильдик) оборудования. "
                            "Прочитай и верни JSON с двумя полями:\n"
                            '{"type_tmc": "тип/название оборудования", "model": "модель"}\n'
                            "Если данные не видны — верни пустые строки."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    },
                ],
            }],
            max_tokens=100,
        )
        text = resp.choices[0].message.content.strip()
        # Пробуем распарсить JSON
        if "{" in text:
            start = text.index("{")
            end = text.rindex("}") + 1
            return json.loads(text[start:end])
    except Exception as e:
        log.error(f"GPT nameplate OCR: {e}")
    return {}


async def transcribe_voice(bot: Bot, file_id: str) -> str:
    """Расшифровывает голосовое сообщение через Whisper. Возвращает текст."""
    if not openai_client:
        return ""
    try:
        tg_file = await bot.get_file(file_id)
        raw = await bot.download_file(tg_file.file_path)
        raw.name = "voice.ogg"
        transcript = await openai_client.audio.transcriptions.create(
            model="whisper-1", file=raw, language="ru",
        )
        return transcript.text.strip()
    except Exception as e:
        log.error(f"Whisper: {e}")
    return ""


async def upload_to_bitrix(bot: Bot, file_id: str, filename: str) -> str:
    """Скачивает фото из Telegram и загружает в Битрикс24 Диск.
    Возвращает публичную ссылку."""
    if not bx or not BITRIX_FOLDER_ID:
        return ""
    tg_file = await bot.get_file(file_id)
    raw = await bot.download_file(tg_file.file_path)
    file_bytes = raw.read()
    uploaded = await bx.upload_file(BITRIX_FOLDER_ID, filename, file_bytes)
    file_id_bx = int(uploaded["ID"])
    link = await bx.get_file_public_link(file_id_bx)
    return link or uploaded.get("DOWNLOAD_URL", "")


async def _suggest_next_inv() -> str:
    """Подсказывает следующий свободный инв. номер (макс.число + 1, дополненный до 3 цифр)."""
    import re
    def _calc():
        sheet = _open_eq_sheet()
        values = sheet.col_values(4)  # col D
        max_num = 0
        for v in values[1:]:
            match = re.search(r"(\d+)", v.strip())
            if match:
                num = int(match.group(1))
                if num > max_num:
                    max_num = num
        return str(max_num + 1).zfill(3)
    return await asyncio.to_thread(_calc)


async def _ensure_auth(message: Message, state: FSMContext,
                       after_action: str = "") -> dict | None:
    """Проверяет регистрацию пользователя.
    Если пользователь известен — возвращает его данные.
    Если нет — запускает RegisterForm и возвращает None."""
    tg_id = message.from_user.id
    user_data = await asyncio.to_thread(_get_user_by_tg_id, tg_id)
    if user_data:
        return user_data
    await state.update_data(after_register=after_action)
    await state.set_state(RegisterForm.enter_name)
    await message.answer(
        "Вы не зарегистрированы в системе.\n\n"
        "Введите своё имя или фамилию для поиска в Битрикс24:"
    )
    return None


async def _notify_approver(
    bot: Bot,
    approver_tg_id: int,
    approver_b24_id: int,
    act_number: str,
    inv: str,
    data: dict,
    photo_link: str,
    now: str,
) -> None:
    """Отправляет уведомление утверждающему через Telegram и создаёт задачу в Б24."""
    text = (
        f"Запрос на списание оборудования\n\n"
        f"Акт: {act_number}\n"
        f"Инв. номер: {inv}\n"
        f"Комплекс: {data.get('eq_complex', '—')}\n"
        f"Категория: {data.get('eq_category', '—')}\n"
        f"Причина: {data.get('reason', '—')}\n"
        f"Описание: {data.get('wo_description', '—')}\n"
        f"Кто списывает: {data.get('who', '—')}\n"
        f"Дата: {now}"
    )
    if photo_link:
        text += f"\nФото: {photo_link}"

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить",
                             callback_data=f"approve:yes:{act_number}"),
        InlineKeyboardButton(text="❌ Отклонить",
                             callback_data=f"approve:no:{act_number}"),
    ]])

    if approver_tg_id:
        try:
            await bot.send_message(approver_tg_id, text, reply_markup=kb)
        except Exception as e:
            log.error(f"Уведомление утверждающему {approver_tg_id}: {e}")

    if bx and approver_b24_id:
        try:
            await bx.create_task(
                title=f"Подтверждение списания {act_number}",
                description=text,
                responsible_id=approver_b24_id,
            )
        except Exception as e:
            log.error(f"Задача Б24 для {approver_b24_id}: {e}")


# ─── Роутер ───────────────────────────────────────────────────────────────────

router = Router()


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user = await _ensure_auth(message, state, after_action="start")
    if user is None:
        return
    kb_rows = list(inline_kb(SPORT_COMPLEXES, "complex").inline_keyboard)
    if DASHBOARD_URL:
        kb_rows.append([InlineKeyboardButton(text="📊 Открыть дашборд", url=DASHBOARD_URL)])
    await message.answer(
        "Учёт ТМЦ — инвентаризация оборудования\n\n"
        "Команды:\n"
        "  /start      — добавить оборудование\n"
        "  /move       — оформить перемещение\n"
        "  /writeoff   — списать оборудование\n"
        "  /find       — найти по номеру\n"
        "  /dashboard  — открыть дашборд\n"
        "  /guide      — инструкция\n"
        "  /cancel     — отменить текущее действие\n\n"
        "Выберите спортивный комплекс:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )
    await state.set_state(Form.choose_complex)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Действие отменено.\n/start — начать заново.",
        reply_markup=ReplyKeyboardRemove(),
    )


# ── Регистрация пользователя ───────────────────────────────────────────────────

@router.message(RegisterForm.enter_name)
async def reg_enter_name(message: Message, state: FSMContext):
    name_query = message.text.strip()
    if not name_query:
        await message.answer("Введите имя или фамилию.")
        return

    if not bx:
        # Битрикс24 не настроен — регистрируем без B24 ID
        await asyncio.to_thread(_save_user, message.from_user.id, name_query, "")
        data = await state.get_data()
        after_action = data.get("after_register", "")
        await state.clear()
        suffix = f"\n\nТеперь используйте /{after_action}" if after_action else ""
        await message.answer(f"Зарегистрированы как: {name_query}{suffix}")
        return

    await message.answer("Ищу в Битрикс24...")
    try:
        users = await bx.get_users(name_query)
    except Exception as e:
        log.error(f"B24 get_users: {e}")
        users = []

    if not users:
        await message.answer(
            f"Пользователь «{name_query}» не найден в Битрикс24.\n\n"
            "Попробуйте другое имя/фамилию:"
        )
        return  # остаёмся в RegisterForm.enter_name

    candidates = [
        f"{u.get('NAME', '')} {u.get('LAST_NAME', '')}".strip()
        for u in users[:5]
    ]
    await state.update_data(b24_candidates=users[:5])
    await message.answer(
        "Найдены пользователи. Выберите себя:",
        reply_markup=inline_kb(candidates, "regpick"),
    )
    await state.set_state(RegisterForm.pick_user)


@router.callback_query(RegisterForm.pick_user, F.data.startswith("regpick:"))
async def reg_pick_user(cb: CallbackQuery, state: FSMContext):
    idx = int(cb.data.split(":")[1])
    data = await state.get_data()
    candidates: list = data.get("b24_candidates", [])
    if idx >= len(candidates):
        await cb.answer("Ошибка выбора", show_alert=True)
        return

    chosen = candidates[idx]
    b24_id = str(chosen.get("ID", ""))
    full_name = f"{chosen.get('NAME', '')} {chosen.get('LAST_NAME', '')}".strip()

    await asyncio.to_thread(_save_user, cb.from_user.id, full_name, b24_id)

    after_action = data.get("after_register", "")
    await state.clear()

    suffix = f"\n\nТеперь используйте /{after_action}" if after_action else ""
    await cb.message.answer(
        f"Добро пожаловать, {full_name}! Вы зарегистрированы в системе.{suffix}"
    )
    await cb.answer()


@router.message(Command("dashboard"))
async def cmd_dashboard(message: Message):
    if not DASHBOARD_URL:
        await message.answer("Дашборд пока не настроен (DASHBOARD_URL не задан).")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📊 Открыть дашборд", url=DASHBOARD_URL),
    ]])
    await message.answer("Панель управления ТМЦ:", reply_markup=kb)


@router.message(Command("guide"))
async def cmd_guide(message: Message):
    guide_url = DASHBOARD_URL.rstrip("/") + "#help" if DASHBOARD_URL else ""
    if not guide_url:
        await message.answer("Дашборд пока не настроен (DASHBOARD_URL не задан).")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📖 Инструкция", url=guide_url),
    ]])
    await message.answer("Инструкция по работе с системой учёта ТМЦ:", reply_markup=kb)


# ── Шаг 1: Комплекс ───────────────────────────────────────────────────────────

@router.callback_query(Form.choose_complex, F.data.startswith("complex:"))
async def step_complex(cb: CallbackQuery, state: FSMContext):
    idx = int(cb.data.split(":")[1])
    complex_name = SPORT_COMPLEXES[idx]
    await state.update_data(complex=complex_name, photos=[], ocr_done=False,
                            nameplate_data={})
    await cb.message.answer(
        f"Комплекс: {complex_name}\n\n"
        "Выберите категорию оборудования:",
        reply_markup=inline_kb(CATEGORIES, "cat"),
    )
    await state.set_state(Form.choose_category)
    await cb.answer()


# ── Шаг 4: Фото ───────────────────────────────────────────────────────────────

# Метки ячеек в таблице (I/J/K) в зависимости от наличия шильдика.
PHOTO_LABELS_WITH_NAMEPLATE    = ["наклейка", "шильдик", "общий_вид"]
PHOTO_LABELS_WITHOUT_NAMEPLATE = ["наклейка", "общий_вид"]


def photo_labels_for(category: str) -> list[str]:
    return PHOTO_LABELS_WITH_NAMEPLATE if has_nameplate(category) else PHOTO_LABELS_WITHOUT_NAMEPLATE


@router.message(Form.photos, F.photo)
async def step_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    photos: list = data.get("photos", [])
    ocr_done: bool = data.get("ocr_done", False)
    nameplate_data: dict = data.get("nameplate_data", {})
    category = data.get("category", "")
    n_needed = photos_needed(category)
    with_nameplate = has_nameplate(category)

    file_id = message.photo[-1].file_id
    photos.append(file_id)
    count = len(photos)

    # OCR на первом фото (наклейка) — инвентарный номер
    inv_from_ocr = None
    if not ocr_done and count == 1:
        try:
            tg_file = await message.bot.get_file(file_id)
            raw = await message.bot.download_file(tg_file.file_path)
            img_bytes = raw.read()
            inv_from_ocr = decode_qr(img_bytes) or await ocr_with_gpt(img_bytes)
        except Exception as e:
            log.warning(f"OCR failed: {e}")
        if inv_from_ocr:
            await state.update_data(inv_number=inv_from_ocr, ocr_done=True)

    # OCR шильдика (тип и модель) — только для категорий с шильдиком,
    # и только на 2-м фото (шильдик).
    if with_nameplate and count == 2 and not nameplate_data:
        try:
            tg_file = await message.bot.get_file(file_id)
            raw = await message.bot.download_file(tg_file.file_path)
            img_bytes = raw.read()
            nameplate_data = await ocr_nameplate(img_bytes)
        except Exception as e:
            log.warning(f"Nameplate OCR failed: {e}")
        if nameplate_data:
            await state.update_data(nameplate_data=nameplate_data)

    await state.update_data(photos=photos, ocr_done=ocr_done or bool(inv_from_ocr))

    if count < n_needed:
        remaining = n_needed - count
        status = f"Фото {count} из {n_needed} получено."
        if inv_from_ocr:
            status += f" Номер считан: {inv_from_ocr}"
        if with_nameplate and count == 2 and nameplate_data:
            np_type = nameplate_data.get("type_tmc", "")
            np_model = nameplate_data.get("model", "")
            if np_type or np_model:
                status += f"\nС шильдика: {np_type} {np_model}".strip()
        # Подсказка какое следующее фото ожидается
        next_hint = ""
        if with_nameplate:
            next_labels = ["шильдик (заводская табличка)", "общий вид"]
            if count - 1 < len(next_labels):
                next_hint = f"\nСледующее фото: {next_labels[count - 1]}."
        else:
            if count == 1:
                next_hint = "\nСледующее фото: общий вид."
        await message.answer(
            f"{status}{next_hint}\n"
            f"Осталось фото: {remaining}. Продолжайте или /done."
        )
    else:
        await state.update_data(photos=photos)
        await _ask_inv_number(message, state)


@router.message(Form.photos, Command("done"))
async def step_photos_done(message: Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    if not photos:
        await message.answer("Ни одного фото не добавлено. Отправьте хотя бы одно фото или /skip.")
        return
    await _ask_inv_number(message, state)


@router.message(Form.photos, Command("skip"))
async def step_photos_skip(message: Message, state: FSMContext):
    await state.update_data(photos=[])
    await _ask_inv_number(message, state)


async def _ask_inv_number(message: Message, state: FSMContext):
    data = await state.get_data()
    inv = data.get("inv_number")

    # Подсказываем следующий свободный номер
    try:
        suggested = await _suggest_next_inv()
    except Exception:
        suggested = None

    if inv:
        hint = f"Инвентарный номер считан автоматически: <b>{inv}</b>\n\n"
        if suggested:
            hint += f"(Подсказка: следующий свободный — {suggested})\n\n"
        hint += "Введите другой номер, если неверно, или отправьте /ok чтобы подтвердить."
    else:
        hint = "Введите инвентарный номер (обязательное поле).\n"
        if suggested:
            hint += f"\nПодсказка: следующий свободный — <b>{suggested}</b>"
        hint += "\n\nНомер должен совпадать с номером на наклейке оборудования."

    await message.answer(hint, parse_mode="HTML")
    await state.set_state(Form.inv_number)


# ── Шаг 3: Инвентарный номер (обязательный) ─────────────────────────────────

async def _inv_exists(inv: str) -> bool:
    """True если инвентарный номер уже есть в таблице оборудования."""
    if not inv or inv in ("—", ""):
        return False
    def _check():
        sheet = _open_eq_sheet()
        values = sheet.col_values(4)  # col D (инв. номер), 1-based
        return inv.strip().lower() in (v.strip().lower() for v in values[1:])
    return await asyncio.to_thread(_check)


@router.message(Form.inv_number, Command("ok"))
async def step_inv_ok(message: Message, state: FSMContext):
    """Подтверждение OCR-номера через /ok."""
    data = await state.get_data()
    inv = data.get("inv_number")
    if not inv:
        await message.answer(
            "Номер не задан. Введите инвентарный номер вручную."
        )
        return
    if await _inv_exists(inv):
        await state.update_data(inv_number=None, ocr_done=False)
        await message.answer(
            f"Номер <b>{inv}</b> уже зарегистрирован в таблице.\n\n"
            "Введите другой номер.",
            parse_mode="HTML",
        )
        return
    await _after_inv_number(message, state)


@router.message(Form.inv_number)
async def step_inv_number(message: Message, state: FSMContext):
    inv = message.text.strip()
    if not inv:
        await message.answer("Инвентарный номер обязателен. Введите номер.")
        return
    if await _inv_exists(inv):
        await message.answer(
            f"Номер <b>{inv}</b> уже зарегистрирован в таблице.\n\n"
            "Введите другой номер.",
            parse_mode="HTML",
        )
        return  # остаёмся в состоянии Form.inv_number
    await state.update_data(inv_number=inv)
    await _after_inv_number(message, state)


async def _after_inv_number(message: Message, state: FSMContext):
    """После инв. номера — либо тип ТМЦ/модель (с шильдиком), либо сразу состояние."""
    data = await state.get_data()
    cat = data.get("category", "")
    if has_nameplate(cat):
        await _ask_type_tmc(message, state)
    else:
        # Для мебели/инвентаря тип и модель не заполняем
        await state.update_data(type_tmc="—", model="—")
        await _ask_condition(message, state)


# ── Шаг 2: Категория ──────────────────────────────────────────────────────────

@router.callback_query(Form.choose_category, F.data.startswith("cat:"))
async def step_category(cb: CallbackQuery, state: FSMContext):
    idx = int(cb.data.split(":")[1])
    cat = CATEGORIES[idx]
    subcats = CATEGORIES_TREE.get(cat, ["Другое"])
    await state.update_data(category=cat)

    if len(subcats) == 1 and subcats[0] == "Другое":
        # Единственная подкатегория — пропускаем выбор
        await state.update_data(subcategory="Другое")
        await _ask_photos(cb.message, state)
    else:
        await cb.message.answer(
            f"Категория: {cat}\n\nВыберите подкатегорию:",
            reply_markup=inline_kb(subcats, "subcat"),
        )
        await state.set_state(Form.choose_subcategory)
    await cb.answer()


# ── Шаг 3: Подкатегория ─────────────────────────────────────────────────────

@router.callback_query(Form.choose_subcategory, F.data.startswith("subcat:"))
async def step_subcategory(cb: CallbackQuery, state: FSMContext):
    idx = int(cb.data.split(":")[1])
    data = await state.get_data()
    cat = data.get("category", "")
    subcats = CATEGORIES_TREE.get(cat, ["Другое"])
    subcat = subcats[idx] if idx < len(subcats) else "Другое"
    await state.update_data(subcategory=subcat)
    await _ask_photos(cb.message, state)
    await cb.answer()


# ── Шаг 4: Подсказка по фото ────────────────────────────────────────────────

async def _ask_photos(message: Message, state: FSMContext):
    data = await state.get_data()
    cat = data.get("category", "")
    subcat = data.get("subcategory", "")
    n_photos = photos_needed(cat)
    instructions = photo_instructions(cat)

    text = (
        f"Категория: {cat}" + (f" / {subcat}" if subcat and subcat != cat else "") + "\n\n"
        f"Сфотографируйте оборудование. {instructions}\n"
        "Отправляйте фото по одному. Когда всё готово — /done\n"
        "Или /skip чтобы пропустить фото."
    )
    await message.answer(text)
    await state.set_state(Form.photos)


# ── Шаг 6: Тип ТМЦ ──────────────────────────────────────────────────────────

async def _ask_type_tmc(message: Message, state: FSMContext):
    data = await state.get_data()
    nameplate = data.get("nameplate_data", {})
    np_type = nameplate.get("type_tmc", "")

    hint = "Укажите тип/название оборудования (с шильдика).\n"
    if np_type:
        hint += f"\nСчитано с фото: <b>{np_type}</b>\n/ok — подтвердить, или введите вручную."
        await state.update_data(type_tmc=np_type)
    else:
        hint += "\nНапример: «Кондиционер сплит-система», «Перфоратор».\n/skip — пропустить."
    await message.answer(hint, parse_mode="HTML")
    await state.set_state(Form.type_tmc)


@router.message(Form.type_tmc, Command("ok"))
async def step_type_ok(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("type_tmc"):
        await _ask_model(message, state)
    else:
        await message.answer("Тип не задан. Введите тип оборудования или /skip.")


@router.message(Form.type_tmc, Command("skip"))
async def step_type_skip(message: Message, state: FSMContext):
    await state.update_data(type_tmc="—")
    await _ask_model(message, state)


@router.message(Form.type_tmc)
async def step_type_text(message: Message, state: FSMContext):
    await state.update_data(type_tmc=message.text.strip())
    await _ask_model(message, state)


# ── Шаг 7: Модель ───────────────────────────────────────────────────────────

async def _ask_model(message: Message, state: FSMContext):
    data = await state.get_data()
    nameplate = data.get("nameplate_data", {})
    np_model = nameplate.get("model", "")

    hint = "Укажите модель оборудования (с шильдика).\n"
    if np_model:
        hint += f"\nСчитано с фото: <b>{np_model}</b>\n/ok — подтвердить, или введите вручную."
        await state.update_data(model=np_model)
    else:
        hint += "\nНапример: «MDV-12HRN1», «ТЭП-3000К».\n/skip — пропустить."
    await message.answer(hint, parse_mode="HTML")
    await state.set_state(Form.model)


@router.message(Form.model, Command("ok"))
async def step_model_ok(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("model"):
        await _ask_condition(message, state)
    else:
        await message.answer("Модель не задана. Введите модель или /skip.")


@router.message(Form.model, Command("skip"))
async def step_model_skip(message: Message, state: FSMContext):
    await state.update_data(model="—")
    await _ask_condition(message, state)


@router.message(Form.model)
async def step_model_text(message: Message, state: FSMContext):
    await state.update_data(model=message.text.strip())
    await _ask_condition(message, state)


# ── Шаг 8: Состояние ────────────────────────────────────────────────────────

async def _ask_condition(message: Message, state: FSMContext):
    await message.answer(
        "Укажите состояние оборудования:",
        reply_markup=inline_kb(CONDITIONS, "cond"),
    )
    await state.set_state(Form.choose_condition)


@router.callback_query(Form.choose_condition, F.data.startswith("cond:"))
async def step_condition(cb: CallbackQuery, state: FSMContext):
    idx = int(cb.data.split(":")[1])
    await state.update_data(condition=CONDITIONS[idx])
    await cb.message.answer(
        "Добавьте описание: назначение, замечания, расположение.\n\n"
        "Можно написать текстом или отправить голосовое сообщение.\n"
        "/skip — пропустить."
    )
    await state.set_state(Form.description)
    await cb.answer()


# ── Шаг 9: Описание ─────────────────────────────────────────────────────────

@router.message(Form.description, F.voice)
async def step_description_voice(message: Message, state: FSMContext):
    await message.answer("Распознаю речь...")
    text = await transcribe_voice(message.bot, message.voice.file_id)
    if text:
        await state.update_data(description=f"[голос] {text}")
        await message.answer(f"Распознано: {text}")
    else:
        await state.update_data(description="[голосовое сообщение]")
        await message.answer("Голосовое сохранено (текст не распознан).")
    await _show_summary(message, state)


@router.message(Form.description, Command("skip"))
async def step_description_skip(message: Message, state: FSMContext):
    await state.update_data(description="—")
    await _show_summary(message, state)


@router.message(Form.description)
async def step_description_text(message: Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await _show_summary(message, state)


# ── Подтверждение ─────────────────────────────────────────────────────────────

async def _show_summary(message: Message, state: FSMContext):
    data = await state.get_data()
    photo_count = len(data.get("photos", []))

    text = (
        "Проверьте данные:\n\n"
        f"Комплекс:      {data.get('complex', '—')}\n"
        f"Инв. номер:    {data.get('inv_number', '—')}\n"
        f"Категория:     {data.get('category', '—')}\n"
        f"Подкатегория:  {data.get('subcategory', '—')}\n"
        f"Тип ТМЦ:       {data.get('type_tmc', '—')}\n"
        f"Модель:        {data.get('model', '—')}\n"
        f"Состояние:     {data.get('condition', '—')}\n"
        f"Описание:      {data.get('description', '—')}\n"
        f"Фото:          {photo_count} шт.\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Сохранить", callback_data="confirm:yes"),
        InlineKeyboardButton(text="Отменить",  callback_data="confirm:no"),
    ]])
    await message.answer(text, reply_markup=kb)
    await state.set_state(Form.confirm)


@router.callback_query(Form.confirm, F.data == "confirm:yes")
async def step_confirm_yes(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await cb.message.answer("Загружаю фото и сохраняю данные...")

    data = await state.get_data()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    user = cb.from_user
    user_name = user.full_name or user.username or str(user.id)
    inv = data.get("inv_number", "NO-NUM")
    photos: list = data.get("photos", [])

    # Загружаем фото в Б24 Диск.
    # Порядок ячеек в таблице: I=Наклейка, J=Шильдик, K=Общий вид.
    # - С шильдиком (3 фото): photo_links[0]=наклейка, [1]=шильдик, [2]=общий вид
    # - Без шильдика (2 фото): photo_links[0]=наклейка, [1]="", [2]=общий вид
    category = data.get("category", "")
    with_nameplate = has_nameplate(category)
    labels = photo_labels_for(category)

    photo_links = ["", "", ""]
    for i, fid in enumerate(photos[:len(labels)]):
        label = labels[i] if i < len(labels) else f"фото_{i+1}"
        try:
            filename = f"{inv}_{label}_{now.replace(':', '-')}.jpg"
            link = await upload_to_bitrix(bot, fid, filename)
            # Для категорий без шильдика 2-е фото — это «общий вид», а не «шильдик»
            if with_nameplate:
                photo_links[i] = link
            else:
                # [0]=наклейка → I, [1]=общий вид → K (шильдик пуст)
                photo_links[0 if i == 0 else 2] = link
        except Exception as e:
            log.error(f"Загрузка фото {label}: {e}")

    # Строка для листа "Оборудование"
    # A=Дата, B=Кто, C=Комплекс, D=Инв.номер, E=Категория, F=Состояние,
    # G=Описание, H=Расположение (пусто), I=Фото1, J=Фото2, K=Фото3,
    # L=Подкатегория, M=Тип ТМЦ, N=Модель
    row = [
        now,
        user_name,
        data.get("complex", ""),
        inv,
        category,
        data.get("condition", ""),
        data.get("description", ""),
        "",               # H: Расположение внутри объекта — не собираем
        photo_links[0],   # I: Фото наклейки
        photo_links[1],   # J: Фото шильдика (пусто для мебели/инвентаря)
        photo_links[2],   # K: Фото общего вида
        data.get("subcategory", ""),   # L: Подкатегория
        data.get("type_tmc", ""),      # M: Тип ТМЦ
        data.get("model", ""),         # N: Модель
    ]

    try:
        eq_sheet, _ = await asyncio.to_thread(_open_sheets)
        await asyncio.to_thread(eq_sheet.append_row, row)
        await cb.message.answer(
            f"✓ Оборудование {inv} сохранено в таблицу.\n\n"
            "/start — добавить следующее."
        )
    except Exception as e:
        log.error(f"Запись в Google Sheets: {e}")
        await cb.message.answer(
            f"Ошибка записи в таблицу: {e}\n"
            "Попробуйте /start заново."
        )

    await state.clear()
    await cb.answer()


@router.callback_query(Form.confirm, F.data == "confirm:no")
async def step_confirm_no(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("Запись отменена. /start — начать заново.")
    await cb.answer()


# ── /find: Поиск по инвентарному номеру ──────────────────────────────────────

@router.message(Command("find"))
async def cmd_find(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Введите инвентарный номер для поиска:")
    await state.set_state(FindForm.inv_number)


@router.message(FindForm.inv_number)
async def find_by_number(message: Message, state: FSMContext):
    inv = message.text.strip()
    await state.clear()

    if not SPREADSHEET_ID:
        await message.answer("Поиск недоступен: Google Sheets не настроен.")
        return

    try:
        eq_sheet = await asyncio.to_thread(_open_eq_sheet)
        all_rows = await asyncio.to_thread(eq_sheet.get_all_values)
    except Exception as e:
        log.error(f"Чтение таблицы: {e}")
        await message.answer(f"Ошибка при обращении к таблице: {e}")
        return

    # Пропускаем заголовок, ищем по столбцу D (индекс 3 — Инв.номер)
    matches = [
        r for r in all_rows[1:]
        if len(r) > 3 and r[3].strip().lower() == inv.lower()
    ]

    if not matches:
        await message.answer(
            f"Оборудование с номером {inv} не найдено.\n"
            "/find — новый поиск."
        )
        return

    for r in matches:
        def col(i: int) -> str:
            return r[i].strip() if i < len(r) and r[i].strip() else "—"

        text = (
            f"Найдено: {inv}\n\n"
            f"Комплекс:     {col(2)}\n"
            f"Категория:    {col(4)}\n"
            f"Подкатегория: {col(11)}\n"
            f"Тип ТМЦ:      {col(12)}\n"
            f"Модель:       {col(13)}\n"
            f"Состояние:    {col(5)}\n"
            f"Описание:     {col(6)}\n"
            f"Дата:         {col(0)}\n"
            f"Кто вносил:   {col(1)}\n"
        )
        links = [col(8), col(9), col(10)]
        links = [l for l in links if l != "—"]
        if links:
            text += "\nФото:\n" + "\n".join(f"  {l}" for l in links)
        await message.answer(text)


# ── /move: Перемещение оборудования ──────────────────────────────────────────

async def _get_current_location(inv: str) -> str | None:
    """Определяет текущий комплекс оборудования по инв. номеру.
    Учитывает журнал перемещений. Возвращает None если не найдено."""
    if not inv or inv == "—":
        return None

    def _check():
        eq_sheet, mv_sheet = _open_sheets()
        # Ищем в таблице оборудования (столбец D=инв.номер, C=комплекс)
        all_eq = eq_sheet.get_all_values()
        original_complex = None
        for row in all_eq[1:]:
            if len(row) > 3 and row[3].strip().lower() == inv.strip().lower():
                original_complex = row[2].strip()
                break
        if not original_complex:
            return None  # оборудование не зарегистрировано

        # Проверяем журнал перемещений — последнее перемещение
        all_mv = mv_sheet.get_all_values()
        last_to = None
        for row in all_mv[1:]:
            if len(row) > 4 and row[2].strip().lower() == inv.strip().lower():
                last_to = row[4].strip()  # столбец E = «Куда»
        return last_to or original_complex

    return await asyncio.to_thread(_check)


@router.message(Command("move"))
async def cmd_move(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Оформление перемещения.\n\nВведите инвентарный номер:")
    await state.set_state(MoveForm.inv_number)


@router.message(MoveForm.inv_number)
async def move_inv(message: Message, state: FSMContext):
    inv = message.text.strip()
    current = await _get_current_location(inv)
    if current is None:
        await message.answer(
            f"Оборудование с номером <b>{inv}</b> не найдено в таблице.\n\n"
            "Введите другой номер или /cancel для отмены.",
            parse_mode="HTML",
        )
        return  # остаёмся в MoveForm.inv_number

    await state.update_data(inv_number=inv, current_location=current)
    await message.answer(
        f"Оборудование найдено. Текущий комплекс: <b>{current}</b>\n\n"
        "Выберите, откуда перемещается:",
        parse_mode="HTML",
        reply_markup=inline_kb(SPORT_COMPLEXES, "mfrom"),
    )
    await state.set_state(MoveForm.from_complex)


@router.callback_query(MoveForm.from_complex, F.data.startswith("mfrom:"))
async def move_from(cb: CallbackQuery, state: FSMContext):
    idx = int(cb.data.split(":")[1])
    from_complex = SPORT_COMPLEXES[idx]
    data = await state.get_data()
    current = data.get("current_location", "")

    if current and current != from_complex:
        await cb.message.answer(
            f"Оборудование <b>{data.get('inv_number')}</b> числится "
            f"на комплексе «<b>{current}</b>», а не «{from_complex}».\n\n"
            "Выберите правильный комплекс:",
            parse_mode="HTML",
            reply_markup=inline_kb(SPORT_COMPLEXES, "mfrom"),
        )
        await cb.answer()
        return  # остаёмся в MoveForm.from_complex

    await state.update_data(from_complex=from_complex)
    await cb.message.answer(
        "Выберите, куда перемещается:",
        reply_markup=inline_kb(SPORT_COMPLEXES, "mto"),
    )
    await state.set_state(MoveForm.to_complex)
    await cb.answer()


@router.callback_query(MoveForm.to_complex, F.data.startswith("mto:"))
async def move_to(cb: CallbackQuery, state: FSMContext):
    idx = int(cb.data.split(":")[1])
    await state.update_data(to_complex=SPORT_COMPLEXES[idx])
    await cb.message.answer(
        "Причина перемещения:",
        reply_markup=inline_kb(MOVE_REASONS, "mreason"),
    )
    await state.set_state(MoveForm.reason)
    await cb.answer()


@router.callback_query(MoveForm.reason, F.data.startswith("mreason:"))
async def move_reason(cb: CallbackQuery, state: FSMContext):
    idx = int(cb.data.split(":")[1])
    await state.update_data(reason=MOVE_REASONS[idx])
    await cb.message.answer("Добавьте примечание или /skip:")
    await state.set_state(MoveForm.note)
    await cb.answer()


@router.message(MoveForm.note, Command("skip"))
async def move_note_skip(message: Message, state: FSMContext):
    await state.update_data(note="—")
    await _show_move_summary(message, state)


@router.message(MoveForm.note)
async def move_note(message: Message, state: FSMContext):
    await state.update_data(note=message.text.strip())
    await _show_move_summary(message, state)


async def _show_move_summary(message: Message, state: FSMContext):
    data = await state.get_data()
    text = (
        "Проверьте данные перемещения:\n\n"
        f"Инв. номер: {data.get('inv_number', '—')}\n"
        f"Откуда:     {data.get('from_complex', '—')}\n"
        f"Куда:       {data.get('to_complex', '—')}\n"
        f"Причина:    {data.get('reason', '—')}\n"
        f"Примечание: {data.get('note', '—')}\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Сохранить", callback_data="mconfirm:yes"),
        InlineKeyboardButton(text="Отменить",  callback_data="mconfirm:no"),
    ]])
    await message.answer(text, reply_markup=kb)
    await state.set_state(MoveForm.confirm)


@router.callback_query(MoveForm.confirm, F.data == "mconfirm:yes")
async def move_confirm_yes(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    user = cb.from_user
    user_name = user.full_name or user.username or str(user.id)
    inv = data.get("inv_number", "")

    # Строка для листа "Перемещения"
    row = [
        now,
        user_name,
        inv,
        data.get("from_complex", ""),
        data.get("to_complex", ""),
        data.get("reason", ""),
        data.get("note", ""),
    ]

    try:
        _, mv_sheet = await asyncio.to_thread(_open_sheets)
        await asyncio.to_thread(mv_sheet.append_row, row)
        await cb.message.answer(
            f"✓ Перемещение {inv} зафиксировано.\n\n"
            "/start — добавить оборудование\n"
            "/move — новое перемещение\n"
            "/find — поиск"
        )
    except Exception as e:
        log.error(f"Запись перемещения: {e}")
        await cb.message.answer(f"Ошибка записи: {e}")

    await state.clear()
    await cb.answer()


@router.callback_query(MoveForm.confirm, F.data == "mconfirm:no")
async def move_confirm_no(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("Перемещение отменено. /move — начать заново.")
    await cb.answer()


# ── /writeoff: Списание оборудования ─────────────────────────────────────────

@router.message(Command("writeoff"))
async def cmd_writeoff(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Оформление списания оборудования.\n\n"
        "Введите инвентарный номер:"
    )
    await state.set_state(WriteoffForm.inv_number)


@router.message(WriteoffForm.inv_number)
async def wo_inv(message: Message, state: FSMContext):
    inv = message.text.strip()

    # Ищем оборудование в таблице
    def _lookup():
        sheet = _open_eq_sheet()
        all_rows = sheet.get_all_values()
        for row in all_rows[1:]:
            if len(row) > 5 and row[3].strip().lower() == inv.lower():
                return {
                    "complex": row[2].strip(),
                    "category": row[4].strip() if len(row) > 4 else "",
                    "condition": row[5].strip() if len(row) > 5 else "",
                    "description": row[6].strip() if len(row) > 6 else "",
                }
        return None

    def _check_pending():
        wo_sheet = _open_writeoff_sheet()
        all_rows = wo_sheet.get_all_values()
        for row in all_rows[1:]:
            if len(row) > 2 and row[2].strip().lower() == inv.lower():
                status = row[9].strip() if len(row) > 9 else ""
                if status in ("Ожидает_Родимовой", "Ожидает_ГД"):
                    return True
        return False

    info = await asyncio.to_thread(_lookup)
    if info is None:
        await message.answer(
            f"Оборудование с номером <b>{inv}</b> не найдено.\n\n"
            "Введите другой номер или /cancel для отмены.",
            parse_mode="HTML",
        )
        return

    if info["condition"] == "Списано":
        await message.answer(
            f"Оборудование <b>{inv}</b> уже списано.\n\n"
            "Введите другой номер или /cancel для отмены.",
            parse_mode="HTML",
        )
        return

    has_pending = await asyncio.to_thread(_check_pending)
    if has_pending:
        await message.answer(
            f"Оборудование <b>{inv}</b> уже находится на стадии согласования списания.\n\n"
            "Введите другой номер или /cancel для отмены.",
            parse_mode="HTML",
        )
        return

    await state.update_data(
        inv_number=inv,
        eq_complex=info["complex"],
        eq_category=info["category"],
        eq_description=info["description"],
    )
    await message.answer(
        f"Найдено оборудование:\n\n"
        f"Инв. номер: <b>{inv}</b>\n"
        f"Комплекс:   {info['complex']}\n"
        f"Категория:  {info['category']}\n"
        f"Состояние:  {info['condition']}\n\n"
        "Укажите причину списания:",
        parse_mode="HTML",
        reply_markup=inline_kb(WRITEOFF_REASONS, "woreason"),
    )
    await state.set_state(WriteoffForm.reason)


@router.callback_query(WriteoffForm.reason, F.data.startswith("woreason:"))
async def wo_reason(cb: CallbackQuery, state: FSMContext):
    idx = int(cb.data.split(":")[1])
    await state.update_data(reason=WRITEOFF_REASONS[idx])
    await cb.message.answer(
        "Опишите ситуацию: что произошло, почему списывается.\n\n"
        "Можно текстом или голосовым сообщением.\n"
        "/skip — пропустить."
    )
    await state.set_state(WriteoffForm.description)
    await cb.answer()


@router.message(WriteoffForm.description, F.voice)
async def wo_desc_voice(message: Message, state: FSMContext):
    await message.answer("Распознаю речь...")
    text = await transcribe_voice(message.bot, message.voice.file_id)
    if text:
        await state.update_data(wo_description=f"[голос] {text}")
        await message.answer(f"Распознано: {text}")
    else:
        await state.update_data(wo_description="[голосовое сообщение]")
        await message.answer("Голосовое сохранено (текст не распознан).")
    await _ask_wo_photo(message, state)


@router.message(WriteoffForm.description, Command("skip"))
async def wo_desc_skip(message: Message, state: FSMContext):
    await state.update_data(wo_description="—")
    await _ask_wo_photo(message, state)


@router.message(WriteoffForm.description)
async def wo_desc_text(message: Message, state: FSMContext):
    await state.update_data(wo_description=message.text.strip())
    await _ask_wo_photo(message, state)


async def _ask_wo_photo(message: Message, state: FSMContext):
    await message.answer(
        "Сфотографируйте оборудование (обязательно).\n"
        "Отправьте фото."
    )
    await state.set_state(WriteoffForm.photo)


@router.message(WriteoffForm.photo, F.photo)
async def wo_photo(message: Message, state: FSMContext):
    file_id = message.photo[-1].file_id
    await state.update_data(wo_photo=file_id)

    # Пробуем автозаполнить "кто списывает" из регистрации
    user_data = await asyncio.to_thread(_get_user_by_tg_id, message.from_user.id)
    if user_data and user_data.get("name"):
        await state.update_data(who=user_data["name"])
        await message.answer(f"Фото получено. Ответственный: {user_data['name']}")
        await _show_wo_summary(message, state)
    else:
        await message.answer(
            "Фото получено.\n\n"
            "Введите ФИО ответственного (кто списывает):"
        )
        await state.set_state(WriteoffForm.who)


@router.message(WriteoffForm.photo)
async def wo_photo_invalid(message: Message, state: FSMContext):
    await message.answer("Отправьте фото оборудования (обязательно).")


@router.message(WriteoffForm.who)
async def wo_who(message: Message, state: FSMContext):
    await state.update_data(who=message.text.strip())
    await _show_wo_summary(message, state)


async def _show_wo_summary(message: Message, state: FSMContext):
    data = await state.get_data()
    text = (
        "Проверьте данные акта списания:\n\n"
        f"Инв. номер: {data.get('inv_number', '—')}\n"
        f"Комплекс:   {data.get('eq_complex', '—')}\n"
        f"Категория:  {data.get('eq_category', '—')}\n"
        f"Причина:    {data.get('reason', '—')}\n"
        f"Описание:   {data.get('wo_description', '—')}\n"
        f"Кто спис.:  {data.get('who', '—')}\n"
        f"Фото:       1 шт.\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Списать", callback_data="woconfirm:yes"),
        InlineKeyboardButton(text="Отменить", callback_data="woconfirm:no"),
    ]])
    await message.answer(text, reply_markup=kb)
    await state.set_state(WriteoffForm.confirm)


@router.callback_query(WriteoffForm.confirm, F.data == "woconfirm:yes")
async def wo_confirm_yes(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await cb.message.answer("Загружаю фото и создаю акт списания...")

    data = await state.get_data()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    inv = data.get("inv_number", "")
    initiator_tg_id = cb.from_user.id

    # Загрузка фото в Битрикс24
    photo_link = ""
    photo_fid = data.get("wo_photo")
    if photo_fid:
        try:
            filename = f"writeoff_{inv}_{now.replace(':', '-')}.jpg"
            photo_link = await upload_to_bitrix(bot, photo_fid, filename)
        except Exception as e:
            log.error(f"Загрузка фото списания: {e}")

    # Запись в лист "Акты списания" со статусом "Ожидает_Родимовой"
    def _save_pending():
        wo_sheet = _open_writeoff_sheet()
        all_vals = wo_sheet.get_all_values()
        next_num = len(all_vals)  # строка 1 = заголовок
        act_number = f"АКТ-{next_num:03d}"
        wo_sheet.append_row([
            now,
            act_number,
            inv,
            data.get("eq_complex", ""),
            data.get("eq_category", ""),
            data.get("reason", ""),
            data.get("wo_description", ""),
            data.get("who", ""),
            photo_link,
            "Ожидает_Родимовой",   # J: Статус
            "",                     # K: Дата подтв.1
            "",                     # L: Дата подтв.2
            "",                     # M: Комм.отклонения
            str(initiator_tg_id),   # N: TG ID инициатора
        ])
        return act_number

    try:
        act_number = await asyncio.to_thread(_save_pending)
    except Exception as e:
        log.error(f"Запись акта списания: {e}")
        await cb.message.answer(f"Ошибка записи: {e}")
        await state.clear()
        await cb.answer()
        return

    # Уведомляем первого утверждающего (Родимова)
    await _notify_approver(
        bot=bot,
        approver_tg_id=APPROVER_1_TG_ID,
        approver_b24_id=APPROVER_1_B24_ID,
        act_number=act_number,
        inv=inv,
        data=data,
        photo_link=photo_link,
        now=now,
    )

    await cb.message.answer(
        f"✓ Акт <b>{act_number}</b> создан.\n"
        f"Ожидает подтверждения: {APPROVER_1_NAME}\n\n"
        "Вы получите уведомление о решении.",
        parse_mode="HTML",
    )
    await state.clear()
    await cb.answer()


@router.callback_query(WriteoffForm.confirm, F.data == "woconfirm:no")
async def wo_confirm_no(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("Списание отменено. /writeoff — начать заново.")
    await cb.answer()


# ── Согласование актов списания ───────────────────────────────────────────────

@router.callback_query(F.data.startswith("approve:"))
async def approve_callback(cb: CallbackQuery, state: FSMContext, bot: Bot):
    """Обработка решения утверждающего: подтвердить / отклонить."""
    parts = cb.data.split(":", 2)
    if len(parts) < 3:
        await cb.answer("Неверный формат", show_alert=True)
        return

    decision = parts[1]   # "yes" / "no"
    act_number = parts[2]
    approver_tg_id = cb.from_user.id

    if decision == "no":
        await state.update_data(
            reject_act=act_number,
            reject_approver_tg_id=approver_tg_id,
        )
        await state.set_state(RejectForm.comment)
        await cb.message.answer(
            f"Отклонение акта {act_number}.\n\nУкажите причину отклонения:"
        )
        await cb.answer()
        return

    # ── Подтверждение ──────────────────────────────────────────────────────
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    def _update_approved():
        wo_sheet = _open_writeoff_sheet()
        all_vals = wo_sheet.get_all_values()
        for i, row in enumerate(all_vals[1:], start=2):
            if len(row) > 1 and row[1].strip() == act_number:
                inv = row[2].strip() if len(row) > 2 else ""
                status = row[9].strip() if len(row) > 9 else ""
                initiator_tg_id = row[13].strip() if len(row) > 13 else ""

                if approver_tg_id == APPROVER_1_TG_ID and status == "Ожидает_Родимовой":
                    wo_sheet.update_cell(i, 10, "Ожидает_ГД")
                    wo_sheet.update_cell(i, 11, now)
                    return "need_approver2", inv, initiator_tg_id, row

                if approver_tg_id == APPROVER_2_TG_ID and status == "Ожидает_ГД":
                    wo_sheet.update_cell(i, 10, "Утверждён")
                    wo_sheet.update_cell(i, 12, now)
                    return "approved", inv, initiator_tg_id, row

                return "wrong_status", inv, initiator_tg_id, row

        return "not_found", "", "", []

    try:
        result, inv, initiator_tg_id, act_row = await asyncio.to_thread(_update_approved)
    except Exception as e:
        log.error(f"Обновление статуса акта {act_number}: {e}")
        await cb.message.answer(f"Ошибка: {e}")
        await cb.answer()
        return

    if result == "not_found":
        await cb.message.answer(f"Акт {act_number} не найден.")
        await cb.answer()
        return

    if result == "wrong_status":
        await cb.message.answer(
            f"Акт {act_number} уже обработан или не в вашей очереди согласования."
        )
        await cb.answer()
        return

    if result == "need_approver2":
        # Передаём на второго утверждающего (ГД Пруцков)
        act_data = {
            "eq_complex":    act_row[3] if len(act_row) > 3 else "",
            "eq_category":   act_row[4] if len(act_row) > 4 else "",
            "reason":        act_row[5] if len(act_row) > 5 else "",
            "wo_description": act_row[6] if len(act_row) > 6 else "",
            "who":           act_row[7] if len(act_row) > 7 else "",
        }
        photo_link = act_row[8] if len(act_row) > 8 else ""
        await _notify_approver(
            bot=bot,
            approver_tg_id=APPROVER_2_TG_ID,
            approver_b24_id=APPROVER_2_B24_ID,
            act_number=act_number,
            inv=inv,
            data=act_data,
            photo_link=photo_link,
            now=now,
        )
        await cb.message.answer(
            f"✅ Акт {act_number} подтверждён. Направлен на согласование {APPROVER_2_NAME}."
        )
        if initiator_tg_id:
            try:
                await bot.send_message(
                    int(initiator_tg_id),
                    f"Акт {act_number} подтверждён {APPROVER_1_NAME}.\n"
                    f"Передан на согласование {APPROVER_2_NAME}.",
                )
            except Exception as e:
                log.warning(f"Уведомление инициатору: {e}")

    elif result == "approved":
        # Финальное подтверждение — ставим "Списано" в листе "Оборудование"
        def _finalize():
            eq_sheet = _open_eq_sheet()
            all_eq = eq_sheet.get_all_values()
            for i, row in enumerate(all_eq[1:], start=2):
                if len(row) > 3 and row[3].strip().lower() == inv.strip().lower():
                    eq_sheet.update_cell(i, 6, "Списано")
                    break

        try:
            await asyncio.to_thread(_finalize)
        except Exception as e:
            log.error(f"Финальное списание {inv}: {e}")

        await cb.message.answer(
            f"✅ Акт {act_number} окончательно утверждён.\n"
            f"Оборудование {inv} — статус «Списано»."
        )
        if initiator_tg_id:
            try:
                await bot.send_message(
                    int(initiator_tg_id),
                    f"✅ Акт {act_number} утверждён {APPROVER_2_NAME}.\n"
                    f"Оборудование {inv} — статус «Списано».",
                )
            except Exception as e:
                log.warning(f"Уведомление инициатору: {e}")

    await cb.answer()


@router.message(RejectForm.comment)
async def reject_comment(message: Message, state: FSMContext, bot: Bot):
    """Сохраняет комментарий отклонения и уведомляет инициатора."""
    comment = message.text.strip()
    data = await state.get_data()
    act_number = data.get("reject_act", "")
    approver_tg_id = data.get("reject_approver_tg_id", 0)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    def _update_rejected():
        wo_sheet = _open_writeoff_sheet()
        all_vals = wo_sheet.get_all_values()
        for i, row in enumerate(all_vals[1:], start=2):
            if len(row) > 1 and row[1].strip() == act_number:
                inv = row[2].strip() if len(row) > 2 else ""
                initiator_tg_id = row[13].strip() if len(row) > 13 else ""
                wo_sheet.update_cell(i, 10, "Отклонён")
                wo_sheet.update_cell(i, 13, comment)
                wo_sheet.update_cell(i, 11, now)  # дата отклонения в K
                return inv, initiator_tg_id
        return "", ""

    try:
        inv, initiator_tg_id = await asyncio.to_thread(_update_rejected)
    except Exception as e:
        log.error(f"Отклонение акта {act_number}: {e}")
        await message.answer(f"Ошибка: {e}")
        await state.clear()
        return

    await message.answer(f"❌ Акт {act_number} отклонён.")

    if initiator_tg_id:
        approver_name = (
            APPROVER_1_NAME if approver_tg_id == APPROVER_1_TG_ID else APPROVER_2_NAME
        )
        try:
            await bot.send_message(
                int(initiator_tg_id),
                f"❌ Акт {act_number} ({inv}) отклонён.\n"
                f"Кем: {approver_name}\n"
                f"Причина: {comment}",
            )
        except Exception as e:
            log.warning(f"Уведомление инициатору об отклонении: {e}")

    await state.clear()


# ── Fallback ──────────────────────────────────────────────────────────────────

@router.message()
async def fallback(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer("/start — добавить оборудование.")
    else:
        await message.answer("Следуйте инструкциям выше или /cancel для отмены.")


# ─── Запуск ───────────────────────────────────────────────────────────────────

async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    commands = [
        BotCommand(command="start",     description="Добавить оборудование"),
        BotCommand(command="move",      description="Оформить перемещение"),
        BotCommand(command="writeoff",  description="Списать оборудование"),
        BotCommand(command="find",      description="Найти по инвентарному номеру"),
        BotCommand(command="dashboard", description="Открыть дашборд"),
        BotCommand(command="guide",     description="Инструкция по работе"),
        BotCommand(command="cancel",    description="Отменить текущее действие"),
    ]
    await bot.set_my_commands(commands)

    log.info("Бот запущен.")
    try:
        await dp.start_polling(bot)
    finally:
        if bx:
            await bx.close()


if __name__ == "__main__":
    asyncio.run(main())
