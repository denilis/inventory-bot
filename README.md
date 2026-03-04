# 🏟 Telegram-бот для инвентаризации оборудования

![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python)
![aiogram](https://img.shields.io/badge/aiogram-3.x-green)
![License](https://img.shields.io/badge/License-MIT-yellow)

MVP-бот для пошагового аудита оборудования спортивных комплексов.  
Сотрудник на объекте проходит по шагам в Telegram → данные и фото сохраняются в Google Sheets + Google Drive.

![Схема работы](https://img.shields.io/badge/Telegram_Bot-→_Google_Sheets_+_Drive-brightgreen?style=for-the-badge)

---

## Что умеет бот

- Пошаговый сбор данных: комплекс → фото наклейки → инв. номер → фото шильдика → фото общего вида → категория → состояние → описание (текст или голос) → расположение
- Кнопки выбора: комплекс, категория, состояние — не нужно печатать
- Пропуск необязательных шагов через `/skip`
- Загрузка фото и голосовых в Google Drive (с публичными ссылками)
- Запись всех данных одной строкой в Google Sheets
- Подтверждение перед сохранением — можно отменить и начать заново

---

## Структура таблицы

**Лист «Оборудование»:**

| Дата | Кто вносил | Комплекс | Инв. номер | Категория | Состояние | Описание | Расположение | Фото наклейки | Фото шильдика | Фото общего вида | Голосовое |
|------|-----------|----------|-----------|-----------|-----------|----------|-------------|--------------|--------------|-----------------|----------|

**Лист «Журнал перемещений»** (для будущего использования):

| Дата | Кто оформил | Инв. номер | Откуда | Куда | Причина | Примечание |
|------|------------|-----------|--------|------|---------|-----------|

---

## Установка и запуск (пошагово)

### 1. Создайте Telegram-бота

1. Откройте Telegram → найдите `@BotFather`
2. Отправьте `/newbot`
3. Введите имя бота (например, «Инвентаризация СК»)
4. Введите username (например, `sk_inventory_bot`)
5. Скопируйте **токен** — он выглядит как `123456789:ABCdef...`

### 2. Настройте Google Cloud

#### Создайте проект и включите API:
1. Перейдите на https://console.cloud.google.com
2. Создайте новый проект (например, «Inventory Bot»)
3. В меню → **APIs & Services** → **Library**
4. Найдите и включите: **Google Sheets API** и **Google Drive API**

#### Создайте сервисный аккаунт:
1. **APIs & Services** → **Credentials** → **Create Credentials** → **Service Account**
2. Имя: `inventory-bot`
3. Роль: **Editor**
4. После создания → кликните на аккаунт → **Keys** → **Add Key** → **JSON**
5. Скачается файл `credentials.json` — положите его в папку проекта

#### Подготовьте Google Sheets:
1. Создайте новую таблицу в Google Sheets
2. Из URL скопируйте **ID таблицы**: `https://docs.google.com/spreadsheets/d/`**`<ВОТ_ЭТОТ_ID>`**`/edit`
3. Откройте доступ к таблице для email сервисного аккаунта (найдите его в `credentials.json` → поле `client_email`) — роль **Редактор**

#### Подготовьте папку Google Drive:
1. Создайте папку в Google Drive (например, «Фото инвентаризации»)
2. Из URL скопируйте **ID папки**: `https://drive.google.com/drive/folders/`**`<ВОТ_ЭТОТ_ID>`**
3. Откройте доступ к папке для email сервисного аккаунта — роль **Редактор**

### 3. Установите и запустите бота

```bash
# Клонируйте или скопируйте папку проекта
cd inventory_bot

# Установите зависимости
pip install -r requirements.txt

# Создайте файл .env (скопируйте из шаблона и заполните)
cp .env.example .env
# Отредактируйте .env — впишите свои токены и ID

# Настройте таблицу (один раз)
python setup_sheet.py

# Запустите бота
python bot.py
```

### 4. Для постоянной работы (сервер)

Бот должен работать 24/7. Варианты:
- **VPS** (Timeweb, Selectel, Beget): от 200 ₽/мес
- **Railway.app**: бесплатный tier, деплой через GitHub
- **Свой ПК**: подходит для тестирования, не для продакшена

#### Вариант А: Docker (рекомендуется)

```bash
# Убедитесь, что .env и credentials.json на месте
docker-compose up -d --build
```

Остановка: `docker-compose down`  
Логи: `docker-compose logs -f`

#### Вариант Б: systemd

Пример systemd-юнита для VPS:

```ini
# /etc/systemd/system/inventory-bot.service
[Unit]
Description=Inventory Telegram Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/inventory_bot
EnvironmentFile=/home/ubuntu/inventory_bot/.env
ExecStart=/usr/bin/python3 /home/ubuntu/inventory_bot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable inventory-bot
sudo systemctl start inventory-bot
```

---

## Кастомизация

### Изменить список комплексов
В `bot.py` отредактируйте массив `SPORT_COMPLEXES` — впишите реальные названия.

### Изменить категории оборудования
Отредактируйте массив `CATEGORIES`.

### Добавить распознавание голоса
Бот уже сохраняет голосовые сообщения в Google Drive. Чтобы добавить автоматическое
распознавание в текст, интегрируйте OpenAI Whisper API:

```python
import openai

async def transcribe_voice(bot, file_id):
    file = await bot.get_file(file_id)
    file_bytes = io.BytesIO()
    await bot.download_file(file.file_path, file_bytes)
    file_bytes.seek(0)
    file_bytes.name = "voice.ogg"
    result = openai.audio.transcriptions.create(
        model="whisper-1", file=file_bytes, language="ru"
    )
    return result.text
```

### Добавить сканирование QR из фото
Для автоматического чтения QR-кода с фото наклейки используйте библиотеку `pyzbar`:

```python
from pyzbar.pyzbar import decode
from PIL import Image

def read_qr_from_image(image_bytes):
    img = Image.open(io.BytesIO(image_bytes))
    codes = decode(img)
    if codes:
        return codes[0].data.decode("utf-8")
    return None
```

---

## Структура проекта

```
inventory_bot/
├── bot.py              # Основной код бота
├── setup_sheet.py      # Скрипт настройки таблицы
├── requirements.txt    # Зависимости Python
├── Dockerfile          # Контейнеризация
├── docker-compose.yml  # Запуск одной командой
├── .env.example        # Шаблон переменных окружения
├── .gitignore          # Исключения для Git
├── LICENSE             # Лицензия MIT
├── .env                # Ваши реальные токены (НЕ коммитить!)
├── credentials.json    # Ключ сервисного аккаунта (НЕ коммитить!)
└── README.md           # Эта инструкция
```
