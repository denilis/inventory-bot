"""
Telegram-бот для инвентаризации оборудования спортивных комплексов.
MVP: пошаговый сбор данных → Google Sheets + Google Drive.
"""

import os
import io
import logging
from datetime import datetime
from enum import Enum, auto

from dotenv import load_dotenv
load_dotenv()  # читает .env из текущей папки

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

import json
import base64
from PIL import Image
from pyzbar import pyzbar as pyzbar_lib
from openai import AsyncOpenAI

# ─── Настройки ────────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")

# Путь к JSON-ключу сервисного аккаунта Google
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")

# ID Google-таблицы (из URL: https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit)
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "YOUR_SPREADSHEET_ID")

# ID папки на Google Drive для фото
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID", "YOUR_DRIVE_FOLDER_ID")

# OpenAI (Whisper + GPT-4 Vision)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# Названия спортивных комплексов
SPORT_COMPLEXES = [
    "Остров",
    "Маяк",
    "Юрловский",
    "Косино",
    "Арктика",
    "Янтарь",
]

# Категории оборудования
CATEGORIES = [
    "Климатическое (чиллеры, пушки, кондиционеры)",
    "Строительное (фены, перфораторы, болгарки)",
    "Уборочное (пылесосы, мойки, поломоечные)",
    "Электрика (генераторы, удлинители, щитки)",
    "Спортивное оборудование",
    "Мебель и инвентарь",
    "Другое",
]

# Варианты состояния
CONDITIONS = [
    "✅ Отличное — работает без замечаний",
    "🟡 Рабочее — есть мелкие замечания",
    "🟠 Требует ремонта",
    "🔴 Не работает / списать",
    "❓ Не проверялось",
]

# Причины перемещения (для /move)
MOVE_REASONS = [
    "Перевод на другой объект",
    "Временная аренда",
    "На ремонт",
    "После ремонта",
    "Другое",
]

# ─── Логирование ──────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─── Состояния FSM ────────────────────────────────────────────────────────────

class Form(StatesGroup):
    choose_complex = State()
    photo_sticker = State()
    inv_number = State()
    photo_nameplate = State()
    photo_general = State()
    choose_category = State()
    choose_condition = State()
    description = State()
    location_detail = State()
    confirm = State()


class FindForm(StatesGroup):
    inv_number = State()


class MoveForm(StatesGroup):
    inv_number = State()
    from_complex = State()
    to_complex = State()
    reason = State()
    note = State()
    confirm = State()


# ─── Google API ───────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_google_creds = None
_gsheet_client = None
_drive_service = None


def get_google_creds():
    global _google_creds
    if _google_creds is None:
        creds_json_b64 = os.getenv("GOOGLE_CREDENTIALS_JSON")
        if creds_json_b64:
            info = json.loads(base64.b64decode(creds_json_b64))
            _google_creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        else:
            _google_creds = Credentials.from_service_account_file(
                GOOGLE_CREDENTIALS_FILE, scopes=SCOPES
            )
    return _google_creds


def get_sheet():
    global _gsheet_client
    if _gsheet_client is None:
        _gsheet_client = gspread.authorize(get_google_creds())
    return _gsheet_client.open_by_key(SPREADSHEET_ID)


def get_drive():
    global _drive_service
    if _drive_service is None:
        _drive_service = build("drive", "v3", credentials=get_google_creds())
    return _drive_service


async def upload_photo_to_drive(bot: Bot, file_id: str, filename: str, mimetype: str = "image/jpeg") -> str:
    """Скачивает файл из Telegram и загружает в Google Drive. Возвращает публичную ссылку."""
    tg_file = await bot.get_file(file_id)
    raw = await bot.download_file(tg_file.file_path)   # aiogram 3 возвращает BytesIO, уже на позиции 0

    drive = get_drive()
    file_metadata = {
        "name": filename,
        "parents": [DRIVE_FOLDER_ID],
    }
    media = MediaIoBaseUpload(raw, mimetype=mimetype, resumable=False)
    uploaded = drive.files().create(
        body=file_metadata, media_body=media, fields="id, webViewLink"
    ).execute()

    # Открываем доступ по ссылке
    drive.permissions().create(
        fileId=uploaded["id"],
        body={"type": "anyone", "role": "reader"},
    ).execute()

    return uploaded.get("webViewLink", f"https://drive.google.com/file/d/{uploaded['id']}/view")


def append_to_sheet(row_data: list):
    """Добавляет строку в первый лист таблицы."""
    sheet = get_sheet()
    worksheet = sheet.sheet1
    worksheet.append_row(row_data, value_input_option="USER_ENTERED")


def find_in_sheet(inv_number: str) -> list:
    """Ищет записи по инвентарному номеру. Возвращает список совпадений."""
    sheet = get_sheet()
    worksheet = sheet.sheet1
    records = worksheet.get_all_records()
    query = inv_number.strip().lower()
    return [r for r in records if str(r.get("Инвентарный номер", "")).strip().lower() == query]


def append_to_movement_log(row_data: list):
    """Добавляет запись в лист «Журнал перемещений»."""
    sheet = get_sheet()
    try:
        worksheet = sheet.worksheet("Журнал перемещений")
    except gspread.exceptions.WorksheetNotFound:
        worksheet = sheet.add_worksheet(title="Журнал перемещений", rows=1000, cols=10)
    worksheet.append_row(row_data, value_input_option="USER_ENTERED")


def decode_qr_barcode(image_bytes: bytes) -> str | None:
    """Читает QR-код или штрихкод с изображения через pyzbar. Возвращает строку или None."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        results = pyzbar_lib.decode(img)
        if results:
            return results[0].data.decode("utf-8", errors="replace").strip()
    except Exception as e:
        log.warning(f"QR/barcode decode: {e}")
    return None


async def ocr_sticker_with_gpt(image_bytes: bytes) -> str | None:
    """Использует GPT-4 Vision для распознавания инвентарного номера с наклейки."""
    if not openai_client:
        return None
    try:
        b64 = base64.b64encode(image_bytes).decode()
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "На фото наклейка с инвентарным номером оборудования. "
                            "Найди и верни ТОЛЬКО инвентарный номер (например: INV-001, А-123, 0045). "
                            "Если номер не виден — ответь словом НЕТ."
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
        result = response.choices[0].message.content.strip()
        if result.upper().startswith("НЕТ") or not result:
            return None
        return result
    except Exception as e:
        log.error(f"GPT Vision OCR error: {e}")
    return None


async def transcribe_voice_ogg(bot: Bot, file_id: str) -> str:
    """Скачивает голосовое OGG-сообщение и транскрибирует через OpenAI Whisper."""
    if not openai_client:
        return ""
    try:
        tg_file = await bot.get_file(file_id)
        raw = await bot.download_file(tg_file.file_path)   # BytesIO
        raw.name = "voice.ogg"   # Whisper требует имя файла
        transcript = await openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=raw,
            language="ru",
        )
        return transcript.text.strip()
    except Exception as e:
        log.error(f"Whisper transcription error: {e}")
    return ""


# ─── Хелперы клавиатур ───────────────────────────────────────────────────────

def make_inline_kb(items: list[str], prefix: str) -> InlineKeyboardMarkup:
    """Создаёт inline-клавиатуру из списка строк."""
    buttons = []
    for i, item in enumerate(items):
        # Ограничение Telegram: callback_data до 64 байт
        buttons.append([InlineKeyboardButton(text=item, callback_data=f"{prefix}:{i}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def make_reply_kb(items: list[str]) -> ReplyKeyboardMarkup:
    """Создаёт reply-клавиатуру из списка строк."""
    buttons = [[KeyboardButton(text=item)] for item in items]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, one_time_keyboard=True)


# ─── Роутер и хэндлеры ───────────────────────────────────────────────────────

router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 Привет! Я бот для учёта ТМЦ спортивных комплексов.\n\n"
        "📋 *Добавить оборудование* — просто отправьте /start и следуйте шагам.\n"
        "📦 *Оформить перемещение* — /move\n"
        "🔍 *Найти по номеру* — /find\n"
        "❌ *Отменить текущее действие* — /cancel\n\n"
        "Начнём! Выберите спортивный комплекс:",
        parse_mode="Markdown",
        reply_markup=make_inline_kb(SPORT_COMPLEXES, "complex"),
    )
    await state.set_state(Form.choose_complex)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "❌ Текущая запись отменена. Нажмите /start чтобы начать заново.",
        reply_markup=ReplyKeyboardRemove(),
    )


# ── Шаг 1: Выбор комплекса ──────────────────────────────────────────────────

@router.callback_query(Form.choose_complex, F.data.startswith("complex:"))
async def step_complex(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":")[1])
    complex_name = SPORT_COMPLEXES[idx]
    await state.update_data(complex=complex_name)
    await callback.message.answer(
        f"🏟 Комплекс: *{complex_name}*\n\n"
        "📸 Теперь сфотографируйте **наклейку / QR-код** на оборудовании.\n"
        "Если наклейки ещё нет — отправьте /skip",
        parse_mode="Markdown",
    )
    await state.set_state(Form.photo_sticker)
    await callback.answer()


# ── Шаг 2: Фото наклейки ────────────────────────────────────────────────────

@router.message(Form.photo_sticker, F.photo)
async def step_photo_sticker(message: Message, state: FSMContext):
    file_id = message.photo[-1].file_id  # наибольшее разрешение
    await state.update_data(photo_sticker_id=file_id)

    # Пробуем считать номер автоматически
    await message.answer("🔍 Читаю наклейку...")
    number = None
    image_bytes = None
    try:
        tg_file = await message.bot.get_file(file_id)
        raw = await message.bot.download_file(tg_file.file_path)
        image_bytes = raw.read()
        number = decode_qr_barcode(image_bytes)   # сначала QR/штрихкод (быстро, бесплатно)
    except Exception as e:
        log.warning(f"Не удалось скачать фото: {e}")

    if not number and image_bytes:
        # QR не нашёл — пробуем GPT-4 Vision
        number = await ocr_sticker_with_gpt(image_bytes)

    if number:
        await state.update_data(inv_number=number)
        await message.answer(
            f"✅ Считан номер: *{number}*",
            parse_mode="Markdown",
        )
        await _ask_nameplate(message, state)
        return

    # Ничего не считалось — вводим вручную
    await message.answer(
        "🔢 Номер не считался автоматически.\n\n"
        "Введите **инвентарный номер** с наклейки (например, INV-001).\n"
        "Или /skip если нет номера.",
        parse_mode="Markdown",
    )
    await state.set_state(Form.inv_number)


@router.message(Form.photo_sticker, Command("skip"))
async def step_photo_sticker_skip(message: Message, state: FSMContext):
    await state.update_data(photo_sticker_id=None)
    await message.answer(
        "⏩ Наклейки нет — пропускаем.\n\n"
        "🔢 Введите **инвентарный номер** вручную (например, INV-001).\n"
        "Или /skip если пока не присвоен.",
        parse_mode="Markdown",
    )
    await state.set_state(Form.inv_number)


# ── Шаг 3: Инвентарный номер ────────────────────────────────────────────────

@router.message(Form.inv_number, Command("skip"))
async def step_inv_skip(message: Message, state: FSMContext):
    await state.update_data(inv_number="—")
    await _ask_nameplate(message, state)


@router.message(Form.inv_number)
async def step_inv_number(message: Message, state: FSMContext):
    await state.update_data(inv_number=message.text.strip())
    await _ask_nameplate(message, state)


async def _ask_nameplate(message: Message, state: FSMContext):
    await message.answer(
        "📸 Теперь сфотографируйте **шильдик** (заводская табличка с моделью, серийным номером).\n"
        "Если шильдика нет — /skip",
        parse_mode="Markdown",
    )
    await state.set_state(Form.photo_nameplate)


# ── Шаг 4: Фото шильдика ────────────────────────────────────────────────────

@router.message(Form.photo_nameplate, F.photo)
async def step_photo_nameplate(message: Message, state: FSMContext):
    file_id = message.photo[-1].file_id
    await state.update_data(photo_nameplate_id=file_id)
    await _ask_general_photo(message, state)


@router.message(Form.photo_nameplate, Command("skip"))
async def step_photo_nameplate_skip(message: Message, state: FSMContext):
    await state.update_data(photo_nameplate_id=None)
    await _ask_general_photo(message, state)


async def _ask_general_photo(message: Message, state: FSMContext):
    await message.answer(
        "📸 Сфотографируйте **оборудование целиком** (общий вид).",
        parse_mode="Markdown",
    )
    await state.set_state(Form.photo_general)


# ── Шаг 5: Фото общего вида ─────────────────────────────────────────────────

@router.message(Form.photo_general, F.photo)
async def step_photo_general(message: Message, state: FSMContext):
    file_id = message.photo[-1].file_id
    await state.update_data(photo_general_id=file_id)
    await message.answer(
        "📂 Выберите **категорию** оборудования:",
        parse_mode="Markdown",
        reply_markup=make_inline_kb(CATEGORIES, "cat"),
    )
    await state.set_state(Form.choose_category)


# ── Шаг 6: Категория ────────────────────────────────────────────────────────

@router.callback_query(Form.choose_category, F.data.startswith("cat:"))
async def step_category(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":")[1])
    await state.update_data(category=CATEGORIES[idx])
    await callback.message.answer(
        "🔧 Выберите **состояние** оборудования:",
        parse_mode="Markdown",
        reply_markup=make_inline_kb(CONDITIONS, "cond"),
    )
    await state.set_state(Form.choose_condition)
    await callback.answer()


# ── Шаг 7: Состояние ────────────────────────────────────────────────────────

@router.callback_query(Form.choose_condition, F.data.startswith("cond:"))
async def step_condition(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":")[1])
    await state.update_data(condition=CONDITIONS[idx])
    await callback.message.answer(
        "💬 Опишите оборудование: **что это, для чего используется, замечания.**\n\n"
        "Можно:\n"
        "• Написать текстом\n"
        "• Отправить голосовое сообщение 🎤\n"
        "• /skip если нечего добавить",
        parse_mode="Markdown",
    )
    await state.set_state(Form.description)
    await callback.answer()


# ── Шаг 8: Описание (текст или голос) ───────────────────────────────────────

@router.message(Form.description, F.voice)
async def step_description_voice(message: Message, state: FSMContext):
    voice_id = message.voice.file_id
    await state.update_data(voice_id=voice_id)

    await message.answer("🎤 Распознаю речь...")
    text = await transcribe_voice_ogg(message.bot, voice_id)

    if text:
        await state.update_data(description=f"[Голос]: {text}")
        await message.answer(
            f"✅ Распознано:\n_{text}_",
            parse_mode="Markdown",
        )
    else:
        await state.update_data(description="[голосовое сообщение]")
        if openai_client:
            await message.answer("⚠️ Речь не распознана. Голосовое сохранено как файл.")
        else:
            await message.answer("🎤 Голосовое сохранено (добавьте OPENAI_API_KEY в .env для транскрипции).")

    await _ask_location(message, state)


@router.message(Form.description, Command("skip"))
async def step_description_skip(message: Message, state: FSMContext):
    await state.update_data(description="—", voice_id=None)
    await _ask_location(message, state)


@router.message(Form.description)
async def step_description_text(message: Message, state: FSMContext):
    await state.update_data(description=message.text.strip(), voice_id=None)
    await _ask_location(message, state)


async def _ask_location(message: Message, state: FSMContext):
    await message.answer(
        "📍 Где именно находится оборудование внутри объекта?\n"
        "Например: *машинное отделение*, *склад 2 этаж*, *у входа в зал*\n"
        "Или /skip",
        parse_mode="Markdown",
    )
    await state.set_state(Form.location_detail)


# ── Шаг 9: Расположение ─────────────────────────────────────────────────────

@router.message(Form.location_detail, Command("skip"))
async def step_location_skip(message: Message, state: FSMContext):
    await state.update_data(location_detail="—")
    await _show_summary(message, state)


@router.message(Form.location_detail)
async def step_location(message: Message, state: FSMContext):
    await state.update_data(location_detail=message.text.strip())
    await _show_summary(message, state)


# ── Шаг 10: Подтверждение ───────────────────────────────────────────────────

async def _show_summary(message: Message, state: FSMContext):
    data = await state.get_data()
    summary = (
        "📋 *Проверьте данные перед сохранением:*\n\n"
        f"🏟 Комплекс: {data.get('complex', '—')}\n"
        f"🔢 Инв. номер: {data.get('inv_number', '—')}\n"
        f"📂 Категория: {data.get('category', '—')}\n"
        f"🔧 Состояние: {data.get('condition', '—')}\n"
        f"💬 Описание: {data.get('description', '—')}\n"
        f"📍 Расположение: {data.get('location_detail', '—')}\n"
        f"📸 Фото наклейки: {'✅' if data.get('photo_sticker_id') else '—'}\n"
        f"📸 Фото шильдика: {'✅' if data.get('photo_nameplate_id') else '—'}\n"
        f"📸 Фото общего вида: {'✅' if data.get('photo_general_id') else '—'}\n"
        f"🎤 Голосовое: {'✅' if data.get('voice_id') else '—'}\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Сохранить", callback_data="confirm:yes"),
            InlineKeyboardButton(text="❌ Отменить", callback_data="confirm:no"),
        ]
    ])
    await message.answer(summary, parse_mode="Markdown", reply_markup=kb)
    await state.set_state(Form.confirm)


@router.callback_query(Form.confirm, F.data == "confirm:yes")
async def step_confirm_yes(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await callback.message.answer("⏳ Сохраняю данные и загружаю фото...")

    data = await state.get_data()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    user = callback.from_user
    user_name = user.full_name or user.username or str(user.id)

    # Загружаем фото в Google Drive
    inv = data.get("inv_number", "NO-NUM")
    links = {}

    for key, label in [
        ("photo_sticker_id", "наклейка"),
        ("photo_nameplate_id", "шильдик"),
        ("photo_general_id", "общий_вид"),
    ]:
        fid = data.get(key)
        if fid:
            try:
                filename = f"{inv}_{label}_{now.replace(':', '-')}.jpg"
                link = await upload_photo_to_drive(bot, fid, filename)
                links[key] = link
            except Exception as e:
                log.error(f"Ошибка загрузки {label}: {e}")
                links[key] = f"[ошибка загрузки: {e}]"

    # Загружаем голосовое, если есть
    voice_link = ""
    if data.get("voice_id"):
        try:
            filename = f"{inv}_голос_{now.replace(':', '-')}.ogg"
            voice_link = await upload_photo_to_drive(bot, data["voice_id"], filename, mimetype="audio/ogg")
        except Exception as e:
            log.error(f"Ошибка загрузки голосового: {e}")
            voice_link = f"[ошибка: {e}]"

    # Формируем строку для таблицы
    row = [
        now,                                          # Дата внесения
        user_name,                                    # Кто вносил
        data.get("complex", ""),                      # Спортивный комплекс
        data.get("inv_number", ""),                   # Инвентарный номер
        data.get("category", ""),                     # Категория
        data.get("condition", ""),                     # Состояние
        data.get("description", ""),                   # Описание / назначение
        data.get("location_detail", ""),               # Расположение
        links.get("photo_sticker_id", ""),            # Ссылка: фото наклейки
        links.get("photo_nameplate_id", ""),          # Ссылка: фото шильдика
        links.get("photo_general_id", ""),            # Ссылка: фото общего вида
        voice_link,                                   # Ссылка: голосовое
    ]

    try:
        append_to_sheet(row)
        await callback.message.answer(
            f"✅ *Оборудование {inv} успешно сохранено!*\n\n"
            "Нажмите /start чтобы добавить следующее.",
            parse_mode="Markdown",
        )
    except Exception as e:
        log.error(f"Ошибка записи в таблицу: {e}")
        await callback.message.answer(
            f"⚠️ Ошибка записи в Google Sheets: `{e}`\n"
            "Данные не сохранены. Попробуйте /start заново.",
            parse_mode="Markdown",
        )

    await state.clear()
    await callback.answer()


@router.callback_query(Form.confirm, F.data == "confirm:no")
async def step_confirm_no(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer(
        "❌ Запись отменена. Нажмите /start чтобы начать заново."
    )
    await callback.answer()


# ── /find: Поиск оборудования по инвентарному номеру ─────────────────────────

@router.message(Command("find"))
async def cmd_find(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🔍 Введите инвентарный номер для поиска:\n"
        "Например: *INV-001*",
        parse_mode="Markdown",
    )
    await state.set_state(FindForm.inv_number)


@router.message(FindForm.inv_number)
async def find_by_number(message: Message, state: FSMContext):
    inv = message.text.strip()
    await state.clear()
    try:
        results = find_in_sheet(inv)
        if not results:
            await message.answer(
                f"❌ Оборудование с номером *{inv}* не найдено.\n\n"
                "Проверьте номер или нажмите /find для нового поиска.",
                parse_mode="Markdown",
            )
            return
        for r in results:
            text = (
                f"✅ *Найдено: {inv}*\n\n"
                f"🏟 Комплекс: {r.get('Спортивный комплекс', '—')}\n"
                f"📂 Категория: {r.get('Категория', '—')}\n"
                f"🔧 Состояние: {r.get('Состояние', '—')}\n"
                f"💬 Описание: {r.get('Описание / назначение', '—')}\n"
                f"📍 Расположение: {r.get('Расположение внутри объекта', '—')}\n"
                f"📅 Дата внесения: {r.get('Дата внесения', '—')}\n"
                f"👤 Кто вносил: {r.get('Кто вносил', '—')}\n"
            )
            await message.answer(text, parse_mode="Markdown")
    except Exception as e:
        log.error(f"Ошибка поиска: {e}")
        await message.answer(f"⚠️ Ошибка при поиске: `{e}`", parse_mode="Markdown")


# ── /move: Перемещение оборудования между комплексами ─────────────────────────

@router.message(Command("move"))
async def cmd_move(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "📦 Оформление перемещения оборудования.\n\n"
        "Введите инвентарный номер оборудования:",
    )
    await state.set_state(MoveForm.inv_number)


@router.message(MoveForm.inv_number)
async def move_inv_number(message: Message, state: FSMContext):
    inv = message.text.strip()
    await state.update_data(inv_number=inv)
    await message.answer(
        f"🔢 Номер: *{inv}*\n\n"
        "Выберите, **откуда** перемещается оборудование:",
        parse_mode="Markdown",
        reply_markup=make_inline_kb(SPORT_COMPLEXES, "move_from"),
    )
    await state.set_state(MoveForm.from_complex)


@router.callback_query(MoveForm.from_complex, F.data.startswith("move_from:"))
async def move_from_complex(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":")[1])
    from_complex = SPORT_COMPLEXES[idx]
    await state.update_data(from_complex=from_complex)
    await callback.message.answer(
        f"📤 Откуда: *{from_complex}*\n\n"
        "Выберите, **куда** перемещается оборудование:",
        parse_mode="Markdown",
        reply_markup=make_inline_kb(SPORT_COMPLEXES, "move_to"),
    )
    await state.set_state(MoveForm.to_complex)
    await callback.answer()


@router.callback_query(MoveForm.to_complex, F.data.startswith("move_to:"))
async def move_to_complex(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":")[1])
    to_complex = SPORT_COMPLEXES[idx]
    await state.update_data(to_complex=to_complex)
    await callback.message.answer(
        f"📥 Куда: *{to_complex}*\n\n"
        "Выберите **причину** перемещения:",
        parse_mode="Markdown",
        reply_markup=make_inline_kb(MOVE_REASONS, "move_reason"),
    )
    await state.set_state(MoveForm.reason)
    await callback.answer()


@router.callback_query(MoveForm.reason, F.data.startswith("move_reason:"))
async def move_reason(callback: CallbackQuery, state: FSMContext):
    idx = int(callback.data.split(":")[1])
    reason = MOVE_REASONS[idx]
    await state.update_data(reason=reason)
    await callback.message.answer(
        f"✏️ Причина: *{reason}*\n\n"
        "Добавьте примечание (необязательно) или /skip:",
        parse_mode="Markdown",
    )
    await state.set_state(MoveForm.note)
    await callback.answer()


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
    summary = (
        "📋 *Проверьте данные перемещения:*\n\n"
        f"🔢 Инв. номер: {data.get('inv_number', '—')}\n"
        f"📤 Откуда: {data.get('from_complex', '—')}\n"
        f"📥 Куда: {data.get('to_complex', '—')}\n"
        f"📝 Причина: {data.get('reason', '—')}\n"
        f"💬 Примечание: {data.get('note', '—')}\n"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Сохранить", callback_data="move_confirm:yes"),
            InlineKeyboardButton(text="❌ Отменить", callback_data="move_confirm:no"),
        ]
    ])
    await message.answer(summary, parse_mode="Markdown", reply_markup=kb)
    await state.set_state(MoveForm.confirm)


@router.callback_query(MoveForm.confirm, F.data == "move_confirm:yes")
async def move_confirm_yes(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    user = callback.from_user
    user_name = user.full_name or user.username or str(user.id)

    row = [
        now,
        user_name,
        data.get("inv_number", ""),
        data.get("from_complex", ""),
        data.get("to_complex", ""),
        data.get("reason", ""),
        data.get("note", ""),
    ]

    try:
        append_to_movement_log(row)
        await callback.message.answer(
            f"✅ *Перемещение {data.get('inv_number', '')} зафиксировано!*\n\n"
            "Используйте /start чтобы добавить оборудование,\n"
            "/move для нового перемещения,\n"
            "/find для поиска.",
            parse_mode="Markdown",
        )
    except Exception as e:
        log.error(f"Ошибка записи перемещения: {e}")
        await callback.message.answer(
            f"⚠️ Ошибка записи: `{e}`",
            parse_mode="Markdown",
        )

    await state.clear()
    await callback.answer()


@router.callback_query(MoveForm.confirm, F.data == "move_confirm:no")
async def move_confirm_no(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer(
        "❌ Перемещение отменено. Нажмите /move чтобы начать заново."
    )
    await callback.answer()


# ── Обработка неожиданных сообщений ──────────────────────────────────────────

@router.message()
async def fallback(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer("Нажмите /start чтобы начать инвентаризацию.")
    else:
        await message.answer(
            "⚠️ Не понял. Следуйте инструкциям выше или нажмите /cancel для отмены."
        )


# ─── Запуск ───────────────────────────────────────────────────────────────────

async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    log.info("Бот запущен. Нажмите Ctrl+C для остановки.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
