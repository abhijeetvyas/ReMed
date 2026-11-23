"""Telegram channel adapter — the only module that knows about the Bot API.

Implements the adapter interface from tech-design-v0.1 §2 so a future WhatsApp
adapter can slot in behind the same calls.
"""
import logging
import asyncio

import httpx

from .config import settings

log = logging.getLogger("remed.telegram")


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self._client = httpx.AsyncClient(timeout=20)

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, payload: dict) -> dict:
        try:
            resp = await self._client.post(f"{self.base}/{method}", json=payload)
        except httpx.HTTPError as e:
            raise TelegramError(f"network error on {method}: {e}") from e
        try:
            data = resp.json()
        except ValueError as e:
            raise TelegramError(f"bad response on {method}: HTTP {resp.status_code}") from e
        if resp.status_code != 200 or not data.get("ok"):
            raise TelegramError(f"{method} failed: HTTP {resp.status_code} {data.get('description', '')}")
        return data["result"]

    # -------------------------------------------------- outbound ops (adapter interface)

    async def send_text(self, chat_id: str, text: str) -> int:
        result = await self._call("sendMessage", {"chat_id": chat_id, "text": text})
        return result["message_id"]

    async def send_message_with_buttons(self, chat_id: str, text: str, buttons: list[list[dict]]) -> int:
        """buttons: rows of {'text': str, 'callback_data': str}"""
        result = await self._call("sendMessage", {
            "chat_id": chat_id, "text": text,
            "reply_markup": {"inline_keyboard": [[
                {"text": b["text"], "callback_data": b["callback_data"]} for b in row
            ] for row in buttons]},
        })
        return result["message_id"]

    async def update_message_buttons(self, chat_id: str, message_id: int, buttons: list[list[dict]]) -> None:
        markup = ({"inline_keyboard": [[
            {"text": b["text"], "callback_data": b["callback_data"]} for b in row
        ] for row in buttons]} if buttons else {"inline_keyboard": []})
        await self._call("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id, "reply_markup": markup})

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        payload: dict = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        try:
            await self._call("answerCallbackQuery", payload)
        except TelegramError as e:
            log.warning("answerCallbackQuery failed: %s", e)  # never blocks the ack path

    # -------------------------------------------------- bot administration

    async def set_webhook(self, url: str, secret: str) -> None:
        await self._call("setWebhook", {"url": url, "secret_token": secret, "allowed_updates": ["message", "callback_query"]})

    async def delete_webhook(self) -> None:
        await self._call("deleteWebhook", {})

    async def get_updates(self, offset: int, timeout: int) -> list[dict]:
        return await self._call("getUpdates", {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]})


def extract_event(update: dict) -> dict | None:
    """Normalize a Telegram update into the internal event format:
    {kind: photo|text|callback, chat_id, ...} — the router only ever sees this shape."""
    msg = update.get("message")
    if msg:
        chat_id = str(msg["chat"]["id"])
        if msg.get("photo"):
            # Telegram sends several sizes; the last is the largest
            file_id = msg["photo"][-1]["file_id"]
            return {"kind": "photo", "chat_id": chat_id, "file_id": file_id}
        text = msg.get("text", "").strip()
        if text:
            return {"kind": "text", "chat_id": chat_id, "text": text}
        return None
    cb = update.get("callback_query")
    if cb:
        msg = cb.get("message") or {}
        return {
            "kind": "callback",
            "chat_id": str(msg.get("chat", {}).get("id", "")),
            "callback_id": cb["id"],
            "data": cb.get("data", ""),
            "message_id": msg.get("message_id"),
        }
    return None


async def download_photo(token: str, file_id: str) -> bytes:
    base = f"https://api.telegram.org/bot{token}"
    max_bytes = settings.max_image_mb * 1024 * 1024
    async with httpx.AsyncClient(timeout=30) as client:
        meta = (await client.get(f"{base}/getFile", params={"file_id": file_id})).json()
        if not meta.get("ok"):
            raise TelegramError("getFile failed")
        path = meta["result"]["file_path"]
        resp = await client.get(f"{base}/file/{path}")
        if resp.status_code >= 400:
            raise TelegramError(f"file download failed: HTTP {resp.status_code}")
        if len(resp.content) > max_bytes:
            raise TelegramError("image too large")
        if resp.content[:8] == b"\x89PNG\r\n\x1a\n":
            return resp.content  # PNG magic
        if resp.content[:3] == b"\xff\xd8\xff":
            return resp.content  # JPEG magic
        raise TelegramError("not an image")
