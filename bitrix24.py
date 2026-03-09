"""
Async Bitrix24 REST API client via incoming webhook.
Используется: disk.folder.uploadfile, disk.file.getExternalLink,
              lists.element.add, lists.element.get
"""

import asyncio
import base64
import logging
import uuid
from typing import Any, Optional

import aiohttp

log = logging.getLogger(__name__)


class Bitrix24Client:
    def __init__(self, webhook_url: str):
        """
        webhook_url: URL вебхука без имени метода.
        Пример: https://sport-vsegda.bitrix24.ru/rest/59/abc123
        """
        self.webhook_url = webhook_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def call(self, method: str, params: dict = None) -> Any:
        """
        Вызов метода REST API.
        Возвращает поле result из ответа.
        При ошибке QUERY_LIMIT_EXCEEDED — ждёт 1 с и повторяет.
        """
        session = await self._session_get()
        url = f"{self.webhook_url}/{method}"

        async with session.post(url, json=params or {}) as resp:
            data = await resp.json(content_type=None)

        if "error" in data:
            code = data["error"]
            desc = data.get("error_description", "")
            if code == "QUERY_LIMIT_EXCEEDED":
                log.warning("Bitrix24: rate limit, повтор через 1 с")
                await asyncio.sleep(1)
                return await self.call(method, params)
            raise RuntimeError(f"Bitrix24 [{code}]: {desc}")

        return data.get("result")

    # ── Диск ─────────────────────────────────────────────────────────────────

    async def get_common_storage(self) -> dict:
        """Возвращает общее хранилище портала (type=common)."""
        storages = await self.call("disk.storage.getlist")
        for s in storages:
            if s.get("ENTITY_TYPE") == "common":
                return s
        # fallback — первое доступное
        return storages[0]

    async def create_folder(self, parent_folder_id: int, name: str) -> dict:
        """Создаёт подпапку. Возвращает объект папки."""
        return await self.call("disk.folder.addsubfolder", {
            "id": parent_folder_id,
            "data": {"NAME": name},
        })

    async def get_folder_children(self, folder_id: int) -> list:
        return await self.call("disk.folder.getchildren", {"id": folder_id}) or []

    async def upload_file(self, folder_id: int, filename: str,
                          file_bytes: bytes) -> dict:
        """
        Загружает файл в папку через base64.
        Возвращает объект файла (содержит ID, DOWNLOAD_URL и др.).
        """
        b64 = base64.b64encode(file_bytes).decode()
        return await self.call("disk.folder.uploadfile", {
            "id": folder_id,
            "data": {"NAME": filename},
            "fileContent": [filename, b64],
            "generateUniqueName": True,
        })

    async def get_file_public_link(self, file_id: int) -> str:
        """Возвращает публичную короткую ссылку на файл."""
        return await self.call("disk.file.getExternalLink", {"id": file_id})

    # ── Списки ───────────────────────────────────────────────────────────────

    async def create_list(self, name: str, code: str,
                          description: str = "") -> int:
        """Создаёт универсальный список. Возвращает IBLOCK_ID."""
        return await self.call("lists.add", {
            "IBLOCK_TYPE_ID": "lists",
            "IBLOCK_CODE": code,
            "FIELDS": {
                "NAME": name,
                "DESCRIPTION": description,
                "SORT": 100,
            },
        })

    async def add_list_field(self, iblock_id: int, name: str, code: str,
                             field_type: str, required: bool = False,
                             multiple: bool = False,
                             list_values: list[str] | None = None,
                             sort: int = 100) -> str:
        """
        Добавляет поле (колонку) в список.
        Возвращает строку вида 'PROPERTY_123'.
        """
        fields: dict = {
            "NAME": name,
            "TYPE": field_type,
            "CODE": code,
            "IS_REQUIRED": "Y" if required else "N",
            "MULTIPLE": "Y" if multiple else "N",
            "SORT": sort,
            "SETTINGS": {"SHOW_ADD_FORM": "Y", "SHOW_EDIT_FORM": "Y"},
        }
        if field_type == "L" and list_values:
            fields["LIST"] = {
                str(i): {"VALUE": v, "SORT": (i + 1) * 10}
                for i, v in enumerate(list_values)
            }
        result = await self.call("lists.field.add", {
            "IBLOCK_TYPE_ID": "lists",
            "IBLOCK_ID": iblock_id,
            "FIELDS": fields,
        })
        # result может быть строкой 'PROPERTY_123' или числом
        return str(result) if result else ""

    async def get_list_fields(self, iblock_id: int) -> dict:
        """Возвращает словарь полей списка (код → описание)."""
        result = await self.call("lists.field.get", {
            "IBLOCK_TYPE_ID": "lists",
            "IBLOCK_ID": iblock_id,
        })
        return result or {}

    async def add_element(self, iblock_id: int, name: str,
                          properties: dict | None = None) -> int:
        """
        Добавляет запись в список.
        properties: словарь {'PROPERTY_123': 'значение', ...}
        Возвращает ID созданной записи.
        """
        fields: dict = {"NAME": name}
        if properties:
            fields.update(properties)
        return await self.call("lists.element.add", {
            "IBLOCK_TYPE_ID": "lists",
            "IBLOCK_ID": iblock_id,
            "ELEMENT_CODE": f"e_{uuid.uuid4().hex[:8]}",
            "FIELDS": fields,
        })

    async def get_elements(self, iblock_id: int,
                           filters: dict | None = None,
                           select: list[str] | None = None,
                           order: dict | None = None,
                           start: int = 0) -> list:
        """Поиск записей в списке. Возвращает список элементов."""
        params: dict = {
            "IBLOCK_TYPE_ID": "lists",
            "IBLOCK_ID": iblock_id,
            "start": start,
        }
        if filters:
            params["FILTER"] = filters
        if select:
            params["SELECT"] = select
        if order:
            params["ELEMENT_ORDER"] = order
        result = await self.call("lists.element.get", params)
        return result or []
