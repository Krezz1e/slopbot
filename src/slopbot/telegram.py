from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import secrets
import time
from typing import Any

import aiohttp

from slopbot.database import SubscriptionStore

LOGGER = logging.getLogger(__name__)
MAX_RECONNECT_DELAY_SECONDS = 60


class TelegramError(RuntimeError):
    pass


class TelegramService:
    def __init__(self, *, token: str, store: SubscriptionStore) -> None:
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._store = store
        self._session: aiohttp.ClientSession | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._offset = 0
        self.username: str | None = None

    async def start(self) -> None:
        if self._poll_task is not None and not self._poll_task.done():
            return
        self._poll_task = asyncio.create_task(
            self._run_supervised(),
            name="telegram-link-poller",
        )

    async def close(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None
        if self._session is not None:
            await self._session.close()
            self._session = None
        self.username = None

    async def _run_supervised(self) -> None:
        reconnect_delay = 1
        while True:
            try:
                await self._connect()
                reconnect_delay = 1
                await self._poll()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception(
                    "Telegram connection failed; retrying in %s second(s)",
                    reconnect_delay,
                )
            finally:
                self.username = None
                if self._session is not None:
                    await self._session.close()
                    self._session = None
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(MAX_RECONNECT_DELAY_SECONDS, reconnect_delay * 2)

    async def _connect(self) -> None:
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=40))
        await self._call("deleteWebhook", {"drop_pending_updates": False})
        identity = await self._call("getMe")
        if not isinstance(identity, dict):
            raise TelegramError("Telegram getMe returned an invalid result")
        username = identity.get("username")
        if not isinstance(username, str) or not username:
            raise TelegramError("Telegram getMe did not return a bot username")
        self.username = username
        LOGGER.info("Telegram delivery connected as @%s", username)

    async def create_link_url(self, *, discord_user_id: int) -> str:
        if self.username is None:
            raise TelegramError("Telegram is not connected")
        code = secrets.token_urlsafe(18)
        await self._store.save_telegram_link_code(
            discord_user_id=discord_user_id,
            code_hash=_hash_code(code),
            expires_at=int(time.time()) + 600,
        )
        return f"https://t.me/{self.username}?start={code}"

    async def send_message(self, *, chat_id: int, text: str) -> bool:
        try:
            await self._call("sendMessage", {"chat_id": chat_id, "text": text})
        except (aiohttp.ClientError, TimeoutError, TelegramError):
            LOGGER.exception("Could not send Telegram message to chat %s", chat_id)
            return False
        return True

    async def _poll(self) -> None:
        while True:
            updates = await self._call(
                "getUpdates",
                {
                    "offset": self._offset,
                    "timeout": 30,
                    "allowed_updates": ["message"],
                },
            )
            if not isinstance(updates, list):
                raise TelegramError("Telegram getUpdates returned an invalid result")
            for update in updates:
                if not isinstance(update, dict):
                    continue
                await self._handle_update(update)
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    self._offset = max(self._offset, update_id + 1)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        text = message.get("text")
        if not isinstance(chat, dict) or chat.get("type") != "private" or not isinstance(text, str):
            return
        chat_id = chat.get("id")
        if not isinstance(chat_id, int) or not text.startswith("/start"):
            return
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            await self.send_message(
                chat_id=chat_id,
                text="Run /notifications in Discord to create a private linking button.",
            )
            return
        discord_user_id = await self._store.consume_telegram_link_code(
            code_hash=_hash_code(parts[1].strip()),
            telegram_chat_id=chat_id,
        )
        if discord_user_id is None:
            await self.send_message(
                chat_id=chat_id,
                text="That link is invalid or expired. Create a new one with /notifications.",
            )
            return
        await self.send_message(
            chat_id=chat_id,
            text="Telegram is linked to your Discord notification preferences.",
        )

    async def _call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        if self._session is None:
            raise TelegramError("Telegram session is not running")
        try:
            async with self._session.post(
                f"{self._base_url}/{method}", json=payload or {}
            ) as response:
                try:
                    data = await response.json(content_type=None)
                except (TypeError, ValueError) as error:
                    raise TelegramError(
                        f"Telegram {method} returned an invalid response (HTTP {response.status})"
                    ) from error
        except (aiohttp.ClientError, TimeoutError):
            # aiohttp exceptions can contain the request URL. Telegram embeds the bot token in
            # that URL, so normalize transport failures before callers log them.
            raise TelegramError(f"Telegram {method} request failed") from None
        if not isinstance(data, dict) or data.get("ok") is not True:
            description = data.get("description") if isinstance(data, dict) else None
            raise TelegramError(str(description or f"Telegram {method} failed"))
        return data.get("result")


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()
