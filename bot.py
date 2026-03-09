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
                                          # K=Фото общего вида, L=Голосовое (пусто)
SHEET_MOVEMENTS  = "Журнал перемещений"  # A=Дата, B=Кто, C=Инв.номер,
                                          # D=Откуда, E=Куда, F=Причина, G=Примечание

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

# Справочники
SPORT_COMPLEXES = [
    "Остров", "Маяк", "Юрловский", "Косино", "Арктика", "Янтарь",
]

CATEGORIES = [
    "Климатическое (чиллеры, пушки, кондиционеры)",
    "Строительное (фены, перфораторы, болгарки)",
    "Уборочное (пылесосы, мойки, поломоечные)",
    "Электрика (генераторы, удлинители, щитки)",
    "Спортивное оборудование",
    "Мебель и инвентарь",
    "Другое",
]

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

PHOTOS_NEEDED = 3  # наклейка, шильдик, общий вид

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


# ─── FSM ──────────────────────────────────────────────────────────────────────

class Form(StatesGroup):
    choose_complex   = State()   # 1. Выбор комплекса
    photos           = State()   # 2. Сбор фото (ждём до 3 штук)
    inv_number       = State()   # 3. Инвентарный номер
    choose_category  = State()   # 4. Категория
    choose_condition = State()   # 5. Состояние
    description      = State()   # 6. Описание
    confirm          = State()   # Подтверждение


class FindForm(StatesGroup):
    inv_number = State()


class MoveForm(StatesGroup):
    inv_number   = State()
    from_complex = State()
    to_complex   = State()
    reason       = State()
    note         = State()
    confirm      = State()


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


# ─── Роутер ───────────────────────────────────────────────────────────────────

router = Router()


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    kb_rows = [inline_kb(SPORT_COMPLEXES, "complex").inline_keyboard]
    if DASHBOARD_URL:
        kb_rows.append([InlineKeyboardButton(text="📊 Открыть дашборд", url=DASHBOARD_URL)])
    await message.answer(
        "Учёт ТМЦ — инвентаризация оборудования\n\n"
        "Команды:\n"
        "  /start      — добавить оборудование\n"
        "  /move       — оформить перемещение\n"
        "  /find       — найти по номеру\n"
        "  /dashboard  — открыть дашборд\n"
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


@router.message(Command("dashboard"))
async def cmd_dashboard(message: Message):
    if not DASHBOARD_URL:
        await message.answer("Дашборд пока не настроен (DASHBOARD_URL не задан).")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📊 Открыть дашборд", url=DASHBOARD_URL),
    ]])
    await message.answer("Панель управления ТМЦ:", reply_markup=kb)


# ── Шаг 1: Комплекс ───────────────────────────────────────────────────────────

@router.callback_query(Form.choose_complex, F.data.startswith("complex:"))
async def step_complex(cb: CallbackQuery, state: FSMContext):
    idx = int(cb.data.split(":")[1])
    complex_name = SPORT_COMPLEXES[idx]
    await state.update_data(complex=complex_name, photos=[], ocr_done=False)
    await cb.message.answer(
        f"Комплекс: {complex_name}\n\n"
        "Сфотографируйте оборудование — нужно 3 фото:\n"
        "  1. Наклейка / QR-код\n"
        "  2. Шильдик (заводская табличка)\n"
        "  3. Общий вид\n\n"
        "Отправляйте фото по одному. Когда всё готово — /done\n"
        "Или /skip чтобы пропустить фото."
    )
    await state.set_state(Form.photos)
    await cb.answer()


# ── Шаг 2: Фото ───────────────────────────────────────────────────────────────

PHOTO_LABELS = ["наклейка", "шильдик", "общий_вид"]


@router.message(Form.photos, F.photo)
async def step_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    photos: list = data.get("photos", [])
    ocr_done: bool = data.get("ocr_done", False)

    file_id = message.photo[-1].file_id
    photos.append(file_id)
    count = len(photos)

    # OCR на первом фото (наклейка)
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

    await state.update_data(photos=photos, ocr_done=ocr_done or bool(inv_from_ocr))

    if count < PHOTOS_NEEDED:
        remaining = PHOTOS_NEEDED - count
        status = f"Фото {count} из {PHOTOS_NEEDED} получено."
        if inv_from_ocr:
            status += f" Номер считан: {inv_from_ocr}"
        await message.answer(
            f"{status}\n"
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
    if inv:
        await message.answer(
            f"Инвентарный номер считан автоматически: {inv}\n\n"
            "Введите другой номер, если неверно, или /skip чтобы оставить."
        )
    else:
        await message.answer(
            "Введите инвентарный номер (например: INV-001, А-123).\n"
            "/skip — если номер не присвоен."
        )
    await state.set_state(Form.inv_number)


# ── Шаг 3: Инвентарный номер ──────────────────────────────────────────────────

async def _inv_exists(inv: str) -> bool:
    """True если инвентарный номер уже есть в таблице оборудования."""
    if not inv or inv in ("—", ""):
        return False
    def _check():
        sheet = _open_eq_sheet()
        values = sheet.col_values(4)  # col D (инв. номер), 1-based
        return inv.strip().lower() in (v.strip().lower() for v in values[1:])
    return await asyncio.to_thread(_check)


@router.message(Form.inv_number, Command("skip"))
async def step_inv_skip(message: Message, state: FSMContext):
    data = await state.get_data()
    inv = data.get("inv_number")
    if not inv:
        await state.update_data(inv_number="—")
    else:
        # Пользователь подтверждает номер с OCR через /skip — проверяем дубликат
        if await _inv_exists(inv):
            await state.update_data(inv_number=None, ocr_done=False)
            await message.answer(
                f"⚠️ Номер <b>{inv}</b> уже зарегистрирован в таблице.\n\n"
                "Введите другой номер вручную или /skip чтобы добавить без номера.",
                parse_mode="HTML",
            )
            return
    await _ask_category(message, state)


@router.message(Form.inv_number)
async def step_inv_number(message: Message, state: FSMContext):
    inv = message.text.strip()
    if await _inv_exists(inv):
        await message.answer(
            f"⚠️ Номер <b>{inv}</b> уже зарегистрирован в таблице.\n\n"
            "Введите другой номер или /skip чтобы добавить без номера.",
            parse_mode="HTML",
        )
        return  # остаёмся в состоянии Form.inv_number
    await state.update_data(inv_number=inv)
    await _ask_category(message, state)


async def _ask_category(message: Message, state: FSMContext):
    await message.answer(
        "Выберите категорию оборудования:",
        reply_markup=inline_kb(CATEGORIES, "cat"),
    )
    await state.set_state(Form.choose_category)


# ── Шаг 4: Категория ──────────────────────────────────────────────────────────

@router.callback_query(Form.choose_category, F.data.startswith("cat:"))
async def step_category(cb: CallbackQuery, state: FSMContext):
    idx = int(cb.data.split(":")[1])
    await state.update_data(category=CATEGORIES[idx])
    await cb.message.answer(
        "Укажите состояние оборудования:",
        reply_markup=inline_kb(CONDITIONS, "cond"),
    )
    await state.set_state(Form.choose_condition)
    await cb.answer()


# ── Шаг 5: Состояние ──────────────────────────────────────────────────────────

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


# ── Шаг 6: Описание ───────────────────────────────────────────────────────────

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
        f"Комплекс:   {data.get('complex', '—')}\n"
        f"Инв. номер: {data.get('inv_number', '—')}\n"
        f"Категория:  {data.get('category', '—')}\n"
        f"Состояние:  {data.get('condition', '—')}\n"
        f"Описание:   {data.get('description', '—')}\n"
        f"Фото:       {photo_count} шт.\n"
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

    # Загружаем фото в Б24 Диск, собираем ссылки
    photo_links = ["", "", ""]
    labels = PHOTO_LABELS + [f"фото_{i+1}" for i in range(max(0, len(photos) - len(PHOTO_LABELS)))]
    for i, fid in enumerate(photos[:3]):
        label = labels[i] if i < len(labels) else f"фото_{i+1}"
        try:
            filename = f"{inv}_{label}_{now.replace(':', '-')}.jpg"
            link = await upload_to_bitrix(bot, fid, filename)
            photo_links[i] = link
        except Exception as e:
            log.error(f"Загрузка фото {label}: {e}")

    # Строка для листа "Оборудование" (совместима со старой структурой таблицы)
    # A=Дата, B=Кто вносил, C=Комплекс, D=Инв.номер, E=Категория, F=Состояние,
    # G=Описание, H=Расположение (пусто), I=Фото наклейки, J=Шильдик, K=Общий вид
    row = [
        now,
        user_name,
        data.get("complex", ""),
        inv,
        data.get("category", ""),
        data.get("condition", ""),
        data.get("description", ""),
        "",               # H: Расположение внутри объекта — не собираем
        photo_links[0],   # I: Фото наклейки
        photo_links[1],   # J: Фото шильдика
        photo_links[2],   # K: Фото общего вида
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
        # A=0 Дата, B=1 Кто, C=2 Комплекс, D=3 Инв.номер, E=4 Категория,
        # F=5 Состояние, G=6 Описание, H=7 Расположение,
        # I=8 Фото наклейки, J=9 Фото шильдика, K=10 Фото общего вида
        def col(i: int) -> str:
            return r[i].strip() if i < len(r) and r[i].strip() else "—"

        text = (
            f"Найдено: {inv}\n\n"
            f"Комплекс:   {col(2)}\n"
            f"Категория:  {col(4)}\n"
            f"Состояние:  {col(5)}\n"
            f"Описание:   {col(6)}\n"
            f"Дата:       {col(0)}\n"
            f"Кто вносил: {col(1)}\n"
        )
        links = [col(8), col(9), col(10)]
        links = [l for l in links if l != "—"]
        if links:
            text += "\nФото:\n" + "\n".join(f"  {l}" for l in links)
        await message.answer(text)


# ── /move: Перемещение оборудования ──────────────────────────────────────────

@router.message(Command("move"))
async def cmd_move(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Оформление перемещения.\n\nВведите инвентарный номер:")
    await state.set_state(MoveForm.inv_number)


@router.message(MoveForm.inv_number)
async def move_inv(message: Message, state: FSMContext):
    await state.update_data(inv_number=message.text.strip())
    await message.answer(
        "Выберите, откуда перемещается:",
        reply_markup=inline_kb(SPORT_COMPLEXES, "mfrom"),
    )
    await state.set_state(MoveForm.from_complex)


@router.callback_query(MoveForm.from_complex, F.data.startswith("mfrom:"))
async def move_from(cb: CallbackQuery, state: FSMContext):
    idx = int(cb.data.split(":")[1])
    await state.update_data(from_complex=SPORT_COMPLEXES[idx])
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
        BotCommand(command="find",      description="Найти по инвентарному номеру"),
        BotCommand(command="dashboard", description="Открыть дашборд"),
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
