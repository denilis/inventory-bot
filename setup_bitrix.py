"""
Скрипт одноразовой инициализации Битрикс24.
Создаёт:
  - Папку "ТМЦ Фото" на общем диске
  - Список "Оборудование" с нужными полями
  - Список "Журнал перемещений" с нужными полями

Запуск: python setup_bitrix.py
После запуска скопируйте ID из вывода в .env
"""

import asyncio
import os
from dotenv import load_dotenv
from bitrix24 import Bitrix24Client

load_dotenv()

BITRIX_WEBHOOK_URL = os.getenv("BITRIX_WEBHOOK_URL", "")

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


async def setup():
    if not BITRIX_WEBHOOK_URL:
        print("ОШИБКА: задайте BITRIX_WEBHOOK_URL в .env")
        return

    bx = Bitrix24Client(BITRIX_WEBHOOK_URL)

    try:
        print("=" * 60)
        print("Инициализация Битрикс24")
        print("=" * 60)

        # ── 1. Папка для фото ────────────────────────────────────────
        print("\n[1/3] Создание папки для фото на диске...")
        storage = await bx.get_common_storage()
        root_folder_id = int(storage["ROOT_OBJECT_ID"])
        print(f"      Общее хранилище: {storage['NAME']} (root folder {root_folder_id})")

        folder = await bx.create_folder(root_folder_id, "ТМЦ Фото")
        photo_folder_id = int(folder["ID"])
        print(f"      Папка создана: ID = {photo_folder_id}")

        # ── 2. Список "Оборудование" ─────────────────────────────────
        print("\n[2/3] Создание списка 'Оборудование'...")
        eq_list_id = await bx.create_list("Оборудование", "tmc_equipment",
                                           "Учёт ТМЦ — инвентаризация оборудования")
        print(f"      Список создан: IBLOCK_ID = {eq_list_id}")

        print("      Добавление полей...")
        fields_eq = {}

        fields_eq["DATE"] = await bx.add_list_field(
            eq_list_id, "Дата внесения", "DATE_ADD", "S:DateTime", sort=10)
        fields_eq["USER"] = await bx.add_list_field(
            eq_list_id, "Кто вносил", "USER_NAME", "S", sort=20)
        fields_eq["COMPLEX"] = await bx.add_list_field(
            eq_list_id, "Спортивный комплекс", "COMPLEX", "L",
            list_values=SPORT_COMPLEXES, sort=30)
        fields_eq["INV_NUM"] = await bx.add_list_field(
            eq_list_id, "Инвентарный номер", "INV_NUM", "S", sort=40)
        fields_eq["CATEGORY"] = await bx.add_list_field(
            eq_list_id, "Категория", "CATEGORY", "L",
            list_values=CATEGORIES, sort=50)
        fields_eq["CONDITION"] = await bx.add_list_field(
            eq_list_id, "Состояние", "CONDITION", "L",
            list_values=CONDITIONS, sort=60)
        fields_eq["DESCRIPTION"] = await bx.add_list_field(
            eq_list_id, "Описание", "DESCRIPTION", "S", sort=70)
        fields_eq["PHOTO_STICKER"] = await bx.add_list_field(
            eq_list_id, "Фото наклейки (ссылка)", "PHOTO_STICKER", "S", sort=80)
        fields_eq["PHOTO_NAMEPLATE"] = await bx.add_list_field(
            eq_list_id, "Фото шильдика (ссылка)", "PHOTO_NAMEPLATE", "S", sort=90)
        fields_eq["PHOTO_GENERAL"] = await bx.add_list_field(
            eq_list_id, "Фото общего вида (ссылка)", "PHOTO_GENERAL", "S", sort=100)
        fields_eq["VOICE"] = await bx.add_list_field(
            eq_list_id, "Голосовое (ссылка)", "VOICE_LINK", "S", sort=110)

        print("      Поля добавлены:")
        for k, v in fields_eq.items():
            print(f"        {k}: {v}")

        # ── 3. Список "Журнал перемещений" ───────────────────────────
        print("\n[3/3] Создание списка 'Журнал перемещений'...")
        mv_list_id = await bx.create_list("Журнал перемещений", "tmc_movements",
                                           "Учёт ТМЦ — журнал перемещений оборудования")
        print(f"      Список создан: IBLOCK_ID = {mv_list_id}")

        print("      Добавление полей...")
        fields_mv = {}

        fields_mv["DATE"] = await bx.add_list_field(
            mv_list_id, "Дата", "DATE_MOVE", "S:DateTime", sort=10)
        fields_mv["USER"] = await bx.add_list_field(
            mv_list_id, "Кто оформил", "USER_NAME", "S", sort=20)
        fields_mv["INV_NUM"] = await bx.add_list_field(
            mv_list_id, "Инвентарный номер", "INV_NUM", "S", sort=30)
        fields_mv["FROM"] = await bx.add_list_field(
            mv_list_id, "Откуда", "FROM_COMPLEX", "L",
            list_values=SPORT_COMPLEXES, sort=40)
        fields_mv["TO"] = await bx.add_list_field(
            mv_list_id, "Куда", "TO_COMPLEX", "L",
            list_values=SPORT_COMPLEXES, sort=50)
        fields_mv["REASON"] = await bx.add_list_field(
            mv_list_id, "Причина", "REASON", "L",
            list_values=MOVE_REASONS, sort=60)
        fields_mv["NOTE"] = await bx.add_list_field(
            mv_list_id, "Примечание", "NOTE", "S", sort=70)

        print("      Поля добавлены:")
        for k, v in fields_mv.items():
            print(f"        {k}: {v}")

        # ── Итог ─────────────────────────────────────────────────────
        print("\n" + "=" * 60)
        print("ГОТОВО. Добавьте в .env следующие строки:")
        print("=" * 60)
        print(f"BITRIX_FOLDER_ID={photo_folder_id}")
        print(f"BITRIX_EQUIPMENT_LIST_ID={eq_list_id}")
        print(f"BITRIX_MOVEMENT_LIST_ID={mv_list_id}")
        print()
        print("Маппинг полей списка 'Оборудование' (для bot.py):")
        for k, v in fields_eq.items():
            print(f"  {k} = {v}")
        print()
        print("Маппинг полей списка 'Журнал перемещений':")
        for k, v in fields_mv.items():
            print(f"  {k} = {v}")
        print("=" * 60)

    except Exception as e:
        print(f"\nОШИБКА: {e}")
        raise
    finally:
        await bx.close()


if __name__ == "__main__":
    asyncio.run(setup())
