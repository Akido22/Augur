"""Канал Telegram (Bot API `sendMessage`) на httpx (ARCHITECTURE.md §2.11).

Токен бота стоит в пути URL, поэтому ни URL, ни текст исключений httpx (они содержат URL)
наружу не уходят: `NotifyError` несёт только тип ошибки или HTTP-код. Ответ Telegram в лог
и в БД не пишется.
"""

from typing import Protocol

import httpx
from pydantic import SecretStr

from augur.notify.models import Notification

__all__ = ["Notifier", "NotifyError", "TelegramNotifier"]


class NotifyError(RuntimeError):
    """Отправка не удалась. Сообщение — безопасная причина (без URL и токена)."""


class Notifier(Protocol):
    async def send(self, msg: Notification) -> None: ...


class TelegramNotifier:
    def __init__(
        self,
        token: SecretStr,
        chat_id: str,
        *,
        api_base: str = "https://api.telegram.org",
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._base = api_base.rstrip("/")
        self._timeout = timeout_s
        self._transport = transport

    def __repr__(self) -> str:
        return f"TelegramNotifier(chat_id={self._chat_id!r})"

    async def send(self, msg: Notification) -> None:
        url = f"{self._base}/bot{self._token.get_secret_value()}/sendMessage"
        body = {"chat_id": self._chat_id, "text": msg.text(), "disable_web_page_preview": True}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.post(url, json=body)
        except httpx.HTTPError as e:
            raise NotifyError(type(e).__name__) from None
        if resp.status_code != 200:
            raise NotifyError(f"http {resp.status_code}")
        try:
            ok = bool(resp.json().get("ok"))
        except ValueError:
            ok = False
        if not ok:
            raise NotifyError("telegram ok=false")
