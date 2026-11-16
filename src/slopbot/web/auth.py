from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit

T = TypeVar("T")
SESSION_SECONDS = 7 * 86400


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class WebIdentityStore:
    """OAuth subjects are identities; email addresses never merge accounts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()

    async def run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        def execute() -> T:
            connection = sqlite3.connect(self.path, timeout=15)
            connection.row_factory = sqlite3.Row
            try:
                with connection:
                    return fn(connection)
            finally:
                connection.close()

        async with self.lock:
            worker = asyncio.create_task(asyncio.to_thread(execute))
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Cancelling an HTTP request cannot stop its SQLite thread. Keep
                # identity operations serialized until that thread has closed its
                # connection, including when shutdown cancels this task repeatedly.
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not worker.cancelled():
                    worker.exception()
                raise

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def create(c: sqlite3.Connection) -> None:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS web_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    google_subject TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    email TEXT NOT NULL,
                    discord_id TEXT UNIQUE,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS web_sessions (
                    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS web_oauth (
                    state_hash TEXT PRIMARY KEY, provider TEXT NOT NULL,
                    session_hash TEXT NOT NULL, verifier TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS web_sessions_user ON web_sessions(user_id);
                CREATE TABLE IF NOT EXISTS web_preferences (
                    user_id INTEGER PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS web_push_subscriptions (
                    endpoint TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                    p256dh TEXT NOT NULL, auth TEXT NOT NULL,
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS web_push_user ON web_push_subscriptions(user_id);
            """)

        await self.run(create)

    async def begin_oauth(self, provider: str, session: str) -> tuple[str, str]:
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)

        def save(c: sqlite3.Connection) -> None:
            now = int(time.time())
            c.execute("DELETE FROM web_oauth WHERE expires_at < ?", (now,))
            c.execute("DELETE FROM web_sessions WHERE expires_at < ?", (now,))
            if c.execute("SELECT COUNT(*) FROM web_oauth").fetchone()[0] >= 2000:
                raise ValueError("Sign-in is busy. Please try again shortly.")
            c.execute(
                "INSERT INTO web_oauth VALUES (?, ?, ?, ?, ?)",
                (digest(state), provider, digest(session), verifier, now + 600),
            )

        await self.run(save)
        return state, verifier

    async def consume_oauth(self, state: str, provider: str, session: str) -> str | None:
        def consume(c: sqlite3.Connection) -> str | None:
            row = c.execute(
                "SELECT * FROM web_oauth WHERE state_hash = ?", (digest(state),)
            ).fetchone()
            c.execute("DELETE FROM web_oauth WHERE state_hash = ?", (digest(state),))
            if (
                row is None
                or row["provider"] != provider
                or row["session_hash"] != digest(session)
                or row["expires_at"] < time.time()
            ):
                return None
            return str(row["verifier"])

        return await self.run(consume)

    async def login(self, subject: str, name: str, email: str) -> str:
        token = secrets.token_urlsafe(48)

        def save(c: sqlite3.Connection) -> None:
            row = c.execute(
                "SELECT id FROM web_users WHERE google_subject = ?", (subject,)
            ).fetchone()
            if row is None and c.execute("SELECT COUNT(*) FROM web_users").fetchone()[0] >= 1000:
                raise ValueError("New sign-ups are currently at capacity. Please try again later.")
            c.execute(
                "INSERT INTO web_users (google_subject, name, email, created_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(google_subject) "
                "DO UPDATE SET name=excluded.name, email=excluded.email",
                (subject, name[:100], email[:254], int(time.time())),
            )
            user_id = c.execute(
                "SELECT id FROM web_users WHERE google_subject = ?", (subject,)
            ).fetchone()[0]
            c.execute("DELETE FROM web_sessions WHERE expires_at < ?", (int(time.time()),))
            c.execute(
                "DELETE FROM web_sessions WHERE user_id=? AND token_hash NOT IN "
                "(SELECT token_hash FROM web_sessions WHERE user_id=? "
                "ORDER BY expires_at DESC LIMIT 4)",
                (user_id, user_id),
            )
            c.execute(
                "INSERT INTO web_sessions VALUES (?, ?, ?)",
                (digest(token), user_id, int(time.time()) + SESSION_SECONDS),
            )

        await self.run(save)
        return token

    async def user(self, token: str) -> dict[str, Any] | None:
        def read(c: sqlite3.Connection) -> dict[str, Any] | None:
            row = c.execute(
                "SELECT u.id,u.name,u.email,u.discord_id FROM web_users u "
                "JOIN web_sessions s ON s.user_id=u.id WHERE s.token_hash=? AND s.expires_at>?",
                (digest(token), int(time.time())),
            ).fetchone()
            return dict(row) if row else None

        return await self.run(read)

    async def link_discord(self, user_id: int, discord_id: str) -> None:
        def link(c: sqlite3.Connection) -> None:
            prior = c.execute("SELECT discord_id FROM web_users WHERE id=?", (user_id,)).fetchone()
            if prior is None:
                raise ValueError("Sign in again before linking Discord.")
            if prior[0] is not None and prior[0] != discord_id:
                raise ValueError("Unlink your current Discord account first.")
            try:
                c.execute("UPDATE web_users SET discord_id=? WHERE id=?", (discord_id, user_id))
            except sqlite3.IntegrityError as error:
                raise ValueError(
                    "That Discord account is already linked to another account."
                ) from error

        await self.run(link)

    async def unlink_discord(self, user_id: int) -> None:
        await self.run(
            lambda c: c.execute("UPDATE web_users SET discord_id=NULL WHERE id=?", (user_id,))
        )

    async def logout(self, token: str) -> None:
        await self.run(
            lambda c: c.execute("DELETE FROM web_sessions WHERE token_hash=?", (digest(token),))
        )

    async def delete_user(self, user_id: int) -> None:
        def delete(c: sqlite3.Connection) -> None:
            c.execute("DELETE FROM web_push_subscriptions WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM web_preferences WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM web_sessions WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM web_users WHERE id=?", (user_id,))

        await self.run(delete)

    async def save_push_subscription(
        self, user_id: int, endpoint: str, p256dh: str, auth: str
    ) -> None:
        if (
            urlsplit(endpoint).scheme != "https"
            or not urlsplit(endpoint).hostname
            or len(endpoint) > 2048
            or not 20 <= len(p256dh) <= 512
            or not 8 <= len(auth) <= 512
        ):
            raise ValueError("Invalid push subscription.")

        def save(c: sqlite3.Connection) -> None:
            prior = c.execute(
                "SELECT user_id FROM web_push_subscriptions WHERE endpoint=?", (endpoint,)
            ).fetchone()
            if prior is not None and int(prior[0]) != user_id:
                raise ValueError("That browser subscription belongs to another account.")
            if (
                prior is None
                and c.execute(
                    "SELECT COUNT(*) FROM web_push_subscriptions WHERE user_id=?", (user_id,)
                ).fetchone()[0]
                >= 5
            ):
                raise ValueError("Remove another device before enabling push on this one.")
            now = int(time.time())
            c.execute(
                "INSERT INTO web_push_subscriptions VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, "
                "auth=excluded.auth, updated_at=excluded.updated_at",
                (endpoint, user_id, p256dh, auth, now, now),
            )

        await self.run(save)

    async def delete_push_subscription(self, user_id: int, endpoint: str) -> bool:
        return await self.run(
            lambda c: c.execute(
                "DELETE FROM web_push_subscriptions WHERE user_id=? AND endpoint=?",
                (user_id, endpoint),
            ).rowcount
            > 0
        )

    async def delete_push_endpoint(self, endpoint: str) -> None:
        await self.run(
            lambda c: c.execute("DELETE FROM web_push_subscriptions WHERE endpoint=?", (endpoint,))
        )

    async def push_subscriptions_for_discord(self, discord_id: str) -> list[dict[str, str]]:
        def read(c: sqlite3.Connection) -> list[dict[str, str]]:
            rows = c.execute(
                "SELECT endpoint,p256dh,auth FROM web_push_subscriptions p "
                "JOIN web_users u ON u.id=p.user_id WHERE u.discord_id=?",
                (discord_id,),
            ).fetchall()
            return [dict(row) for row in rows]

        return await self.run(read)

    async def push_subscription_count(self, user_id: int) -> int:
        return await self.run(
            lambda c: int(
                c.execute(
                    "SELECT COUNT(*) FROM web_push_subscriptions WHERE user_id=?", (user_id,)
                ).fetchone()[0]
            )
        )

    async def preferences(self, user_id: int) -> dict[str, Any]:
        def read(c: sqlite3.Connection) -> dict[str, Any]:
            row = c.execute(
                "SELECT payload FROM web_preferences WHERE user_id=?", (user_id,)
            ).fetchone()
            return self._normalized_preferences(row[0] if row else None)

        return await self.run(read)

    async def theme_preferences_for_discord(self, discord_id: str) -> dict[str, str]:
        """Return saved visual preferences for a linked Discord account."""

        def read(c: sqlite3.Connection) -> dict[str, str]:
            row = c.execute(
                "SELECT p.payload FROM web_users u JOIN web_preferences p ON p.user_id=u.id "
                "WHERE u.discord_id=?",
                (discord_id,),
            ).fetchone()
            if row is None:
                return {}
            data = dict(json.loads(row[0]))
            return {
                key: value
                for key in ("theme_preset", "theme_mode")
                if isinstance((value := data.get(key)), str)
            }

        return await self.run(read)

    @staticmethod
    def _normalized_preferences(payload: str | None) -> dict[str, Any]:
        data = dict(json.loads(payload)) if payload else {}
        report_view = dict(data.get("report_view", {}))
        data["timezone"] = data.get("timezone") or report_view.get("timezone") or "America/New_York"
        report_view["timezone"] = data["timezone"]
        data["report_view"] = report_view
        return data

    async def save_preferences(self, user_id: int, patch: dict[str, Any]) -> None:
        def save(c: sqlite3.Connection) -> None:
            row = c.execute(
                "SELECT payload FROM web_preferences WHERE user_id=?", (user_id,)
            ).fetchone()
            data = dict(json.loads(row[0])) if row else {}
            data.update(patch)
            if "timezone" in patch:
                data["report_view"] = {**data.get("report_view", {}), "timezone": patch["timezone"]}
            elif "timezone" in patch.get("report_view", {}):
                data["timezone"] = patch["report_view"]["timezone"]
            c.execute(
                "INSERT INTO web_preferences VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET payload=excluded.payload",
                (user_id, json.dumps(data)),
            )

        await self.run(save)
