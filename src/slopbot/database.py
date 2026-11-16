from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypeVar, cast

from slopbot import sessions, summaries
from slopbot.models import (
    MAX_ITEM_SHOP_WATCHES_PER_USER,
    MAX_SUBSCRIPTIONS_PER_USER,
    TOURNAMENT_GAME_MODES,
    CommandPolicy,
    CompletedRocketLeagueStreak,
    CurrentRocketLeagueStreak,
    DeliveryMethod,
    GetOnDelivery,
    ItemShopChange,
    ItemShopDelivery,
    ItemShopItem,
    ItemShopPrice,
    ItemShopWatch,
    KeywordNotificationMode,
    KeywordProfile,
    KeywordWatcher,
    NotificationDowntime,
    ResponseVisibility,
    RocketLeagueAccount,
    RocketLeagueEvent,
    RocketLeagueMatchEvent,
    RocketLeaguePeakMMR,
    RocketLeagueRecord,
    RocketLeagueTrackingConfig,
    Subscription,
    TournamentChannelAlert,
    TournamentOptInDelivery,
    TournamentUserAlert,
    validate_preferences,
    validate_tournament_alert,
)
from slopbot.rltracking import ResultInference, infer_activity_results

T = TypeVar("T")

GETON_COOLDOWN_SECONDS = 30
MAX_PENDING_GETON_REQUESTS_PER_SENDER = 20
MAX_ROCKET_LEAGUE_PLAYLIST_ID = 10_000
ROCKET_LEAGUE_RECORD_CATEGORIES = frozenset({"win_streak", "loss_streak"})
ROCKET_LEAGUE_RECORDS_PER_BOARD = 50
ROCKET_LEAGUE_MAIN_PLAYLISTS = frozenset({10, 11, 13})
ROCKET_LEAGUE_STREAK_CLASSES = frozenset({"main", "special"})
ROCKET_LEAGUE_ALL_TIME_SCOPES = frozenset({"main", "all"})
ROCKET_LEAGUE_STREAK_LEADERBOARD_SIZE = 15
ROCKET_LEAGUE_EVENT_NOTIFICATION_TYPES = frozenset(
    {
        "match",
        "rank_change",
        "peak_mmr",
        "session_streak",
        "all_time_streak",
        "leaderboard_movement",
        "streak_leaderboard_competitive",
        "streak_leaderboard_all_ranked",
    }
)


def _decode_team_sizes(value: str) -> tuple[int, ...]:
    decoded = json.loads(value)
    return tuple(cast(list[int], decoded))


def _decode_game_modes(value: str) -> tuple[str, ...]:
    decoded = json.loads(value)
    return tuple(cast(list[str], decoded))


class GetOnRateLimitError(ValueError):
    """Raised when a sender queues get-on requests too quickly."""


class SubscriptionLimitError(ValueError):
    """Raised when a member has reached the per-server alert limit."""


class RocketLeagueAccountConflictError(ValueError):
    """Raised when a Rocket League account is already linked to another Discord user."""


class ItemShopWatchLimitError(ValueError):
    """Raised when a member has too many item-shop watches in one server."""


class SubscriptionStore:
    """Small async wrapper around SQLite, serialized for predictable Pi workloads."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        await self._run(self._initialize_sync)

    async def save(
        self,
        *,
        guild_id: int,
        user_id: int,
        channel_id: int,
        threshold: int,
        notify_on_reach: bool,
        notify_on_drop: bool,
    ) -> Subscription:
        validate_preferences(
            threshold=threshold,
            notify_on_reach=notify_on_reach,
            notify_on_drop=notify_on_drop,
        )
        return await self._run(
            lambda connection: self._save_sync(
                connection,
                guild_id=guild_id,
                user_id=user_id,
                channel_id=channel_id,
                threshold=threshold,
                notify_on_reach=notify_on_reach,
                notify_on_drop=notify_on_drop,
            )
        )

    async def list_for_user(self, *, guild_id: int, user_id: int) -> list[Subscription]:
        return await self._run(
            lambda connection: self._select_many(
                connection,
                """
                SELECT id, guild_id, user_id, channel_id, threshold,
                       notify_on_reach, notify_on_drop
                FROM subscriptions
                WHERE guild_id = ? AND user_id = ?
                ORDER BY channel_id
                """,
                (guild_id, user_id),
            )
        )

    async def list_for_channel(self, *, guild_id: int, channel_id: int) -> list[Subscription]:
        return await self._run(
            lambda connection: self._select_many(
                connection,
                """
                SELECT id, guild_id, user_id, channel_id, threshold,
                       notify_on_reach, notify_on_drop
                FROM subscriptions
                WHERE guild_id = ? AND channel_id = ?
                ORDER BY id
                """,
                (guild_id, channel_id),
            )
        )

    async def get_for_user(
        self, *, subscription_id: int, guild_id: int, user_id: int
    ) -> Subscription | None:
        def operation(connection: sqlite3.Connection) -> Subscription | None:
            row = connection.execute(
                """
                SELECT id, guild_id, user_id, channel_id, threshold,
                       notify_on_reach, notify_on_drop
                FROM subscriptions
                WHERE id = ? AND guild_id = ? AND user_id = ?
                """,
                (subscription_id, guild_id, user_id),
            ).fetchone()
            return self._row_to_subscription(row) if row is not None else None

        return await self._run(operation)

    async def delete_for_user(self, *, subscription_id: int, guild_id: int, user_id: int) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                "DELETE FROM subscriptions WHERE id = ? AND guild_id = ? AND user_id = ?",
                (subscription_id, guild_id, user_id),
            )
            connection.commit()
            return cursor.rowcount == 1

        return await self._run(operation)

    async def delete_channel(self, *, guild_id: int, channel_id: int) -> int:
        def operation(connection: sqlite3.Connection) -> int:
            connection.execute(
                "UPDATE summary_deliveries SET status='cancelled' WHERE guild_id=? AND owner_id<=0 "
                "AND status='pending' AND (owner_id,cadence) IN "
                "(SELECT owner_id,cadence FROM summary_preferences "
                "WHERE guild_id=? AND owner_id<=0 AND json_extract(config,'$.channel_id')=?)",
                (guild_id, guild_id, channel_id),
            )
            connection.execute(
                "UPDATE summary_preferences SET config=json_set(config,'$.enabled',json('false'), "
                "'$.channel_id',NULL), next_at=0,revision=revision+1 "
                "WHERE guild_id=? AND owner_id<=0 "
                "AND json_extract(config,'$.channel_id')=?",
                (guild_id, channel_id),
            )
            connection.execute(
                "UPDATE play_sessions SET notified=1 WHERE guild_id=? AND owner_id=0 "
                "AND notified=0 AND enabled=1 AND EXISTS ("
                "SELECT 1 FROM play_session_preferences WHERE guild_id=? AND owner_id=0 "
                "AND channel_id=?)",
                (guild_id, guild_id, channel_id),
            )
            connection.execute(
                "UPDATE play_session_preferences SET enabled=0,channel_id=NULL "
                "WHERE guild_id=? AND owner_id=0 AND channel_id=?",
                (guild_id, channel_id),
            )
            cursor = connection.execute(
                "DELETE FROM subscriptions WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            )
            connection.execute(
                "DELETE FROM keyword_channels WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            )
            connection.execute(
                "UPDATE rocket_league_tracking_config SET streak_channel_id = NULL "
                "WHERE guild_id = ? AND streak_channel_id = ?",
                (guild_id, channel_id),
            )
            connection.execute(
                "UPDATE rocket_league_tracking_config SET leaderboard_channel_id = NULL "
                "WHERE guild_id = ? AND leaderboard_channel_id = ?",
                (guild_id, channel_id),
            )
            connection.execute(
                "DELETE FROM rocket_league_event_notification_config "
                "WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            )
            connection.execute(
                "DELETE FROM tournament_channel_alerts WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            )
            connection.execute(
                "DELETE FROM tournament_alert_deliveries "
                "WHERE target_type = 'channel' AND guild_id = ? AND target_id = ?",
                (guild_id, channel_id),
            )
            connection.execute(
                "DELETE FROM slopbot_status_channel_alerts WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            )
            connection.execute(
                "DELETE FROM command_policy_channels WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            )
            connection.commit()
            return cursor.rowcount

        return await self._run(operation)

    async def delete_guild(self, *, guild_id: int) -> int:
        def operation(connection: sqlite3.Connection) -> int:
            cursor = connection.execute("DELETE FROM subscriptions WHERE guild_id = ?", (guild_id,))
            connection.execute(
                "DELETE FROM notification_preferences WHERE guild_id = ?", (guild_id,)
            )
            connection.execute("DELETE FROM keyword_profiles WHERE guild_id = ?", (guild_id,))
            connection.execute("DELETE FROM geton_preferences WHERE guild_id = ?", (guild_id,))
            connection.execute("DELETE FROM geton_requests WHERE guild_id = ?", (guild_id,))
            connection.execute("DELETE FROM rocket_league_accounts WHERE guild_id = ?", (guild_id,))
            connection.execute(
                "DELETE FROM rocket_league_unlinked_accounts WHERE guild_id = ?", (guild_id,)
            )
            connection.execute(
                "DELETE FROM rocket_league_tracking_config WHERE guild_id = ?", (guild_id,)
            )
            connection.execute(
                "DELETE FROM rocket_league_leaderboard_playlists WHERE guild_id = ?", (guild_id,)
            )
            connection.execute(
                "DELETE FROM rocket_league_activity_samples WHERE guild_id = ?", (guild_id,)
            )
            connection.execute(
                "DELETE FROM rocket_league_rank_state_visits WHERE guild_id = ?", (guild_id,)
            )
            connection.execute(
                "DELETE FROM rocket_league_mmr_history WHERE guild_id = ?", (guild_id,)
            )
            connection.execute(
                "DELETE FROM rocket_league_match_history WHERE guild_id = ?", (guild_id,)
            )
            connection.execute("DELETE FROM play_sessions WHERE guild_id=?", (guild_id,))
            connection.execute("DELETE FROM summary_preferences WHERE guild_id=?", (guild_id,))
            connection.execute("DELETE FROM summary_deliveries WHERE guild_id=?", (guild_id,))
            connection.execute("DELETE FROM play_session_preferences WHERE guild_id=?", (guild_id,))
            connection.execute(
                "DELETE FROM rocket_league_event_notification_deliveries WHERE event_id IN "
                "(SELECT id FROM rocket_league_events WHERE guild_id = ?)",
                (guild_id,),
            )
            connection.execute("DELETE FROM rocket_league_events WHERE guild_id = ?", (guild_id,))
            connection.execute(
                "DELETE FROM rocket_league_streak_state WHERE guild_id = ?", (guild_id,)
            )
            connection.execute(
                "DELETE FROM rocket_league_leaderboard_snapshot WHERE guild_id = ?", (guild_id,)
            )
            connection.execute("DELETE FROM rocket_league_records WHERE guild_id = ?", (guild_id,))
            connection.execute(
                "DELETE FROM rocket_league_combined_streak_records WHERE guild_id = ?", (guild_id,)
            )
            connection.execute(
                "DELETE FROM rocket_league_combined_streak_state WHERE guild_id = ?", (guild_id,)
            )
            connection.execute(
                "DELETE FROM rocket_league_all_time_streak_records WHERE guild_id = ?", (guild_id,)
            )
            connection.execute(
                "DELETE FROM rocket_league_all_time_streak_state WHERE guild_id = ?", (guild_id,)
            )
            connection.execute(
                "DELETE FROM rocket_league_event_notification_config WHERE guild_id = ?",
                (guild_id,),
            )
            connection.execute("DELETE FROM rocket_league_peak_mmr WHERE guild_id = ?", (guild_id,))
            connection.execute("DELETE FROM item_shop_watches WHERE guild_id = ?", (guild_id,))
            connection.execute("DELETE FROM item_shop_deliveries WHERE guild_id = ?", (guild_id,))
            connection.execute("DELETE FROM tournament_user_alerts WHERE guild_id = ?", (guild_id,))
            connection.execute(
                "DELETE FROM tournament_channel_alerts WHERE guild_id = ?", (guild_id,)
            )
            connection.execute(
                "DELETE FROM tournament_alert_deliveries WHERE guild_id = ?", (guild_id,)
            )
            connection.execute(
                "DELETE FROM tournament_opt_in_offers WHERE guild_id = ?", (guild_id,)
            )
            connection.execute(
                "DELETE FROM slopbot_status_user_alerts WHERE guild_id = ?", (guild_id,)
            )
            connection.execute(
                "DELETE FROM slopbot_status_channel_alerts WHERE guild_id = ?", (guild_id,)
            )
            connection.execute("DELETE FROM command_policies WHERE guild_id = ?", (guild_id,))
            connection.commit()
            return cursor.rowcount

        return await self._run(operation)

    async def get_command_policy(self, *, guild_id: int, command_name: str) -> CommandPolicy:
        def operation(connection: sqlite3.Connection) -> CommandPolicy:
            row = connection.execute(
                "SELECT default_visibility, channel_restricted FROM command_policies "
                "WHERE guild_id = ? AND command_name = ?",
                (guild_id, command_name),
            ).fetchone()
            channels = connection.execute(
                "SELECT channel_id FROM command_policy_channels "
                "WHERE guild_id = ? AND command_name = ? ORDER BY channel_id",
                (guild_id, command_name),
            ).fetchall()
            return CommandPolicy(
                command_name=command_name,
                default_visibility=(
                    ResponseVisibility(row[0]) if row is not None else ResponseVisibility.PUBLIC
                ),
                channel_restricted=bool(row[1]) if row is not None else False,
                channel_ids=tuple(cast(int, channel[0]) for channel in channels),
            )

        return await self._run(operation)

    async def list_command_policies(self, *, guild_id: int) -> list[CommandPolicy]:
        def operation(connection: sqlite3.Connection) -> list[CommandPolicy]:
            rows = connection.execute(
                "SELECT command_name, default_visibility, channel_restricted "
                "FROM command_policies WHERE guild_id = ? ORDER BY command_name",
                (guild_id,),
            ).fetchall()
            channels = connection.execute(
                "SELECT command_name, channel_id FROM command_policy_channels "
                "WHERE guild_id = ? ORDER BY command_name, channel_id",
                (guild_id,),
            ).fetchall()
            channel_ids: dict[str, list[int]] = {}
            for command_name, channel_id in channels:
                channel_ids.setdefault(cast(str, command_name), []).append(cast(int, channel_id))
            return [
                CommandPolicy(
                    command_name=cast(str, row[0]),
                    default_visibility=ResponseVisibility(cast(str, row[1])),
                    channel_restricted=bool(row[2]),
                    channel_ids=tuple(channel_ids.get(cast(str, row[0]), [])),
                )
                for row in rows
            ]

        return await self._run(operation)

    async def replace_command_policy(
        self,
        *,
        guild_id: int,
        command_name: str,
        visibility: ResponseVisibility,
        restricted: bool,
        channel_ids: Sequence[int],
    ) -> None:
        """Save a complete policy atomically, without transient allow/deny states."""

        def operation(connection: sqlite3.Connection) -> None:
            with connection:
                connection.execute(
                    "INSERT INTO command_policies "
                    "(guild_id, command_name, default_visibility, channel_restricted) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT (guild_id, command_name) DO UPDATE SET "
                    "default_visibility=excluded.default_visibility, "
                    "channel_restricted=excluded.channel_restricted",
                    (guild_id, command_name, visibility.value, int(restricted)),
                )
                connection.execute(
                    "DELETE FROM command_policy_channels WHERE guild_id=? AND command_name=?",
                    (guild_id, command_name),
                )
                if restricted:
                    connection.executemany(
                        "INSERT INTO command_policy_channels (guild_id, command_name, channel_id) "
                        "VALUES (?, ?, ?)",
                        ((guild_id, command_name, c) for c in dict.fromkeys(channel_ids)),
                    )

        await self._run(operation)

    async def set_command_visibility(
        self,
        *,
        guild_id: int,
        command_name: str,
        visibility: ResponseVisibility,
    ) -> CommandPolicy:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO command_policies (guild_id, command_name, default_visibility)
                VALUES (?, ?, ?)
                ON CONFLICT (guild_id, command_name) DO UPDATE SET
                    default_visibility = excluded.default_visibility,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id, command_name, visibility.value),
            )
            connection.commit()

        await self._run(operation)
        return await self.get_command_policy(guild_id=guild_id, command_name=command_name)

    async def allow_command_channel(
        self, *, guild_id: int, command_name: str, channel_id: int
    ) -> CommandPolicy:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO command_policies (guild_id, command_name, channel_restricted)
                VALUES (?, ?, 1)
                ON CONFLICT (guild_id, command_name) DO UPDATE SET
                    channel_restricted = 1,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id, command_name),
            )
            connection.execute(
                "INSERT OR IGNORE INTO command_policy_channels "
                "(guild_id, command_name, channel_id) VALUES (?, ?, ?)",
                (guild_id, command_name, channel_id),
            )
            connection.commit()

        await self._run(operation)
        return await self.get_command_policy(guild_id=guild_id, command_name=command_name)

    async def disallow_command_channel(
        self, *, guild_id: int, command_name: str, channel_id: int
    ) -> CommandPolicy:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO command_policies (guild_id, command_name, channel_restricted)
                VALUES (?, ?, 1)
                ON CONFLICT (guild_id, command_name) DO UPDATE SET
                    channel_restricted = 1,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id, command_name),
            )
            connection.execute(
                "DELETE FROM command_policy_channels "
                "WHERE guild_id = ? AND command_name = ? AND channel_id = ?",
                (guild_id, command_name, channel_id),
            )
            connection.commit()

        await self._run(operation)
        return await self.get_command_policy(guild_id=guild_id, command_name=command_name)

    async def clear_command_channel_restrictions(
        self, *, guild_id: int, command_name: str
    ) -> CommandPolicy:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO command_policies (guild_id, command_name, channel_restricted)
                VALUES (?, ?, 0)
                ON CONFLICT (guild_id, command_name) DO UPDATE SET
                    channel_restricted = 0,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id, command_name),
            )
            connection.execute(
                "DELETE FROM command_policy_channels WHERE guild_id = ? AND command_name = ?",
                (guild_id, command_name),
            )
            connection.commit()

        await self._run(operation)
        return await self.get_command_policy(guild_id=guild_id, command_name=command_name)

    async def command_channel_is_allowed(
        self,
        *,
        guild_id: int,
        command_name: str,
        channel_ids: Sequence[int],
    ) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            row = connection.execute(
                "SELECT channel_restricted FROM command_policies "
                "WHERE guild_id = ? AND command_name = ?",
                (guild_id, command_name),
            ).fetchone()
            if row is None or not bool(row[0]):
                return True
            return any(
                connection.execute(
                    "SELECT 1 FROM command_policy_channels "
                    "WHERE guild_id = ? AND command_name = ? AND channel_id = ?",
                    (guild_id, command_name, channel_id),
                ).fetchone()
                is not None
                for channel_id in channel_ids
            )

        return await self._run(operation)

    async def _delete_where(self, clause: str, parameters: tuple[int, ...]) -> int:
        def operation(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(f"DELETE FROM subscriptions WHERE {clause}", parameters)
            connection.commit()
            return cursor.rowcount

        return await self._run(operation)

    async def get_delivery_method(self, *, guild_id: int, user_id: int) -> DeliveryMethod:
        def operation(connection: sqlite3.Connection) -> DeliveryMethod:
            row = connection.execute(
                "SELECT delivery_method FROM notification_preferences "
                "WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            return DeliveryMethod(row[0]) if row is not None else DeliveryMethod.DISCORD

        return await self._run(operation)

    async def set_delivery_method(
        self, *, guild_id: int, user_id: int, delivery_method: DeliveryMethod
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "DELETE FROM telegram_link_codes WHERE expires_at < ?", (int(time.time()),)
            )
            connection.execute(
                """
                INSERT INTO notification_preferences (guild_id, user_id, delivery_method)
                VALUES (?, ?, ?)
                ON CONFLICT (guild_id, user_id) DO UPDATE SET
                    delivery_method = excluded.delivery_method,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id, user_id, delivery_method.value),
            )
            connection.commit()

        await self._run(operation)

    async def get_notification_downtime(
        self, *, guild_id: int, user_id: int
    ) -> NotificationDowntime:
        def operation(connection: sqlite3.Connection) -> NotificationDowntime:
            row = connection.execute(
                "SELECT downtime_enabled, downtime_start_minute, downtime_end_minute, "
                "downtime_timezone FROM notification_preferences "
                "WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            if row is None:
                return NotificationDowntime()
            return NotificationDowntime(
                enabled=bool(row[0]),
                start_minute=cast(int, row[1]),
                end_minute=cast(int, row[2]),
                timezone=cast(str, row[3]),
            )

        return await self._run(operation)

    async def set_notification_downtime(
        self,
        *,
        guild_id: int,
        user_id: int,
        downtime: NotificationDowntime,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO notification_preferences
                    (guild_id, user_id, downtime_enabled, downtime_start_minute,
                     downtime_end_minute, downtime_timezone)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (guild_id, user_id) DO UPDATE SET
                    downtime_enabled = excluded.downtime_enabled,
                    downtime_start_minute = excluded.downtime_start_minute,
                    downtime_end_minute = excluded.downtime_end_minute,
                    downtime_timezone = excluded.downtime_timezone,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    guild_id,
                    user_id,
                    int(downtime.enabled),
                    downtime.start_minute,
                    downtime.end_minute,
                    downtime.timezone,
                ),
            )
            connection.commit()

        await self._run(operation)

    async def notification_downtime_active(self, *, guild_id: int, user_id: int) -> bool:
        downtime = await self.get_notification_downtime(guild_id=guild_id, user_id=user_id)
        return downtime.active_at()

    async def get_telegram_chat_id(self, *, discord_user_id: int) -> int | None:
        def operation(connection: sqlite3.Connection) -> int | None:
            row = connection.execute(
                "SELECT telegram_chat_id FROM telegram_links WHERE discord_user_id = ?",
                (discord_user_id,),
            ).fetchone()
            return cast(int, row[0]) if row is not None else None

        return await self._run(operation)

    async def save_telegram_link_code(
        self, *, discord_user_id: int, code_hash: str, expires_at: int
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "DELETE FROM telegram_link_codes WHERE discord_user_id = ?", (discord_user_id,)
            )
            connection.execute(
                "INSERT INTO telegram_link_codes (code_hash, discord_user_id, expires_at) "
                "VALUES (?, ?, ?)",
                (code_hash, discord_user_id, expires_at),
            )
            connection.commit()

        await self._run(operation)

    async def consume_telegram_link_code(
        self, *, code_hash: str, telegram_chat_id: int
    ) -> int | None:
        def operation(connection: sqlite3.Connection) -> int | None:
            row = connection.execute(
                "SELECT discord_user_id, expires_at FROM telegram_link_codes WHERE code_hash = ?",
                (code_hash,),
            ).fetchone()
            connection.execute("DELETE FROM telegram_link_codes WHERE code_hash = ?", (code_hash,))
            if row is None or cast(int, row[1]) < int(time.time()):
                connection.commit()
                return None
            discord_user_id = cast(int, row[0])
            prior_link = connection.execute(
                "SELECT discord_user_id FROM telegram_links WHERE telegram_chat_id = ?",
                (telegram_chat_id,),
            ).fetchone()
            connection.execute(
                "DELETE FROM telegram_links WHERE telegram_chat_id = ?", (telegram_chat_id,)
            )
            if prior_link is not None and cast(int, prior_link[0]) != discord_user_id:
                connection.execute(
                    "UPDATE notification_preferences SET delivery_method = CASE delivery_method "
                    "WHEN 'telegram_pwa' THEN 'pwa' WHEN 'all' THEN 'discord_pwa' "
                    "ELSE 'discord' END, "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE user_id = ? AND delivery_method IN "
                    "('telegram', 'both', 'telegram_pwa', 'all')",
                    (cast(int, prior_link[0]),),
                )
            connection.execute(
                """
                INSERT INTO telegram_links (discord_user_id, telegram_chat_id)
                VALUES (?, ?)
                ON CONFLICT (discord_user_id) DO UPDATE SET
                    telegram_chat_id = excluded.telegram_chat_id,
                    linked_at = CURRENT_TIMESTAMP
                """,
                (discord_user_id, telegram_chat_id),
            )
            connection.commit()
            return discord_user_id

        return await self._run(operation)

    async def unlink_telegram(self, *, discord_user_id: int) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                "DELETE FROM telegram_links WHERE discord_user_id = ?", (discord_user_id,)
            )
            connection.execute(
                "DELETE FROM telegram_link_codes WHERE discord_user_id = ?", (discord_user_id,)
            )
            connection.execute(
                "UPDATE notification_preferences SET delivery_method = CASE delivery_method "
                "WHEN 'telegram_pwa' THEN 'pwa' WHEN 'all' THEN 'discord_pwa' "
                "ELSE 'discord' END, "
                "updated_at = CURRENT_TIMESTAMP "
                "WHERE user_id = ? AND delivery_method IN "
                "('telegram', 'both', 'telegram_pwa', 'all')",
                (discord_user_id,),
            )
            connection.commit()
            return cursor.rowcount == 1

        return await self._run(operation)

    async def save_item_shop_watch(
        self,
        *,
        guild_id: int,
        user_id: int,
        query: str,
        max_price: int | None,
    ) -> ItemShopWatch:
        cleaned_query = " ".join(query.split())
        if len(cleaned_query) > 100:
            raise ValueError("item-shop searches may contain at most 100 characters")
        if max_price is not None and not 0 <= max_price <= 1_000_000:
            raise ValueError("maximum price must be between 0 and 1,000,000")
        normalized_query = cleaned_query.casefold()
        stored_max_price = max_price if max_price is not None else -1

        def operation(connection: sqlite3.Connection) -> ItemShopWatch:
            existing = connection.execute(
                "SELECT id FROM item_shop_watches WHERE guild_id = ? AND user_id = ? "
                "AND normalized_query = ? AND max_price = ?",
                (guild_id, user_id, normalized_query, stored_max_price),
            ).fetchone()
            if existing is None:
                count = cast(
                    int,
                    connection.execute(
                        "SELECT COUNT(*) FROM item_shop_watches WHERE guild_id = ? AND user_id = ?",
                        (guild_id, user_id),
                    ).fetchone()[0],
                )
                if count >= MAX_ITEM_SHOP_WATCHES_PER_USER:
                    raise ItemShopWatchLimitError(
                        f"You can have up to {MAX_ITEM_SHOP_WATCHES_PER_USER} item-shop watches "
                        "in one server."
                    )
                cursor = connection.execute(
                    "INSERT INTO item_shop_watches "
                    "(guild_id, user_id, query, normalized_query, max_price) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (guild_id, user_id, cleaned_query, normalized_query, stored_max_price),
                )
                watch_id = cast(int, cursor.lastrowid)
            else:
                watch_id = cast(int, existing[0])
            connection.commit()
            return ItemShopWatch(watch_id, guild_id, user_id, cleaned_query, max_price)

        return await self._run(operation)

    async def list_item_shop_watches(self, *, guild_id: int, user_id: int) -> list[ItemShopWatch]:
        def operation(connection: sqlite3.Connection) -> list[ItemShopWatch]:
            rows = connection.execute(
                "SELECT id, guild_id, user_id, query, max_price FROM item_shop_watches "
                "WHERE guild_id = ? AND user_id = ? ORDER BY id",
                (guild_id, user_id),
            ).fetchall()
            return [
                ItemShopWatch(
                    cast(int, row[0]),
                    cast(int, row[1]),
                    cast(int, row[2]),
                    cast(str, row[3]),
                    None if cast(int, row[4]) < 0 else cast(int, row[4]),
                )
                for row in rows
            ]

        return await self._run(operation)

    async def delete_item_shop_watch(self, *, watch_id: int, guild_id: int, user_id: int) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                "DELETE FROM item_shop_watches WHERE id = ? AND guild_id = ? AND user_id = ?",
                (watch_id, guild_id, user_id),
            )
            connection.commit()
            return cursor.rowcount == 1

        return await self._run(operation)

    async def replace_item_shop_snapshot(
        self, *, items: Sequence[ItemShopItem], observed_at: int
    ) -> int:
        unique_items = {item.key: item for item in items}

        def operation(connection: sqlite3.Connection) -> int:
            initialized = connection.execute(
                "SELECT initialized FROM item_shop_state WHERE singleton = 1"
            ).fetchone()
            previous_rows = connection.execute(
                "SELECT item_key, fingerprint, payload FROM item_shop_snapshot"
            ).fetchall()
            previous = {
                cast(str, row[0]): (cast(str, row[1]), cast(str, row[2])) for row in previous_rows
            }
            encoded = {key: self._encode_item_shop_item(item) for key, item in unique_items.items()}
            fingerprints = {
                key: hashlib.sha256(payload.encode("utf-8")).hexdigest()
                for key, payload in encoded.items()
            }
            changes: list[tuple[str, str, str | None]] = []
            if initialized is not None and cast(int, initialized[0]) == 1:
                for key, payload in encoded.items():
                    if key not in previous:
                        changes.append(("added", payload, None))
                    elif previous[key][0] != fingerprints[key]:
                        changes.append(("updated", payload, previous[key][1]))
                for key, (_fingerprint, payload) in previous.items():
                    if key not in encoded:
                        changes.append(("removed", payload, None))

            connection.execute("DELETE FROM item_shop_snapshot")
            connection.executemany(
                "INSERT INTO item_shop_snapshot "
                "(item_key, fingerprint, payload, searchable_text, observed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        key,
                        fingerprints[key],
                        encoded[key],
                        item.searchable_text,
                        observed_at,
                    )
                    for key, item in unique_items.items()
                ],
            )
            watches = connection.execute(
                "SELECT guild_id, user_id, normalized_query, max_price FROM item_shop_watches"
            ).fetchall()
            for change_type, payload, old_payload in changes:
                cursor = connection.execute(
                    "INSERT INTO item_shop_changes (change_type, payload, observed_at) "
                    "VALUES (?, ?, ?)",
                    (change_type, payload, observed_at),
                )
                change_id = cast(int, cursor.lastrowid)
                item = self._decode_item_shop_item(payload)
                old_item = (
                    self._decode_item_shop_item(old_payload) if old_payload is not None else None
                )
                recipients = {
                    (cast(int, row[0]), cast(int, row[1]))
                    for row in watches
                    if self._item_shop_watch_matches(
                        item,
                        query=cast(str, row[2]),
                        max_price=cast(int, row[3]),
                    )
                    or (
                        old_item is not None
                        and self._item_shop_watch_matches(
                            old_item,
                            query=cast(str, row[2]),
                            max_price=cast(int, row[3]),
                        )
                    )
                }
                connection.executemany(
                    "INSERT INTO item_shop_deliveries "
                    "(change_id, guild_id, user_id, next_attempt_at) VALUES (?, ?, ?, ?)",
                    [
                        (change_id, guild_id, user_id, observed_at)
                        for guild_id, user_id in recipients
                    ],
                )
            connection.execute(
                "INSERT INTO item_shop_state (singleton, initialized, checked_at) VALUES (1, 1, ?) "
                "ON CONFLICT (singleton) DO UPDATE SET initialized = 1, "
                "checked_at = excluded.checked_at",
                (observed_at,),
            )
            connection.commit()
            return len(changes)

        return await self._run(operation)

    async def list_item_shop_snapshot(self) -> list[ItemShopItem]:
        def operation(connection: sqlite3.Connection) -> list[ItemShopItem]:
            rows = connection.execute(
                "SELECT payload FROM item_shop_snapshot ORDER BY searchable_text, item_key"
            ).fetchall()
            return [self._decode_item_shop_item(cast(str, row[0])) for row in rows]

        return await self._run(operation)

    async def list_item_shop_changes(
        self, *, since: int, query: str = "", limit: int = 25
    ) -> list[ItemShopChange]:
        normalized_query = " ".join(query.split()).casefold()

        def operation(connection: sqlite3.Connection) -> list[ItemShopChange]:
            rows = connection.execute(
                "SELECT id, change_type, payload, observed_at FROM item_shop_changes "
                "WHERE observed_at >= ? ORDER BY observed_at DESC, id DESC LIMIT 250",
                (since,),
            ).fetchall()
            result: list[ItemShopChange] = []
            for row in rows:
                item = self._decode_item_shop_item(cast(str, row[2]))
                if normalized_query and not all(
                    term in item.searchable_text for term in normalized_query.split()
                ):
                    continue
                result.append(
                    ItemShopChange(
                        cast(int, row[0]),
                        cast(str, row[1]),
                        item,
                        cast(int, row[3]),
                    )
                )
                if len(result) >= limit:
                    break
            return result

        return await self._run(operation)

    async def list_due_item_shop_deliveries(
        self, *, now: int, limit: int = 100
    ) -> list[ItemShopDelivery]:
        def operation(connection: sqlite3.Connection) -> list[ItemShopDelivery]:
            rows = connection.execute(
                "SELECT c.id, c.change_type, c.payload, c.observed_at, "
                "d.guild_id, d.user_id, d.attempt_count "
                "FROM item_shop_deliveries d "
                "JOIN item_shop_changes c ON c.id = d.change_id "
                "WHERE d.delivered = 0 AND d.next_attempt_at <= ? "
                "ORDER BY d.next_attempt_at, c.id LIMIT ?",
                (now, limit),
            ).fetchall()
            return [
                ItemShopDelivery(
                    ItemShopChange(
                        cast(int, row[0]),
                        cast(str, row[1]),
                        self._decode_item_shop_item(cast(str, row[2])),
                        cast(int, row[3]),
                    ),
                    cast(int, row[4]),
                    cast(int, row[5]),
                    cast(int, row[6]),
                )
                for row in rows
            ]

        return await self._run(operation)

    async def finish_item_shop_delivery(
        self, *, change_id: int, guild_id: int, user_id: int, detail: str = ""
    ) -> None:
        await self._run(
            lambda connection: self._finish_item_shop_delivery_sync(
                connection,
                change_id=change_id,
                guild_id=guild_id,
                user_id=user_id,
                detail=detail,
            )
        )

    async def retry_item_shop_delivery(
        self,
        *,
        change_id: int,
        guild_id: int,
        user_id: int,
        next_attempt_at: int,
        detail: str,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE item_shop_deliveries SET attempt_count = attempt_count + 1, "
                "next_attempt_at = ?, last_error = ? "
                "WHERE change_id = ? AND guild_id = ? AND user_id = ? AND delivered = 0",
                (next_attempt_at, detail[:500], change_id, guild_id, user_id),
            )
            connection.commit()

        await self._run(operation)

    async def prune_item_shop_changes(self, *, before: int) -> int:
        def operation(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                "DELETE FROM item_shop_changes WHERE observed_at < ?", (before,)
            )
            connection.commit()
            return cursor.rowcount

        return await self._run(operation)

    async def save_rocket_league_account(
        self,
        *,
        guild_id: int,
        discord_user_id: int,
        player_id: str,
        player_name: str,
    ) -> RocketLeagueAccount:
        def operation(connection: sqlite3.Connection) -> RocketLeagueAccount:
            owner = connection.execute(
                "SELECT discord_user_id FROM rocket_league_accounts "
                "WHERE guild_id = ? AND player_id = ?",
                (guild_id, player_id),
            ).fetchone()
            if owner is not None and cast(int, owner[0]) != discord_user_id:
                raise RocketLeagueAccountConflictError(
                    "That Rocket League account is already linked to another Discord user."
                )
            prior = connection.execute(
                "SELECT player_id FROM rocket_league_accounts "
                "WHERE guild_id = ? AND discord_user_id = ?",
                (guild_id, discord_user_id),
            ).fetchone()
            if prior is not None and cast(str, prior[0]) != player_id:
                connection.execute(
                    "DELETE FROM play_sessions WHERE guild_id=? AND user_id=?",
                    (guild_id, discord_user_id),
                )
                connection.execute(
                    "DELETE FROM rocket_league_event_notification_deliveries "
                    "WHERE event_id IN (SELECT id FROM rocket_league_events "
                    "WHERE guild_id = ? AND discord_user_id = ?)",
                    (guild_id, discord_user_id),
                )
                for table in (
                    "rocket_league_activity_samples",
                    "rocket_league_mmr_history",
                    "rocket_league_match_history",
                    "rocket_league_events",
                    "rocket_league_rank_state_visits",
                    "rocket_league_streak_state",
                    "rocket_league_combined_streak_state",
                    "rocket_league_all_time_streak_state",
                    "rocket_league_leaderboard_snapshot",
                ):
                    connection.execute(
                        f"DELETE FROM {table} WHERE guild_id = ? AND discord_user_id = ?",
                        (guild_id, discord_user_id),
                    )
            unlinked = connection.execute(
                "SELECT id FROM rocket_league_unlinked_accounts "
                "WHERE guild_id = ? AND player_id = ?",
                (guild_id, player_id),
            ).fetchone()
            if unlinked is not None:
                unlinked_tracking_id = -cast(int, unlinked[0])
                connection.execute(
                    "UPDATE play_sessions SET user_id=? WHERE guild_id=? AND user_id=?",
                    (discord_user_id, guild_id, unlinked_tracking_id),
                )
                connection.execute(
                    "DELETE FROM rocket_league_unlinked_accounts WHERE id = ?",
                    (cast(int, unlinked[0]),),
                )
                connection.execute(
                    "DELETE FROM rocket_league_event_notification_deliveries "
                    "WHERE event_id IN (SELECT id FROM rocket_league_events "
                    "WHERE guild_id = ? AND discord_user_id = ?)",
                    (guild_id, discord_user_id),
                )
                for table in (
                    "rocket_league_activity_samples",
                    "rocket_league_rank_state_visits",
                    "rocket_league_streak_state",
                    "rocket_league_combined_streak_state",
                    "rocket_league_all_time_streak_state",
                    "rocket_league_leaderboard_snapshot",
                    "rocket_league_events",
                ):
                    connection.execute(
                        f"DELETE FROM {table} WHERE guild_id = ? AND discord_user_id = ?",
                        (guild_id, discord_user_id),
                    )
                    connection.execute(
                        f"UPDATE {table} SET discord_user_id = ? "
                        "WHERE guild_id = ? AND discord_user_id = ?",
                        (discord_user_id, guild_id, unlinked_tracking_id),
                    )
                connection.execute(
                    "UPDATE rocket_league_match_history SET discord_user_id = ? "
                    "WHERE guild_id = ? AND discord_user_id = ?",
                    (discord_user_id, guild_id, unlinked_tracking_id),
                )
                for table in ("rocket_league_mmr_history", "rocket_league_peak_mmr"):
                    connection.execute(
                        f"UPDATE {table} SET discord_user_id = ? "
                        "WHERE guild_id = ? AND discord_user_id = ?",
                        (discord_user_id, guild_id, unlinked_tracking_id),
                    )
                connection.execute(
                    "UPDATE rocket_league_all_time_streak_records SET discord_user_id = ? "
                    "WHERE guild_id = ? AND discord_user_id = ?",
                    (discord_user_id, guild_id, unlinked_tracking_id),
                )
            connection.execute(
                """
                INSERT INTO rocket_league_accounts
                    (guild_id, discord_user_id, player_id, player_name)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (guild_id, discord_user_id) DO UPDATE SET
                    player_id = excluded.player_id,
                    player_name = excluded.player_name,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id, discord_user_id, player_id, player_name),
            )
            connection.commit()
            return RocketLeagueAccount(guild_id, discord_user_id, player_id, player_name)

        return await self._run(operation)

    async def get_rocket_league_account(
        self, *, guild_id: int, discord_user_id: int
    ) -> RocketLeagueAccount | None:
        def operation(connection: sqlite3.Connection) -> RocketLeagueAccount | None:
            row = connection.execute(
                "SELECT guild_id, discord_user_id, player_id, player_name "
                "FROM rocket_league_accounts WHERE guild_id = ? AND discord_user_id = ?",
                (guild_id, discord_user_id),
            ).fetchone()
            return RocketLeagueAccount(*cast(tuple[int, int, str, str], row)) if row else None

        return await self._run(operation)

    async def list_rocket_league_accounts(
        self, *, guild_id: int, discord_user_ids: Sequence[int]
    ) -> list[RocketLeagueAccount]:
        wanted = set(discord_user_ids)
        if not wanted:
            return []

        def operation(connection: sqlite3.Connection) -> list[RocketLeagueAccount]:
            rows = connection.execute(
                "SELECT guild_id, discord_user_id, player_id, player_name "
                "FROM rocket_league_accounts WHERE guild_id = ?",
                (guild_id,),
            ).fetchall()
            return [
                RocketLeagueAccount(*cast(tuple[int, int, str, str], row))
                for row in rows
                if cast(int, row[1]) in wanted
            ]

        return await self._run(operation)

    async def list_all_rocket_league_accounts(self, *, guild_id: int) -> list[RocketLeagueAccount]:
        def operation(connection: sqlite3.Connection) -> list[RocketLeagueAccount]:
            rows = connection.execute(
                "SELECT guild_id, discord_user_id, player_id, player_name "
                "FROM rocket_league_accounts WHERE guild_id = ? ORDER BY discord_user_id",
                (guild_id,),
            ).fetchall()
            linked = [RocketLeagueAccount(*cast(tuple[int, int, str, str], row)) for row in rows]
            unlinked_rows = connection.execute(
                "SELECT id, player_id, player_name FROM rocket_league_unlinked_accounts "
                "WHERE guild_id = ? ORDER BY player_name COLLATE NOCASE, id",
                (guild_id,),
            ).fetchall()
            return linked + [
                RocketLeagueAccount(
                    guild_id, -cast(int, row[0]), cast(str, row[1]), cast(str, row[2]), True
                )
                for row in unlinked_rows
            ]

        return await self._run(operation)

    async def list_linked_rocket_league_accounts(self) -> list[RocketLeagueAccount]:
        def operation(connection: sqlite3.Connection) -> list[RocketLeagueAccount]:
            rows = connection.execute(
                "SELECT guild_id, discord_user_id, player_id, player_name "
                "FROM rocket_league_accounts ORDER BY guild_id, discord_user_id"
            ).fetchall()
            linked = [RocketLeagueAccount(*cast(tuple[int, int, str, str], row)) for row in rows]
            unlinked_rows = connection.execute(
                "SELECT guild_id, id, player_id, player_name FROM rocket_league_unlinked_accounts "
                "ORDER BY guild_id, id"
            ).fetchall()
            return linked + [
                RocketLeagueAccount(
                    cast(int, row[0]),
                    -cast(int, row[1]),
                    cast(str, row[2]),
                    cast(str, row[3]),
                    True,
                )
                for row in unlinked_rows
            ]

        return await self._run(operation)

    async def save_unlinked_rocket_league_account(
        self, *, guild_id: int, player_id: str, player_name: str
    ) -> RocketLeagueAccount:
        def operation(connection: sqlite3.Connection) -> RocketLeagueAccount:
            linked = connection.execute(
                "SELECT 1 FROM rocket_league_accounts WHERE guild_id = ? AND player_id = ?",
                (guild_id, player_id),
            ).fetchone()
            if linked is not None:
                raise RocketLeagueAccountConflictError(
                    "That Rocket League account is already linked to a Discord user."
                )
            row = connection.execute(
                "SELECT id FROM rocket_league_unlinked_accounts "
                "WHERE guild_id = ? AND player_id = ?",
                (guild_id, player_id),
            ).fetchone()
            if row is None:
                cursor = connection.execute(
                    "INSERT INTO rocket_league_unlinked_accounts "
                    "(guild_id, player_id, player_name) VALUES (?, ?, ?)",
                    (guild_id, player_id, player_name),
                )
                account_id = cast(int, cursor.lastrowid)
            else:
                account_id = cast(int, row[0])
                connection.execute(
                    "UPDATE rocket_league_unlinked_accounts SET player_name = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (player_name, account_id),
                )
            connection.commit()
            return RocketLeagueAccount(guild_id, -account_id, player_id, player_name, True)

        return await self._run(operation)

    async def get_unlinked_rocket_league_account(
        self, *, guild_id: int, player_id: str
    ) -> RocketLeagueAccount | None:
        def operation(connection: sqlite3.Connection) -> RocketLeagueAccount | None:
            row = connection.execute(
                "SELECT id, player_id, player_name FROM rocket_league_unlinked_accounts "
                "WHERE guild_id = ? AND player_id = ?",
                (guild_id, player_id),
            ).fetchone()
            return (
                RocketLeagueAccount(
                    guild_id, -cast(int, row[0]), cast(str, row[1]), cast(str, row[2]), True
                )
                if row
                else None
            )

        return await self._run(operation)

    async def unlink_unlinked_rocket_league_account(self, *, guild_id: int, player_id: str) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            row = connection.execute(
                "SELECT id FROM rocket_league_unlinked_accounts "
                "WHERE guild_id = ? AND player_id = ?",
                (guild_id, player_id),
            ).fetchone()
            if row is None:
                return False
            tracking_id = -cast(int, row[0])
            connection.execute(
                "DELETE FROM play_sessions WHERE guild_id=? AND user_id=?", (guild_id, tracking_id)
            )
            connection.execute(
                "DELETE FROM rocket_league_unlinked_accounts WHERE id = ?", (cast(int, row[0]),)
            )
            connection.execute(
                "DELETE FROM rocket_league_event_notification_deliveries "
                "WHERE event_id IN (SELECT id FROM rocket_league_events "
                "WHERE guild_id = ? AND discord_user_id = ?)",
                (guild_id, tracking_id),
            )
            for table in (
                "rocket_league_activity_samples",
                "rocket_league_rank_state_visits",
                "rocket_league_match_history",
                "rocket_league_events",
                "rocket_league_streak_state",
                "rocket_league_combined_streak_state",
                "rocket_league_all_time_streak_state",
            ):
                connection.execute(
                    f"DELETE FROM {table} WHERE guild_id = ? AND discord_user_id = ?",
                    (guild_id, tracking_id),
                )
            connection.execute(
                "DELETE FROM rocket_league_leaderboard_snapshot "
                "WHERE guild_id = ? AND discord_user_id = ?",
                (guild_id, tracking_id),
            )
            connection.commit()
            return True

        return await self._run(operation)

    async def unlink_rocket_league_account(self, *, guild_id: int, discord_user_id: int) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                "DELETE FROM rocket_league_accounts WHERE guild_id = ? AND discord_user_id = ?",
                (guild_id, discord_user_id),
            )
            connection.execute(
                "DELETE FROM rocket_league_event_notification_deliveries "
                "WHERE event_id IN (SELECT id FROM rocket_league_events "
                "WHERE guild_id = ? AND discord_user_id = ?)",
                (guild_id, discord_user_id),
            )
            for table in (
                "rocket_league_activity_samples",
                "rocket_league_rank_state_visits",
                "rocket_league_match_history",
                "rocket_league_events",
                "rocket_league_streak_state",
                "rocket_league_combined_streak_state",
                "rocket_league_all_time_streak_state",
            ):
                connection.execute(
                    f"DELETE FROM {table} WHERE guild_id = ? AND discord_user_id = ?",
                    (guild_id, discord_user_id),
                )
            connection.execute(
                "DELETE FROM rocket_league_leaderboard_snapshot "
                "WHERE guild_id = ? AND discord_user_id = ?",
                (guild_id, discord_user_id),
            )
            connection.execute(
                "DELETE FROM play_sessions WHERE guild_id=? AND user_id=?",
                (guild_id, discord_user_id),
            )
            connection.commit()
            return cursor.rowcount == 1

        return await self._run(operation)

    async def get_rocket_league_tracking_config(
        self, *, guild_id: int
    ) -> RocketLeagueTrackingConfig:
        def operation(connection: sqlite3.Connection) -> RocketLeagueTrackingConfig:
            row = connection.execute(
                "SELECT guild_id, streak_channel_id, streak_threshold, inactivity_seconds, "
                "leaderboard_channel_id "
                "FROM rocket_league_tracking_config WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
            if row is None:
                return RocketLeagueTrackingConfig(guild_id, None, 3, 1800, None, (11,))
            playlist_rows = connection.execute(
                "SELECT playlist FROM rocket_league_leaderboard_playlists "
                "WHERE guild_id = ? ORDER BY playlist",
                (guild_id,),
            ).fetchall()
            return RocketLeagueTrackingConfig(
                cast(int, row[0]),
                cast(int | None, row[1]),
                cast(int, row[2]),
                cast(int, row[3]),
                cast(int | None, row[4]),
                tuple(cast(int, playlist_row[0]) for playlist_row in playlist_rows) or (11,),
            )

        return await self._run(operation)

    async def list_rocket_league_tracking_configs(self) -> list[RocketLeagueTrackingConfig]:
        def operation(connection: sqlite3.Connection) -> list[RocketLeagueTrackingConfig]:
            rows = connection.execute(
                "SELECT guild_id, streak_channel_id, streak_threshold, inactivity_seconds, "
                "leaderboard_channel_id "
                "FROM rocket_league_tracking_config"
            ).fetchall()
            configs: list[RocketLeagueTrackingConfig] = []
            for row in rows:
                guild_id = cast(int, row[0])
                playlist_rows = connection.execute(
                    "SELECT playlist FROM rocket_league_leaderboard_playlists "
                    "WHERE guild_id = ? ORDER BY playlist",
                    (guild_id,),
                ).fetchall()
                configs.append(
                    RocketLeagueTrackingConfig(
                        guild_id,
                        cast(int | None, row[1]),
                        cast(int, row[2]),
                        cast(int, row[3]),
                        cast(int | None, row[4]),
                        tuple(cast(int, playlist_row[0]) for playlist_row in playlist_rows)
                        or (11,),
                    )
                )
            return configs

        return await self._run(operation)

    async def configure_rocket_league_streaks(
        self,
        *,
        guild_id: int,
        channel_id: int | None,
        threshold: int,
        inactivity_seconds: int,
    ) -> RocketLeagueTrackingConfig:
        if not 3 <= threshold <= 20:
            raise ValueError("streak threshold must be between 3 and 20")
        if not 300 <= inactivity_seconds <= 86400:
            raise ValueError("inactivity must be between 5 minutes and 24 hours")

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO rocket_league_tracking_config
                    (guild_id, streak_channel_id, streak_threshold, inactivity_seconds)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (guild_id) DO UPDATE SET
                    streak_channel_id = excluded.streak_channel_id,
                    streak_threshold = excluded.streak_threshold,
                    inactivity_seconds = excluded.inactivity_seconds,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id, channel_id, threshold, inactivity_seconds),
            )
            if channel_id is None:
                connection.execute(
                    "DELETE FROM rocket_league_event_notification_config "
                    "WHERE guild_id = ? AND event_key = 'session_streak'",
                    (guild_id,),
                )
            else:
                connection.execute(
                    "INSERT INTO rocket_league_event_notification_config "
                    "(guild_id, event_key, channel_id, minimum_count, configured_at) "
                    "VALUES (?, 'session_streak', ?, ?, ?) "
                    "ON CONFLICT (guild_id, event_key) DO UPDATE SET "
                    "channel_id = excluded.channel_id, minimum_count = excluded.minimum_count, "
                    "configured_at = excluded.configured_at",
                    (guild_id, channel_id, threshold, int(time.time())),
                )
            connection.commit()

        await self._run(operation)
        return await self.get_rocket_league_tracking_config(guild_id=guild_id)

    async def configure_rocket_league_leaderboard(
        self,
        *,
        guild_id: int,
        channel_id: int | None,
        playlists: Sequence[int],
    ) -> RocketLeagueTrackingConfig:
        selected = tuple(dict.fromkeys(playlists))
        if not selected or any(
            isinstance(playlist, bool) or not 0 < playlist <= MAX_ROCKET_LEAGUE_PLAYLIST_ID
            for playlist in selected
        ):
            raise ValueError("at least one valid Rocket League playlist is required")

        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO rocket_league_tracking_config
                    (guild_id, leaderboard_channel_id, leaderboard_playlist, inactivity_seconds)
                VALUES (?, ?, ?, 1800)
                ON CONFLICT (guild_id) DO UPDATE SET
                    leaderboard_channel_id = excluded.leaderboard_channel_id,
                    leaderboard_playlist = excluded.leaderboard_playlist,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id, channel_id, selected[0]),
            )
            connection.execute(
                "DELETE FROM rocket_league_leaderboard_playlists WHERE guild_id = ?", (guild_id,)
            )
            connection.executemany(
                "INSERT INTO rocket_league_leaderboard_playlists (guild_id, playlist) "
                "VALUES (?, ?)",
                [(guild_id, playlist) for playlist in selected],
            )
            connection.execute(
                "DELETE FROM rocket_league_leaderboard_snapshot WHERE guild_id = ?",
                (guild_id,),
            )
            if channel_id is None:
                connection.execute(
                    "DELETE FROM rocket_league_event_notification_config "
                    "WHERE guild_id = ? AND event_key = 'leaderboard_movement'",
                    (guild_id,),
                )
            else:
                connection.execute(
                    "INSERT INTO rocket_league_event_notification_config "
                    "(guild_id, event_key, channel_id, minimum_count, configured_at) "
                    "VALUES (?, 'leaderboard_movement', ?, 1, ?) "
                    "ON CONFLICT (guild_id, event_key) DO UPDATE SET "
                    "channel_id = excluded.channel_id, minimum_count = 1, "
                    "configured_at = excluded.configured_at",
                    (guild_id, channel_id, int(time.time())),
                )
            connection.commit()

        await self._run(operation)
        return await self.get_rocket_league_tracking_config(guild_id=guild_id)

    async def record_rocket_league_activity_batch(
        self,
        *,
        guild_id: int,
        discord_user_id: int,
        player_id: str,
        player_name: str,
        activities: Sequence[tuple[int, int, int, float, str]],
        threshold: int,
        observed_at: int,
        inactivity_seconds: int = 1800,
        rank_positions: dict[int, tuple[int, int]] | None = None,
    ) -> list[CompletedRocketLeagueStreak]:
        if not 300 <= inactivity_seconds <= 86400:
            raise ValueError("inactivity must be between 5 minutes and 24 hours")

        positions = rank_positions or {}

        def operation(connection: sqlite3.Connection) -> list[CompletedRocketLeagueStreak]:
            session_cursor = connection.execute(
                "SELECT COALESCE(MAX(id),0) FROM rocket_league_match_history"
            ).fetchone()[0]
            active_session = connection.execute(
                "SELECT last_at,cutoff FROM play_sessions WHERE guild_id=? AND player_id=? "
                "AND owner_id=? ORDER BY id DESC LIMIT 1",
                (guild_id, player_id, discord_user_id if guild_id < 0 else 0),
            ).fetchone()
            updates: dict[str, list[tuple[int, ResultInference]]] = {
                "main": [],
                "special": [],
            }
            all_time_updates: dict[str, list[tuple[int, ResultInference]]] = {
                "main": [],
                "all": [],
            }
            for playlist, matches_played, win_streak, mmr, rank in activities:
                tier, division = positions.get(playlist, (None, None))
                sample = connection.execute(
                    "SELECT player_id, matches_played, win_streak, mmr, tier, division, rank, "
                    "observed_at FROM rocket_league_activity_samples "
                    "WHERE guild_id = ? AND discord_user_id = ? AND playlist = ?",
                    (guild_id, discord_user_id, playlist),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO rocket_league_activity_samples
                        (guild_id, discord_user_id, player_id, playlist, matches_played,
                         win_streak, mmr, tier, division, rank, observed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (guild_id, discord_user_id, playlist) DO UPDATE SET
                        player_id = excluded.player_id,
                        matches_played = excluded.matches_played,
                        win_streak = excluded.win_streak,
                        mmr = excluded.mmr,
                        tier = excluded.tier,
                        division = excluded.division,
                        rank = excluded.rank,
                        observed_at = excluded.observed_at
                    """,
                    (
                        guild_id,
                        discord_user_id,
                        player_id,
                        playlist,
                        matches_played,
                        win_streak,
                        mmr,
                        tier,
                        division,
                        rank,
                        observed_at,
                    ),
                )
                if (
                    sample is None
                    or cast(str, sample[0]) != player_id
                    or cast(float, sample[3]) != mmr
                    or observed_at - cast(int, sample[7]) >= 3600
                ):
                    connection.execute(
                        "INSERT INTO rocket_league_mmr_history "
                        "(guild_id, discord_user_id, player_id, player_name, playlist, mmr, rank, "
                        "observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            guild_id,
                            discord_user_id,
                            player_id,
                            player_name,
                            playlist,
                            mmr,
                            rank,
                            observed_at,
                        ),
                    )
                previous_peak_row = connection.execute(
                    "SELECT mmr FROM rocket_league_peak_mmr "
                    "WHERE guild_id = ? AND player_id = ? AND playlist = ?",
                    (guild_id, player_id, playlist),
                ).fetchone()
                connection.execute(
                    """
                    INSERT INTO rocket_league_peak_mmr
                        (guild_id, discord_user_id, player_id, player_name, playlist, mmr, rank,
                         observed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (guild_id, player_id, playlist) DO UPDATE SET
                        discord_user_id = excluded.discord_user_id,
                        player_name = excluded.player_name,
                        mmr = excluded.mmr,
                        rank = excluded.rank,
                        observed_at = excluded.observed_at
                    WHERE excluded.mmr > rocket_league_peak_mmr.mmr
                    """,
                    (
                        guild_id,
                        discord_user_id,
                        player_id,
                        player_name,
                        playlist,
                        mmr,
                        rank,
                        observed_at,
                    ),
                )
                if sample is None or cast(str, sample[0]) != player_id:
                    if tier is not None and division is not None:
                        connection.execute(
                            "INSERT INTO rocket_league_rank_state_visits "
                            "(guild_id,discord_user_id,playlist,player_id,tier,division,"
                            "entered_matches) VALUES (?,?,?,?,?,?,?) "
                            "ON CONFLICT (guild_id,discord_user_id,playlist) DO UPDATE SET "
                            "player_id=excluded.player_id,tier=excluded.tier,"
                            "division=excluded.division,entered_matches=excluded.entered_matches,"
                            "wins=0,losses=0,unknown=0",
                            (
                                guild_id,
                                discord_user_id,
                                playlist,
                                player_id,
                                tier,
                                division,
                                matches_played,
                            ),
                        )
                    continue
                previous_tier = cast(int | None, sample[4])
                previous_division = cast(int | None, sample[5])
                previous_rank = cast(str | None, sample[6])
                previous_matches = cast(int, sample[1])
                season_reset = matches_played < previous_matches
                inference = (
                    ResultInference(0, ())
                    if season_reset
                    else infer_activity_results(
                        previous_matches=previous_matches,
                        previous_win_streak=cast(int, sample[2]),
                        previous_mmr=cast(float, sample[3]),
                        current_matches=matches_played,
                        current_win_streak=win_streak,
                        current_mmr=mmr,
                    )
                )
                if previous_tier is not None and previous_division is not None:
                    visit = connection.execute(
                        "SELECT player_id,tier,division FROM rocket_league_rank_state_visits "
                        "WHERE guild_id=? AND discord_user_id=? AND playlist=?",
                        (guild_id, discord_user_id, playlist),
                    ).fetchone()
                    if visit is None or (
                        cast(str, visit[0]),
                        cast(int, visit[1]),
                        cast(int, visit[2]),
                    ) != (player_id, previous_tier, previous_division):
                        connection.execute(
                            "INSERT INTO rocket_league_rank_state_visits "
                            "(guild_id,discord_user_id,playlist,player_id,tier,division,"
                            "entered_matches) VALUES (?,?,?,?,?,?,?) "
                            "ON CONFLICT (guild_id,discord_user_id,playlist) DO UPDATE SET "
                            "player_id=excluded.player_id,tier=excluded.tier,"
                            "division=excluded.division,entered_matches=excluded.entered_matches,"
                            "wins=0,losses=0,unknown=0",
                            (
                                guild_id,
                                discord_user_id,
                                playlist,
                                player_id,
                                previous_tier,
                                previous_division,
                                previous_matches,
                            ),
                        )
                    connection.execute(
                        "UPDATE rocket_league_rank_state_visits SET wins=wins+?,"
                        "losses=losses+?,unknown=unknown+? WHERE guild_id=? "
                        "AND discord_user_id=? AND playlist=?",
                        (
                            inference.known_tail.count("win"),
                            inference.known_tail.count("loss"),
                            inference.unknown_count,
                            guild_id,
                            discord_user_id,
                            playlist,
                        ),
                    )
                if (
                    not season_reset
                    and tier is not None
                    and division is not None
                    and previous_tier is not None
                    and previous_division is not None
                    and (tier, division) != (previous_tier, previous_division)
                ):
                    direction = (
                        "win" if (tier, division) > (previous_tier, previous_division) else "loss"
                    )
                    visit_totals = connection.execute(
                        "SELECT wins,losses,unknown FROM rocket_league_rank_state_visits "
                        "WHERE guild_id=? AND discord_user_id=? AND playlist=?",
                        (guild_id, discord_user_id, playlist),
                    ).fetchone()
                    result_count = (
                        cast(int, visit_totals[0 if direction == "win" else 1])
                        if visit_totals is not None
                        and cast(int, visit_totals[2]) == 0
                        and cast(int, visit_totals[0 if direction == "win" else 1]) > 0
                        else None
                    )
                    connection.execute(
                        """
                        INSERT INTO rocket_league_rank_boundaries
                            (playlist,from_tier,from_division,to_tier,to_division,direction,
                             mean_mmr,mean_results,result_sample_count,sample_count,
                             last_observed)
                        VALUES (?,?,?,?,?,?,?,?,CASE WHEN ? IS NULL THEN 0 ELSE 1 END,1,?)
                        ON CONFLICT
                            (playlist,from_tier,from_division,to_tier,to_division,direction)
                        DO UPDATE SET
                            mean_mmr =
                                (rocket_league_rank_boundaries.mean_mmr *
                                 MIN(rocket_league_rank_boundaries.sample_count,24) +
                                 excluded.mean_mmr) /
                                (MIN(rocket_league_rank_boundaries.sample_count,24) + 1),
                            mean_results = CASE WHEN excluded.mean_results IS NULL
                                THEN rocket_league_rank_boundaries.mean_results
                                WHEN rocket_league_rank_boundaries.mean_results IS NULL
                                THEN excluded.mean_results
                                ELSE (rocket_league_rank_boundaries.mean_results *
                                      MIN(rocket_league_rank_boundaries.result_sample_count,24) +
                                      excluded.mean_results) /
                                     (MIN(rocket_league_rank_boundaries.result_sample_count,24)+1)
                                END,
                            result_sample_count = MIN(
                                rocket_league_rank_boundaries.result_sample_count +
                                excluded.result_sample_count, 50
                            ),
                            sample_count = MIN(
                                rocket_league_rank_boundaries.sample_count + 1, 50
                            ),
                            last_observed = excluded.last_observed
                        """,
                        (
                            playlist,
                            previous_tier,
                            previous_division,
                            tier,
                            division,
                            direction,
                            (cast(float, sample[3]) + mmr) / 2,
                            result_count,
                            result_count,
                            observed_at,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO rocket_league_events "
                        "(guild_id, discord_user_id, player_id, player_name, event_type, playlist, "
                        "occurred_at, rank_before, rank_after, mmr_before, mmr_after) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            guild_id,
                            discord_user_id,
                            player_id,
                            player_name,
                            "rank_up" if direction == "win" else "rank_down",
                            playlist,
                            observed_at,
                            previous_rank,
                            rank,
                            cast(float, sample[3]),
                            mmr,
                        ),
                    )
                    connection.execute(
                        "UPDATE rocket_league_rank_state_visits SET tier=?,division=?,"
                        "entered_matches=?,wins=0,losses=0,unknown=0 WHERE guild_id=? "
                        "AND discord_user_id=? AND playlist=?",
                        (
                            tier,
                            division,
                            matches_played,
                            guild_id,
                            discord_user_id,
                            playlist,
                        ),
                    )
                if previous_peak_row is not None and mmr > cast(float, previous_peak_row[0]):
                    connection.execute(
                        "INSERT INTO rocket_league_events "
                        "(guild_id, discord_user_id, player_id, player_name, event_type, playlist, "
                        "occurred_at, mmr_before, mmr_after) "
                        "VALUES (?, ?, ?, ?, 'peak_mmr', ?, ?, ?, ?)",
                        (
                            guild_id,
                            discord_user_id,
                            player_id,
                            player_name,
                            playlist,
                            observed_at,
                            cast(float, previous_peak_row[0]),
                            mmr,
                        ),
                    )
                streak_class = "main" if playlist in ROCKET_LEAGUE_MAIN_PLAYLISTS else "special"
                if season_reset:
                    if tier is not None and division is not None:
                        connection.execute(
                            "UPDATE rocket_league_rank_state_visits SET tier=?,division=?,"
                            "entered_matches=?,wins=0,losses=0,unknown=0 WHERE guild_id=? "
                            "AND discord_user_id=? AND playlist=?",
                            (
                                tier,
                                division,
                                matches_played,
                                guild_id,
                                discord_user_id,
                                playlist,
                            ),
                        )
                    reset = ResultInference(max(previous_matches - matches_played, 1), ())
                    updates[streak_class].append((playlist, reset))
                    all_time_updates["all"].append((playlist, reset))
                    if streak_class == "main":
                        all_time_updates["main"].append((playlist, reset))
                    continue
                if inference.unknown_count:
                    history_cursor = connection.execute(
                        """
                        INSERT INTO rocket_league_match_history
                            (guild_id, discord_user_id, player_id, playlist, result, match_count,
                             observed_at, mmr_before, mmr_after)
                        VALUES (?, ?, ?, ?, 'gap', ?, ?, ?, ?)
                        """,
                        (
                            guild_id,
                            discord_user_id,
                            player_id,
                            playlist,
                            inference.unknown_count,
                            observed_at,
                            cast(float, sample[3]),
                            mmr,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO rocket_league_events "
                        "(guild_id, discord_user_id, player_id, player_name, event_type, playlist, "
                        "occurred_at, result, event_count, mmr_before, mmr_after, "
                        "source_match_history_id) "
                        "VALUES (?, ?, ?, ?, 'match', ?, ?, 'gap', ?, ?, ?, ?)",
                        (
                            guild_id,
                            discord_user_id,
                            player_id,
                            player_name,
                            playlist,
                            observed_at,
                            inference.unknown_count,
                            cast(float, sample[3]),
                            mmr,
                            history_cursor.lastrowid,
                        ),
                    )
                for result in inference.known_tail:
                    history_cursor = connection.execute(
                        """
                        INSERT INTO rocket_league_match_history
                            (guild_id, discord_user_id, player_id, playlist, result, match_count,
                             observed_at, mmr_before, mmr_after)
                        VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                        """,
                        (
                            guild_id,
                            discord_user_id,
                            player_id,
                            playlist,
                            result,
                            observed_at,
                            cast(float, sample[3]),
                            mmr,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO rocket_league_events "
                        "(guild_id, discord_user_id, player_id, player_name, event_type, playlist, "
                        "occurred_at, result, event_count, mmr_before, mmr_after, "
                        "source_match_history_id) "
                        "VALUES (?, ?, ?, ?, 'match', ?, ?, ?, 1, ?, ?, ?)",
                        (
                            guild_id,
                            discord_user_id,
                            player_id,
                            player_name,
                            playlist,
                            observed_at,
                            result,
                            cast(float, sample[3]),
                            mmr,
                            history_cursor.lastrowid,
                        ),
                    )
                if inference.unknown_count or inference.known_tail:
                    updates[streak_class].append((playlist, inference))
                    all_time_updates["all"].append((playlist, inference))
                    if streak_class == "main":
                        all_time_updates["main"].append((playlist, inference))

            completed: list[CompletedRocketLeagueStreak] = []

            def finish_known(
                streak_class: str,
                state: sqlite3.Row | tuple[object, ...] | None,
                ended_by: str,
                *,
                notify: bool,
            ) -> None:
                if state is None or state[0] is None or cast(int, state[1]) <= 0:
                    return
                prior_result = cast(str, state[0])
                prior_count = cast(int, state[1])
                all_time_scope = "main" if streak_class == "main" else "all"
                all_time_state = connection.execute(
                    "SELECT result, streak_count FROM rocket_league_all_time_streak_state "
                    "WHERE guild_id = ? AND discord_user_id = ? AND scope = ?",
                    (guild_id, discord_user_id, all_time_scope),
                ).fetchone()
                total_count = (
                    cast(int, all_time_state[1])
                    if all_time_state is not None and cast(str, all_time_state[0]) == prior_result
                    else prior_count
                )
                connection.execute(
                    "INSERT INTO rocket_league_events "
                    "(guild_id, discord_user_id, player_id, player_name, event_type, playlist, "
                    "occurred_at, result, event_count, scope, total_count, notification_eligible) "
                    "VALUES (?, ?, ?, ?, 'session_streak_completed', 0, ?, ?, ?, ?, ?, ?)",
                    (
                        guild_id,
                        discord_user_id,
                        player_id,
                        player_name,
                        observed_at,
                        prior_result,
                        prior_count,
                        streak_class,
                        total_count,
                        notify,
                    ),
                )
                if notify and total_count >= threshold:
                    completed.append(
                        CompletedRocketLeagueStreak(
                            guild_id,
                            discord_user_id,
                            player_name,
                            streak_class,
                            prior_result,
                            prior_count,
                            ended_by,
                            total_count,
                            total_count > prior_count,
                        )
                    )

            for streak_class, changed in updates.items():
                state = connection.execute(
                    "SELECT result, streak_count, last_match_at "
                    "FROM rocket_league_combined_streak_state "
                    "WHERE guild_id = ? AND discord_user_id = ? AND streak_class = ?",
                    (guild_id, discord_user_id, streak_class),
                ).fetchone()
                if (
                    state is not None
                    and state[2] is not None
                    and (
                        active_session[0] + active_session[1] < observed_at
                        if active_session
                        else cast(int, state[2]) < observed_at - inactivity_seconds
                    )
                ):
                    finish_known(streak_class, state, "inactivity", notify=True)
                    connection.execute(
                        "DELETE FROM rocket_league_combined_streak_state "
                        "WHERE guild_id = ? AND discord_user_id = ? AND streak_class = ?",
                        (guild_id, discord_user_id, streak_class),
                    )
                    state = None
                if not changed:
                    continue

                ambiguous = False
                results: tuple[str, ...] = ()
                if len(changed) == 1:
                    inference = changed[0][1]
                    if inference.unknown_count:
                        finish_known(streak_class, state, "unknown_results", notify=False)
                        state = None
                        connection.execute(
                            "DELETE FROM rocket_league_combined_streak_state "
                            "WHERE guild_id = ? AND discord_user_id = ? AND streak_class = ?",
                            (guild_id, discord_user_id, streak_class),
                        )
                    results = inference.known_tail
                else:
                    flattened = tuple(
                        result
                        for _playlist, inference in changed
                        for result in inference.known_tail
                    )
                    has_unknown = any(inference.unknown_count for _playlist, inference in changed)
                    ambiguous = has_unknown or len(set(flattened)) > 1
                    if ambiguous:
                        finish_known(streak_class, state, "unknown_results", notify=False)
                        connection.execute(
                            "DELETE FROM rocket_league_combined_streak_state "
                            "WHERE guild_id = ? AND discord_user_id = ? AND streak_class = ?",
                            (guild_id, discord_user_id, streak_class),
                        )
                        state = None
                    else:
                        results = flattened
                if ambiguous or not results:
                    continue

                current_result = cast(str | None, state[0]) if state is not None else None
                current_count = cast(int, state[1]) if state is not None else 0
                for result in results:
                    if current_result is not None and current_result != result:
                        finish_known(
                            streak_class,
                            (current_result, current_count, observed_at),
                            "opposite_result",
                            notify=True,
                        )
                        current_count = 0
                    current_count = current_count + 1 if current_result == result else 1
                    current_result = result
                connection.execute(
                    """
                    INSERT INTO rocket_league_combined_streak_state
                        (guild_id, discord_user_id, streak_class, result, streak_count,
                         last_match_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (guild_id, discord_user_id, streak_class) DO UPDATE SET
                        result = excluded.result,
                        streak_count = excluded.streak_count,
                        last_match_at = excluded.last_match_at
                    """,
                    (
                        guild_id,
                        discord_user_id,
                        streak_class,
                        current_result,
                        current_count,
                        observed_at,
                    ),
                )

            def finish_all_time(scope: str, state: sqlite3.Row | tuple[object, ...] | None) -> None:
                if state is None or state[0] is None or cast(int, state[1]) <= 0:
                    return
                prior_result = cast(str, state[0])
                prior_count = cast(int, state[1])
                record_cursor = connection.execute(
                    "INSERT INTO rocket_league_all_time_streak_records "
                    "(guild_id, discord_user_id, player_id, player_name, scope, result, "
                    "streak_count, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        guild_id,
                        discord_user_id,
                        player_id,
                        player_name,
                        scope,
                        prior_result,
                        prior_count,
                        observed_at,
                    ),
                )
                record_id = cast(int, record_cursor.lastrowid)
                position_row = connection.execute(
                    "SELECT COUNT(*) + 1 FROM rocket_league_all_time_streak_records "
                    "WHERE guild_id = ? AND scope = ? AND result = ? AND ("
                    "streak_count > ? OR (streak_count = ? AND (observed_at < ? OR "
                    "(observed_at = ? AND id < ?))))",
                    (
                        guild_id,
                        scope,
                        prior_result,
                        prior_count,
                        prior_count,
                        observed_at,
                        observed_at,
                        record_id,
                    ),
                ).fetchone()
                leaderboard_position = cast(int, position_row[0])
                connection.execute(
                    "INSERT INTO rocket_league_events "
                    "(guild_id, discord_user_id, player_id, player_name, event_type, playlist, "
                    "occurred_at, result, event_count, scope, total_count, position_after) "
                    "VALUES (?, ?, ?, ?, 'all_time_streak_completed', 0, ?, ?, ?, ?, ?, ?)",
                    (
                        guild_id,
                        discord_user_id,
                        player_id,
                        player_name,
                        observed_at,
                        prior_result,
                        prior_count,
                        scope,
                        prior_count,
                        leaderboard_position,
                    ),
                )
                if leaderboard_position <= ROCKET_LEAGUE_STREAK_LEADERBOARD_SIZE:
                    connection.execute(
                        "INSERT INTO rocket_league_events "
                        "(guild_id, discord_user_id, player_id, player_name, event_type, playlist, "
                        "occurred_at, result, event_count, scope, total_count, position_after) "
                        "VALUES (?, ?, ?, ?, 'streak_leaderboard_movement', 0, ?, ?, ?, ?, ?, ?)",
                        (
                            guild_id,
                            discord_user_id,
                            player_id,
                            player_name,
                            observed_at,
                            prior_result,
                            prior_count,
                            scope,
                            prior_count,
                            leaderboard_position,
                        ),
                    )
                connection.execute(
                    "DELETE FROM rocket_league_all_time_streak_records "
                    "WHERE id IN ("
                    "SELECT id FROM ("
                    "SELECT id, ROW_NUMBER() OVER ("
                    "ORDER BY streak_count DESC, observed_at ASC, id ASC"
                    ") AS position FROM rocket_league_all_time_streak_records "
                    "WHERE guild_id = ? AND scope = ? AND result = ?"
                    ") WHERE position > ?"
                    ")",
                    (
                        guild_id,
                        scope,
                        prior_result,
                        ROCKET_LEAGUE_STREAK_LEADERBOARD_SIZE,
                    ),
                )

            for scope, changed in all_time_updates.items():
                if not changed:
                    continue
                state = connection.execute(
                    "SELECT result, streak_count FROM rocket_league_all_time_streak_state "
                    "WHERE guild_id = ? AND discord_user_id = ? AND scope = ?",
                    (guild_id, discord_user_id, scope),
                ).fetchone()
                ambiguous = False
                all_time_results: tuple[str, ...] = ()
                if len(changed) == 1:
                    inference = changed[0][1]
                    if inference.unknown_count:
                        finish_all_time(scope, state)
                        state = None
                        connection.execute(
                            "DELETE FROM rocket_league_all_time_streak_state "
                            "WHERE guild_id = ? AND discord_user_id = ? AND scope = ?",
                            (guild_id, discord_user_id, scope),
                        )
                    all_time_results = inference.known_tail
                else:
                    flattened = tuple(
                        result
                        for _playlist, inference in changed
                        for result in inference.known_tail
                    )
                    ambiguous = (
                        any(inference.unknown_count for _playlist, inference in changed)
                        or len(set(flattened)) > 1
                    )
                    if ambiguous:
                        finish_all_time(scope, state)
                        connection.execute(
                            "DELETE FROM rocket_league_all_time_streak_state "
                            "WHERE guild_id = ? AND discord_user_id = ? AND scope = ?",
                            (guild_id, discord_user_id, scope),
                        )
                        state = None
                    else:
                        all_time_results = flattened
                if ambiguous or not all_time_results:
                    continue

                current_result = cast(str | None, state[0]) if state is not None else None
                current_count = cast(int, state[1]) if state is not None else 0
                for result in all_time_results:
                    if current_result is not None and current_result != result:
                        finish_all_time(scope, (current_result, current_count))
                        current_count = 0
                    current_count = current_count + 1 if current_result == result else 1
                    current_result = result
                connection.execute(
                    """
                    INSERT INTO rocket_league_all_time_streak_state
                        (guild_id, discord_user_id, scope, result, streak_count)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (guild_id, discord_user_id, scope) DO UPDATE SET
                        result = excluded.result,
                        streak_count = excluded.streak_count
                    """,
                    (guild_id, discord_user_id, scope, current_result, current_count),
                )
            sessions.ingest(connection, session_cursor, player_name)
            connection.commit()
            return completed

        return await self._run(operation)

    async def record_rocket_league_activity(
        self,
        *,
        guild_id: int,
        discord_user_id: int,
        player_id: str,
        player_name: str,
        playlist: int,
        matches_played: int,
        win_streak: int,
        mmr: float,
        threshold: int,
        observed_at: int,
        inactivity_seconds: int = 1800,
        rank: str = "Unknown rank",
    ) -> list[CompletedRocketLeagueStreak]:
        return await self.record_rocket_league_activity_batch(
            guild_id=guild_id,
            discord_user_id=discord_user_id,
            player_id=player_id,
            player_name=player_name,
            activities=[(playlist, matches_played, win_streak, mmr, rank)],
            threshold=threshold,
            observed_at=observed_at,
            inactivity_seconds=inactivity_seconds,
        )

    async def _record_rocket_league_activity_legacy(
        self,
        *,
        guild_id: int,
        discord_user_id: int,
        player_id: str,
        player_name: str,
        playlist: int,
        matches_played: int,
        win_streak: int,
        mmr: float,
        threshold: int,
        observed_at: int,
        inactivity_seconds: int = 1800,
        rank: str = "Unknown rank",
    ) -> list[CompletedRocketLeagueStreak]:
        if not 300 <= inactivity_seconds <= 86400:
            raise ValueError("inactivity must be between 5 minutes and 24 hours")

        def operation(connection: sqlite3.Connection) -> list[CompletedRocketLeagueStreak]:
            sample = connection.execute(
                "SELECT player_id, matches_played, win_streak, mmr "
                "FROM rocket_league_activity_samples "
                "WHERE guild_id = ? AND discord_user_id = ? AND playlist = ?",
                (guild_id, discord_user_id, playlist),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO rocket_league_activity_samples
                    (guild_id, discord_user_id, player_id, playlist, matches_played,
                     win_streak, mmr, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (guild_id, discord_user_id, playlist) DO UPDATE SET
                    player_id = excluded.player_id,
                    matches_played = excluded.matches_played,
                    win_streak = excluded.win_streak,
                    mmr = excluded.mmr,
                    observed_at = excluded.observed_at
                """,
                (
                    guild_id,
                    discord_user_id,
                    player_id,
                    playlist,
                    matches_played,
                    win_streak,
                    mmr,
                    observed_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO rocket_league_peak_mmr
                    (guild_id, discord_user_id, player_id, player_name, playlist, mmr, rank,
                     observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (guild_id, player_id, playlist) DO UPDATE SET
                    discord_user_id = excluded.discord_user_id,
                    player_name = excluded.player_name,
                    mmr = excluded.mmr,
                    rank = excluded.rank,
                    observed_at = excluded.observed_at
                WHERE excluded.mmr > rocket_league_peak_mmr.mmr
                """,
                (
                    guild_id,
                    discord_user_id,
                    player_id,
                    player_name,
                    playlist,
                    mmr,
                    rank,
                    observed_at,
                ),
            )
            if sample is None or cast(str, sample[0]) != player_id:
                connection.commit()
                return []
            previous_matches = cast(int, sample[1])
            if matches_played < previous_matches:
                connection.execute(
                    "DELETE FROM rocket_league_streak_state "
                    "WHERE guild_id = ? AND discord_user_id = ? AND playlist = ?",
                    (guild_id, discord_user_id, playlist),
                )
                connection.commit()
                return []
            inference = infer_activity_results(
                previous_matches=previous_matches,
                previous_win_streak=cast(int, sample[2]),
                previous_mmr=cast(float, sample[3]),
                current_matches=matches_played,
                current_win_streak=win_streak,
                current_mmr=mmr,
            )
            if inference.unknown_count:
                interrupted = connection.execute(
                    "SELECT result, streak_count FROM rocket_league_streak_state "
                    "WHERE guild_id = ? AND discord_user_id = ? AND playlist = ?",
                    (guild_id, discord_user_id, playlist),
                ).fetchone()
                if interrupted is not None and cast(str | None, interrupted[0]) is not None:
                    interrupted_result = cast(str, interrupted[0])
                    interrupted_count = cast(int, interrupted[1])
                    if interrupted_count:
                        self._insert_rocket_league_record(
                            connection,
                            guild_id=guild_id,
                            discord_user_id=discord_user_id,
                            player_name=player_name,
                            playlist=playlist,
                            category=f"{interrupted_result}_streak",
                            value=float(interrupted_count),
                            match_count=interrupted_count,
                            observed_at=observed_at,
                        )
                connection.execute(
                    """
                    INSERT INTO rocket_league_match_history
                        (guild_id, discord_user_id, player_id, playlist, result, match_count,
                         observed_at, mmr_before, mmr_after)
                    VALUES (?, ?, ?, ?, 'gap', ?, ?, ?, ?)
                    """,
                    (
                        guild_id,
                        discord_user_id,
                        player_id,
                        playlist,
                        inference.unknown_count,
                        observed_at,
                        cast(float, sample[3]),
                        mmr,
                    ),
                )
                connection.execute(
                    "DELETE FROM rocket_league_streak_state "
                    "WHERE guild_id = ? AND discord_user_id = ? AND playlist = ?",
                    (guild_id, discord_user_id, playlist),
                )

            completed: list[CompletedRocketLeagueStreak] = []
            if inference.known_tail:
                state = connection.execute(
                    "SELECT result, streak_count, last_match_at "
                    "FROM rocket_league_streak_state "
                    "WHERE guild_id = ? AND discord_user_id = ? AND playlist = ?",
                    (guild_id, discord_user_id, playlist),
                ).fetchone()
                if (
                    state is not None
                    and state[2] is not None
                    and cast(int, state[2]) <= observed_at - inactivity_seconds
                ):
                    prior_result = cast(str | None, state[0])
                    prior_count = cast(int, state[1])
                    if prior_result is not None and prior_count:
                        self._insert_rocket_league_record(
                            connection,
                            guild_id=guild_id,
                            discord_user_id=discord_user_id,
                            player_name=player_name,
                            playlist=playlist,
                            category=f"{prior_result}_streak",
                            value=float(prior_count),
                            match_count=prior_count,
                            observed_at=observed_at,
                        )
                    if prior_result is not None and prior_count >= threshold:
                        completed.append(
                            CompletedRocketLeagueStreak(
                                guild_id,
                                discord_user_id,
                                player_name,
                                "main" if playlist in ROCKET_LEAGUE_MAIN_PLAYLISTS else "special",
                                prior_result,
                                prior_count,
                                "inactivity",
                            )
                        )
                    connection.execute(
                        "DELETE FROM rocket_league_streak_state "
                        "WHERE guild_id = ? AND discord_user_id = ? AND playlist = ?",
                        (guild_id, discord_user_id, playlist),
                    )
            for result in inference.known_tail:
                connection.execute(
                    """
                    INSERT INTO rocket_league_match_history
                        (guild_id, discord_user_id, player_id, playlist, result, match_count,
                         observed_at, mmr_before, mmr_after)
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        guild_id,
                        discord_user_id,
                        player_id,
                        playlist,
                        result,
                        observed_at,
                        cast(float, sample[3]),
                        mmr,
                    ),
                )
                state = connection.execute(
                    "SELECT result, streak_count FROM rocket_league_streak_state "
                    "WHERE guild_id = ? AND discord_user_id = ? AND playlist = ?",
                    (guild_id, discord_user_id, playlist),
                ).fetchone()
                prior_result = cast(str | None, state[0]) if state is not None else None
                prior_count = cast(int, state[1]) if state is not None else 0
                if prior_result is not None and prior_result != result and prior_count:
                    self._insert_rocket_league_record(
                        connection,
                        guild_id=guild_id,
                        discord_user_id=discord_user_id,
                        player_name=player_name,
                        playlist=playlist,
                        category=f"{prior_result}_streak",
                        value=float(prior_count),
                        match_count=prior_count,
                        observed_at=observed_at,
                    )
                if prior_result is not None and prior_result != result and prior_count >= threshold:
                    completed.append(
                        CompletedRocketLeagueStreak(
                            guild_id,
                            discord_user_id,
                            player_name,
                            "main" if playlist in ROCKET_LEAGUE_MAIN_PLAYLISTS else "special",
                            prior_result,
                            prior_count,
                            "opposite_result",
                        )
                    )
                count = prior_count + 1 if prior_result == result else 1
                connection.execute(
                    """
                    INSERT INTO rocket_league_streak_state
                        (guild_id, discord_user_id, playlist, result, streak_count, last_match_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (guild_id, discord_user_id, playlist) DO UPDATE SET
                        result = excluded.result,
                        streak_count = excluded.streak_count,
                        last_match_at = excluded.last_match_at
                    """,
                    (guild_id, discord_user_id, playlist, result, count, observed_at),
                )
            connection.commit()
            return completed

        return await self._run(operation)

    async def finish_inactive_rocket_league_streaks(
        self, *, now: int
    ) -> list[CompletedRocketLeagueStreak]:
        def operation(connection: sqlite3.Connection) -> list[CompletedRocketLeagueStreak]:
            rows = connection.execute(
                """
                SELECT s.guild_id, s.discord_user_id,
                       COALESCE(a.player_id, u.player_id),
                       COALESCE(a.player_name, u.player_name), s.streak_class,
                       s.result, s.streak_count,
                       COALESCE(c.streak_threshold, 3), c.streak_channel_id,
                       ats.result, ats.streak_count
                FROM rocket_league_combined_streak_state AS s
                LEFT JOIN rocket_league_tracking_config AS c ON c.guild_id = s.guild_id
                LEFT JOIN rocket_league_accounts AS a
                  ON a.guild_id = s.guild_id AND a.discord_user_id = s.discord_user_id
                LEFT JOIN rocket_league_unlinked_accounts AS u
                  ON u.guild_id = s.guild_id AND -u.id = s.discord_user_id
                LEFT JOIN rocket_league_all_time_streak_state AS ats
                  ON ats.guild_id = s.guild_id
                 AND ats.discord_user_id = s.discord_user_id
                 AND ats.scope = CASE WHEN s.streak_class = 'main' THEN 'main' ELSE 'all' END
                WHERE s.result IS NOT NULL
                  AND COALESCE((SELECT ps.last_at+ps.cutoff FROM play_sessions ps
                      WHERE ps.guild_id=s.guild_id AND ps.user_id=s.discord_user_id
                      AND ps.owner_id=CASE WHEN s.guild_id<0 THEN s.discord_user_id ELSE 0 END
                      ORDER BY ps.id DESC LIMIT 1),
                      s.last_match_at+COALESCE(c.inactivity_seconds,1800)) < ?
                """,
                (now,),
            ).fetchall()
            for row in rows:
                total_count = (
                    cast(int, row[10])
                    if row[9] is not None and cast(str, row[9]) == cast(str, row[5])
                    else cast(int, row[6])
                )
                connection.execute(
                    "INSERT INTO rocket_league_events "
                    "(guild_id, discord_user_id, player_id, player_name, event_type, playlist, "
                    "occurred_at, result, event_count, scope, total_count) "
                    "VALUES (?, ?, ?, ?, 'session_streak_completed', 0, ?, ?, ?, ?, ?)",
                    (
                        cast(int, row[0]),
                        cast(int, row[1]),
                        cast(str, row[2]),
                        cast(str, row[3]),
                        now,
                        cast(str, row[5]),
                        cast(int, row[6]),
                        cast(str, row[4]),
                        total_count,
                    ),
                )
            completed = [
                CompletedRocketLeagueStreak(
                    cast(int, row[0]),
                    cast(int, row[1]),
                    cast(str, row[3]),
                    cast(str, row[4]),
                    cast(str, row[5]),
                    cast(int, row[6]),
                    "inactivity",
                    (
                        cast(int, row[10])
                        if row[9] is not None and cast(str, row[9]) == cast(str, row[5])
                        else cast(int, row[6])
                    ),
                    row[9] is not None
                    and cast(str, row[9]) == cast(str, row[5])
                    and cast(int, row[10]) > cast(int, row[6]),
                )
                for row in rows
                if row[8] is not None
                and (
                    cast(int, row[10])
                    if row[9] is not None and cast(str, row[9]) == cast(str, row[5])
                    else cast(int, row[6])
                )
                >= cast(int, row[7])
            ]
            connection.executemany(
                "DELETE FROM rocket_league_combined_streak_state "
                "WHERE guild_id=? AND discord_user_id=? AND streak_class=?",
                ((row[0], row[1], row[4]) for row in rows),
            )
            connection.commit()
            return completed

        return await self._run(operation)

    async def list_rocket_league_player_records(
        self, *, guild_id: int, player_id: str
    ) -> list[RocketLeagueRecord]:
        def operation(connection: sqlite3.Connection) -> list[RocketLeagueRecord]:
            rows = connection.execute(
                "SELECT id, guild_id, discord_user_id, player_id, player_name, scope, "
                "result, streak_count, observed_at "
                "FROM rocket_league_all_time_streak_records "
                "WHERE guild_id = ? AND player_id = ? "
                "ORDER BY scope, result, streak_count DESC, observed_at ASC, id ASC",
                (guild_id, player_id),
            ).fetchall()
            records: list[RocketLeagueRecord] = []
            seen: set[tuple[str, str]] = set()
            for row in rows:
                key = (cast(str, row[5]), cast(str, row[6]))
                if key in seen:
                    continue
                seen.add(key)
                records.append(
                    RocketLeagueRecord(
                        cast(int, row[0]),
                        cast(int, row[1]),
                        cast(int, row[2]),
                        cast(str, row[3]),
                        cast(str, row[4]),
                        key[0],
                        key[1],
                        cast(int, row[7]),
                        cast(int, row[8]),
                    )
                )
            return records

        return await self._run(operation)

    async def list_rocket_league_current_streaks(
        self, *, guild_id: int, discord_user_id: int
    ) -> list[CurrentRocketLeagueStreak]:
        def operation(connection: sqlite3.Connection) -> list[CurrentRocketLeagueStreak]:
            rows = connection.execute(
                "SELECT guild_id, discord_user_id, scope, result, streak_count "
                "FROM rocket_league_all_time_streak_state "
                "WHERE guild_id = ? AND discord_user_id = ? "
                "ORDER BY CASE scope WHEN 'main' THEN 0 ELSE 1 END",
                (guild_id, discord_user_id),
            ).fetchall()
            return [
                CurrentRocketLeagueStreak(
                    guild_id=cast(int, row[0]),
                    discord_user_id=cast(int, row[1]),
                    streak_class=cast(str, row[2]),
                    result=cast(str, row[3]),
                    count=cast(int, row[4]),
                )
                for row in rows
            ]

        return await self._run(operation)

    async def _finish_inactive_rocket_league_streaks_legacy(
        self, *, now: int
    ) -> list[CompletedRocketLeagueStreak]:
        def operation(connection: sqlite3.Connection) -> list[CompletedRocketLeagueStreak]:
            rows = connection.execute(
                """
                SELECT s.guild_id, s.discord_user_id,
                       COALESCE(a.player_name, u.player_name), s.playlist,
                       s.result, s.streak_count,
                       COALESCE(c.streak_threshold, 3), c.streak_channel_id
                FROM rocket_league_streak_state AS s
                LEFT JOIN rocket_league_tracking_config AS c ON c.guild_id = s.guild_id
                LEFT JOIN rocket_league_accounts AS a
                  ON a.guild_id = s.guild_id AND a.discord_user_id = s.discord_user_id
                LEFT JOIN rocket_league_unlinked_accounts AS u
                  ON u.guild_id = s.guild_id AND -u.id = s.discord_user_id
                WHERE s.result IS NOT NULL
                  AND COALESCE((SELECT ps.last_at+ps.cutoff FROM play_sessions ps
                      WHERE ps.guild_id=s.guild_id AND ps.user_id=s.discord_user_id
                      AND ps.owner_id=CASE WHEN s.guild_id<0 THEN s.discord_user_id ELSE 0 END
                      ORDER BY ps.id DESC LIMIT 1),
                      s.last_match_at+COALESCE(c.inactivity_seconds,1800)) < ?
                """,
                (now,),
            ).fetchall()
            for row in rows:
                self._insert_rocket_league_record(
                    connection,
                    guild_id=cast(int, row[0]),
                    discord_user_id=cast(int, row[1]),
                    player_name=cast(str, row[2]),
                    playlist=cast(int, row[3]),
                    category=f"{cast(str, row[4])}_streak",
                    value=float(cast(int, row[5])),
                    match_count=cast(int, row[5]),
                    observed_at=now,
                )
            completed = [
                CompletedRocketLeagueStreak(
                    cast(int, row[0]),
                    cast(int, row[1]),
                    cast(str, row[2]),
                    "main" if cast(int, row[3]) in ROCKET_LEAGUE_MAIN_PLAYLISTS else "special",
                    cast(str, row[4]),
                    cast(int, row[5]),
                    "inactivity",
                )
                for row in rows
                if row[7] is not None and cast(int, row[5]) >= cast(int, row[6])
            ]
            connection.executemany(
                "DELETE FROM rocket_league_streak_state "
                "WHERE guild_id=? AND discord_user_id=? AND playlist=?",
                ((row[0], row[1], row[3]) for row in rows),
            )
            connection.commit()
            return completed

        return await self._run(operation)

    async def list_rocket_league_peak_mmr(
        self, *, guild_id: int, playlist: int, limit: int = 3
    ) -> list[RocketLeaguePeakMMR]:
        if isinstance(playlist, bool) or not 0 < playlist <= MAX_ROCKET_LEAGUE_PLAYLIST_ID:
            raise ValueError("unsupported Rocket League playlist")
        if not 1 <= limit <= 50:
            raise ValueError("peak MMR limit must be between 1 and 50")

        def operation(connection: sqlite3.Connection) -> list[RocketLeaguePeakMMR]:
            rows = connection.execute(
                "SELECT p.guild_id, p.discord_user_id, p.player_id, p.player_name, p.playlist, "
                "p.mmr, p.rank, p.observed_at FROM rocket_league_peak_mmr AS p "
                "WHERE p.guild_id = ? AND p.playlist = ? AND ("
                "EXISTS (SELECT 1 FROM rocket_league_accounts AS a "
                "WHERE a.guild_id=p.guild_id AND a.player_id=p.player_id) OR "
                "EXISTS (SELECT 1 FROM rocket_league_unlinked_accounts AS u "
                "WHERE u.guild_id=p.guild_id AND u.player_id=p.player_id)) "
                "ORDER BY mmr DESC, observed_at ASC, player_id ASC LIMIT ?",
                (guild_id, playlist, limit),
            ).fetchall()
            return [
                RocketLeaguePeakMMR(
                    cast(int, row[0]),
                    cast(int, row[1]),
                    cast(str, row[2]),
                    cast(str, row[3]),
                    cast(int, row[4]),
                    cast(float, row[5]),
                    cast(str, row[6]),
                    cast(int, row[7]),
                )
                for row in rows
            ]

        return await self._run(operation)

    async def get_rocket_league_peak_mmr(
        self, *, guild_id: int, player_id: str, playlist: int
    ) -> RocketLeaguePeakMMR | None:
        if isinstance(playlist, bool) or not 0 < playlist <= MAX_ROCKET_LEAGUE_PLAYLIST_ID:
            raise ValueError("unsupported Rocket League playlist")

        def operation(connection: sqlite3.Connection) -> RocketLeaguePeakMMR | None:
            row = connection.execute(
                "SELECT guild_id, discord_user_id, player_id, player_name, playlist, mmr, rank, "
                "observed_at FROM rocket_league_peak_mmr "
                "WHERE guild_id = ? AND player_id = ? AND playlist = ?",
                (guild_id, player_id, playlist),
            ).fetchone()
            return (
                RocketLeaguePeakMMR(
                    cast(int, row[0]),
                    cast(int, row[1]),
                    cast(str, row[2]),
                    cast(str, row[3]),
                    cast(int, row[4]),
                    cast(float, row[5]),
                    cast(str, row[6]),
                    cast(int, row[7]),
                )
                if row is not None
                else None
            )

        return await self._run(operation)

    async def get_rocket_league_peak_mmr_position(
        self, *, guild_id: int, player_id: str, playlist: int
    ) -> int | None:
        if isinstance(playlist, bool) or not 0 < playlist <= MAX_ROCKET_LEAGUE_PLAYLIST_ID:
            raise ValueError("unsupported Rocket League playlist")

        def operation(connection: sqlite3.Connection) -> int | None:
            row = connection.execute(
                "WITH selected AS ("
                "SELECT p.mmr, p.observed_at FROM rocket_league_peak_mmr AS p "
                "WHERE p.guild_id = ? AND p.player_id = ? AND p.playlist = ? AND ("
                "EXISTS (SELECT 1 FROM rocket_league_accounts AS a "
                "WHERE a.guild_id=p.guild_id AND a.player_id=p.player_id) OR "
                "EXISTS (SELECT 1 FROM rocket_league_unlinked_accounts AS u "
                "WHERE u.guild_id=p.guild_id AND u.player_id=p.player_id))) "
                "SELECT 1 + COUNT(*) FROM rocket_league_peak_mmr AS other, selected "
                "WHERE other.guild_id = ? AND other.playlist = ? AND ("
                "EXISTS (SELECT 1 FROM rocket_league_accounts AS a "
                "WHERE a.guild_id=other.guild_id AND a.player_id=other.player_id) OR "
                "EXISTS (SELECT 1 FROM rocket_league_unlinked_accounts AS u "
                "WHERE u.guild_id=other.guild_id AND u.player_id=other.player_id)) AND ("
                "other.mmr > selected.mmr OR (other.mmr = selected.mmr AND ("
                "other.observed_at < selected.observed_at OR ("
                "other.observed_at = selected.observed_at AND other.player_id < ?)))) "
                "HAVING EXISTS (SELECT 1 FROM selected)",
                (guild_id, player_id, playlist, guild_id, playlist, player_id),
            ).fetchone()
            return cast(int, row[0]) if row is not None else None

        return await self._run(operation)

    async def list_rocket_league_records(
        self, *, guild_id: int, streak_class: str, result: str, limit: int = 3
    ) -> list[RocketLeagueRecord]:
        if streak_class not in ROCKET_LEAGUE_ALL_TIME_SCOPES:
            raise ValueError("unsupported Rocket League streak class")
        if result not in {"win", "loss"}:
            raise ValueError("unsupported Rocket League streak result")
        if not 1 <= limit <= 500:
            raise ValueError("record limit must be between 1 and 500")

        def operation(connection: sqlite3.Connection) -> list[RocketLeagueRecord]:
            rows = connection.execute(
                "SELECT r.id, r.guild_id, r.discord_user_id, r.player_id, r.player_name, "
                "r.scope, r.result, r.streak_count, r.observed_at "
                "FROM rocket_league_all_time_streak_records AS r "
                "WHERE r.guild_id = ? AND r.scope = ? AND r.result = ? AND ("
                "EXISTS (SELECT 1 FROM rocket_league_accounts AS a "
                "WHERE a.guild_id=r.guild_id AND a.player_id=r.player_id) OR "
                "EXISTS (SELECT 1 FROM rocket_league_unlinked_accounts AS u "
                "WHERE u.guild_id=r.guild_id AND u.player_id=r.player_id)) "
                "ORDER BY streak_count DESC, observed_at ASC, id ASC LIMIT ?",
                (guild_id, streak_class, result, limit),
            ).fetchall()
            return [
                RocketLeagueRecord(
                    cast(int, row[0]),
                    cast(int, row[1]),
                    cast(int, row[2]),
                    cast(str, row[3]),
                    cast(str, row[4]),
                    cast(str, row[5]),
                    cast(str, row[6]),
                    cast(int, row[7]),
                    cast(int, row[8]),
                )
                for row in rows
            ]

        return await self._run(operation)

    async def get_rocket_league_record_position(
        self, *, guild_id: int, record_id: int
    ) -> int | None:
        def operation(connection: sqlite3.Connection) -> int | None:
            row = connection.execute(
                "SELECT ranked.position FROM ("
                "SELECT r.id, ROW_NUMBER() OVER ("
                "PARTITION BY scope, result "
                "ORDER BY streak_count DESC, observed_at ASC, id ASC"
                ") AS position FROM rocket_league_all_time_streak_records AS r "
                "WHERE r.guild_id = ? AND ("
                "EXISTS (SELECT 1 FROM rocket_league_accounts AS a "
                "WHERE a.guild_id=r.guild_id AND a.player_id=r.player_id) OR "
                "EXISTS (SELECT 1 FROM rocket_league_unlinked_accounts AS u "
                "WHERE u.guild_id=r.guild_id AND u.player_id=r.player_id))"
                ") AS ranked WHERE ranked.id = ?",
                (guild_id, record_id),
            ).fetchone()
            return cast(int, row[0]) if row is not None else None

        return await self._run(operation)

    async def _list_rocket_league_records_legacy(
        self, *, guild_id: int, playlist: int, category: str, limit: int = 3
    ) -> list[RocketLeagueRecord]:
        if isinstance(playlist, bool) or not 0 < playlist <= MAX_ROCKET_LEAGUE_PLAYLIST_ID:
            raise ValueError("unsupported Rocket League playlist")
        if category not in ROCKET_LEAGUE_RECORD_CATEGORIES:
            raise ValueError("unsupported Rocket League record category")
        if not 1 <= limit <= ROCKET_LEAGUE_RECORDS_PER_BOARD:
            raise ValueError("record limit must be between 1 and 50")

        def operation(connection: sqlite3.Connection) -> list[RocketLeagueRecord]:
            rows = connection.execute(
                "SELECT id, guild_id, discord_user_id, player_name, playlist, category, "
                "value, match_count, observed_at FROM rocket_league_records "
                "WHERE guild_id = ? AND playlist = ? AND category = ? "
                "ORDER BY value DESC, observed_at ASC, id ASC LIMIT ?",
                (guild_id, playlist, category, limit),
            ).fetchall()
            return [
                RocketLeagueRecord(
                    cast(int, row[0]),
                    cast(int, row[1]),
                    cast(int, row[2]),
                    "",
                    cast(str, row[3]),
                    "main" if cast(int, row[4]) in ROCKET_LEAGUE_MAIN_PLAYLISTS else "special",
                    cast(str, row[5]).removesuffix("_streak"),
                    cast(int, row[7]),
                    cast(int, row[8]),
                )
                for row in rows
            ]

        return await self._run(operation)

    @staticmethod
    def _insert_rocket_league_record(
        connection: sqlite3.Connection,
        *,
        guild_id: int,
        discord_user_id: int,
        player_name: str,
        playlist: int,
        category: str,
        value: float,
        match_count: int,
        observed_at: int,
    ) -> None:
        connection.execute(
            "INSERT INTO rocket_league_records "
            "(guild_id, discord_user_id, player_name, playlist, category, value, "
            "match_count, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                guild_id,
                discord_user_id,
                player_name,
                playlist,
                category,
                value,
                match_count,
                observed_at,
            ),
        )
        connection.execute(
            "DELETE FROM rocket_league_records WHERE guild_id = ? AND playlist = ? "
            "AND category = ? AND id NOT IN ("
            "SELECT id FROM rocket_league_records WHERE guild_id = ? AND playlist = ? "
            "AND category = ? ORDER BY value DESC, observed_at ASC, id ASC LIMIT ?)",
            (
                guild_id,
                playlist,
                category,
                guild_id,
                playlist,
                category,
                ROCKET_LEAGUE_RECORDS_PER_BOARD,
            ),
        )

    async def list_rocket_league_mmr_history(
        self,
        *,
        guild_id: int,
        player_id: str,
        playlist: int,
        since: int,
    ) -> list[tuple[int, float, str]]:
        def operation(connection: sqlite3.Connection) -> list[tuple[int, float, str]]:
            rows = connection.execute(
                "SELECT observed_at, mmr, rank FROM rocket_league_mmr_history "
                "WHERE guild_id = ? AND player_id = ? AND playlist = ? AND observed_at >= ? "
                "ORDER BY observed_at ASC, id ASC",
                (guild_id, player_id, playlist, since),
            ).fetchall()
            return [(cast(int, row[0]), cast(float, row[1]), cast(str, row[2])) for row in rows]

        return await self._run(operation)

    async def list_rocket_league_movers(
        self,
        *,
        guild_id: int,
        playlist: int,
        since: int,
        limit: int | None = 10,
    ) -> list[tuple[str, str, int, float, float]]:
        if limit is not None and not 1 <= limit <= 20:
            raise ValueError("mover limit must be between 1 and 20")

        def operation(
            connection: sqlite3.Connection,
        ) -> list[tuple[str, str, int, float, float]]:
            rows = connection.execute(
                "SELECT player_id, player_name, discord_user_id, mmr "
                "FROM rocket_league_mmr_history "
                "WHERE guild_id = ? AND playlist = ? AND observed_at >= ? "
                "ORDER BY observed_at ASC, id ASC",
                (guild_id, playlist, since),
            ).fetchall()
            observations: dict[str, tuple[str, int, float, float]] = {}
            for player_id, player_name, discord_user_id, mmr in rows:
                typed_player_id = cast(str, player_id)
                current = observations.get(typed_player_id)
                if current is None:
                    observations[typed_player_id] = (
                        cast(str, player_name),
                        cast(int, discord_user_id),
                        cast(float, mmr),
                        cast(float, mmr),
                    )
                else:
                    observations[typed_player_id] = (
                        cast(str, player_name),
                        cast(int, discord_user_id),
                        current[2],
                        cast(float, mmr),
                    )
            movers = [
                (player_id, player_name, discord_user_id, first_mmr, last_mmr)
                for player_id, (
                    player_name,
                    discord_user_id,
                    first_mmr,
                    last_mmr,
                ) in observations.items()
                if first_mmr != last_mmr
            ]
            movers.sort(key=lambda item: (-abs(item[4] - item[3]), item[1].casefold()))
            return movers if limit is None else movers[:limit]

        return await self._run(operation)

    async def prune_rocket_league_mmr_history(self, *, before: int) -> int:
        def operation(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                "DELETE FROM rocket_league_mmr_history WHERE observed_at < ?", (before,)
            )
            connection.commit()
            return cursor.rowcount

        return await self._run(operation)

    async def list_rocket_league_match_history(
        self, *, guild_id: int, discord_user_id: int, since: int
    ) -> list[RocketLeagueMatchEvent]:
        def operation(connection: sqlite3.Connection) -> list[RocketLeagueMatchEvent]:
            rows = connection.execute(
                "SELECT result, playlist, match_count, observed_at, mmr_before, mmr_after "
                "FROM rocket_league_match_history "
                "WHERE guild_id = ? AND discord_user_id = ? AND observed_at >= ? "
                "ORDER BY observed_at ASC, id ASC",
                (guild_id, discord_user_id, since),
            ).fetchall()
            return [
                RocketLeagueMatchEvent(
                    cast(str, row[0]),
                    cast(int, row[1]),
                    cast(int, row[2]),
                    cast(int, row[3]),
                    cast(float | None, row[4]),
                    cast(float | None, row[5]),
                )
                for row in rows
            ]

        return await self._run(operation)

    async def record_rocket_league_events(
        self,
        events: Sequence[RocketLeagueEvent],
        *,
        guild_id: int,
        leaderboard_snapshot: tuple[int, Sequence[tuple[int, int, float]]] | None = None,
    ) -> None:
        for event in events:
            if (
                not event.event_type
                or len(event.event_type) > 64
                or not event.event_type.replace("_", "").isalnum()
            ):
                raise ValueError("event type must be a short identifier")
            if not 0 < event.count <= 10_000:
                raise ValueError("event count must be between 1 and 10000")

        def operation(connection: sqlite3.Connection) -> None:
            connection.executemany(
                "INSERT INTO rocket_league_events "
                "(guild_id, discord_user_id, player_id, player_name, event_type, playlist, "
                "occurred_at, result, event_count, mmr_before, mmr_after, rank_before, "
                "rank_after, position_before, position_after, scope, total_count, "
                "notification_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        guild_id,
                        event.discord_user_id,
                        event.player_id,
                        event.player_name,
                        event.event_type,
                        event.playlist,
                        event.occurred_at,
                        event.result,
                        event.count,
                        event.mmr_before,
                        event.mmr_after,
                        event.rank_before,
                        event.rank_after,
                        event.position_before,
                        event.position_after,
                        event.scope,
                        event.total_count,
                        event.notification_text,
                    )
                    for event in events
                ],
            )
            if leaderboard_snapshot is not None:
                playlist, entries = leaderboard_snapshot
                self._replace_leaderboard_snapshot_sync(connection, guild_id, playlist, entries)
            connection.commit()

        await self._run(operation)

    async def list_rocket_league_events(
        self,
        *,
        guild_id: int,
        since: int,
        discord_user_id: int | None = None,
        event_types: Sequence[str] = (),
        event_keys: Sequence[str] | None = None,
        playlist: int | None = None,
        limit: int = 250,
    ) -> list[RocketLeagueEvent]:
        if not 1 <= limit <= 500:
            raise ValueError("event limit must be between 1 and 500")

        def operation(connection: sqlite3.Connection) -> list[RocketLeagueEvent]:
            clauses = ["guild_id = ?", "occurred_at >= ?"]
            parameters: list[object] = [guild_id, since]
            if not event_types:
                clauses.append("event_type != 'leaderboard_update'")
            if discord_user_id is not None:
                clauses.append("discord_user_id = ?")
                parameters.append(discord_user_id)
            if event_types:
                clauses.append(f"event_type IN ({','.join('?' for _ in event_types)})")
                parameters.extend(event_types)
            if event_keys is not None:
                from slopbot.summaries import EVENT_TYPES

                selected = []
                for key in event_keys:
                    kinds = ("match",) if key == "match" else EVENT_TYPES.get(key, ())
                    if not kinds:
                        raise ValueError("Invalid history event category.")
                    condition = f"event_type IN ({','.join('?' for _ in kinds)})"
                    parameters.extend(kinds)
                    if key.startswith("streak_leaderboard_"):
                        condition += " AND scope=?"
                        parameters.append("main" if key.endswith("competitive") else "all")
                    selected.append("(" + condition + ")")
                clauses.append("(" + " OR ".join(selected) + ")" if selected else "0")
            if playlist is not None:
                clauses.append("playlist = ?")
                parameters.append(playlist)
            parameters.append(limit)
            rows = connection.execute(
                "SELECT event_type, discord_user_id, player_id, player_name, playlist, "
                "occurred_at, result, event_count, mmr_before, mmr_after, rank_before, "
                "rank_after, position_before, position_after, scope, total_count, id, "
                "notification_text "
                "FROM rocket_league_events WHERE "
                + " AND ".join(clauses)
                + " ORDER BY occurred_at DESC, id DESC LIMIT ?",
                parameters,
            ).fetchall()
            return [
                RocketLeagueEvent(
                    cast(str, row[0]),
                    cast(int, row[1]),
                    cast(str, row[2]),
                    cast(str, row[3]),
                    cast(int, row[4]),
                    cast(int, row[5]),
                    cast(str | None, row[6]),
                    cast(int, row[7]),
                    cast(float | None, row[8]),
                    cast(float | None, row[9]),
                    cast(str | None, row[10]),
                    cast(str | None, row[11]),
                    cast(int | None, row[12]),
                    cast(int | None, row[13]),
                    cast(str | None, row[14]),
                    cast(int | None, row[15]),
                    cast(int, row[16]),
                    cast(str | None, row[17]),
                )
                for row in rows
            ]

        return await self._run(operation)

    async def configure_rocket_league_event_notification(
        self,
        *,
        guild_id: int,
        event_key: str,
        channel_id: int | None,
        minimum_count: int = 1,
        configured_at: int | None = None,
    ) -> None:
        if event_key not in ROCKET_LEAGUE_EVENT_NOTIFICATION_TYPES:
            raise ValueError("unsupported Rocket League event notification type")
        if not 1 <= minimum_count <= 100:
            raise ValueError("notification minimum must be between 1 and 100")

        def operation(connection: sqlite3.Connection) -> None:
            if channel_id is None:
                connection.execute(
                    "DELETE FROM rocket_league_event_notification_config "
                    "WHERE guild_id = ? AND event_key = ?",
                    (guild_id, event_key),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO rocket_league_event_notification_config
                        (guild_id, event_key, channel_id, minimum_count, configured_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (guild_id, event_key) DO UPDATE SET
                        channel_id = excluded.channel_id,
                        minimum_count = excluded.minimum_count,
                        configured_at = excluded.configured_at
                    """,
                    (
                        guild_id,
                        event_key,
                        channel_id,
                        minimum_count,
                        configured_at if configured_at is not None else int(time.time()),
                    ),
                )
            connection.commit()

        await self._run(operation)

    async def list_rocket_league_event_notification_configs(
        self, *, guild_id: int
    ) -> list[tuple[str, int, int]]:
        def operation(connection: sqlite3.Connection) -> list[tuple[str, int, int]]:
            rows = connection.execute(
                "SELECT event_key, channel_id, minimum_count "
                "FROM rocket_league_event_notification_config WHERE guild_id = ? "
                "ORDER BY event_key",
                (guild_id,),
            ).fetchall()
            return [(cast(str, row[0]), cast(int, row[1]), cast(int, row[2])) for row in rows]

        return await self._run(operation)

    async def list_pending_rocket_league_event_notifications(
        self, *, limit: int = 100
    ) -> list[tuple[RocketLeagueEvent, int, str]]:
        if not 1 <= limit <= 500:
            raise ValueError("notification limit must be between 1 and 500")

        def operation(connection: sqlite3.Connection) -> list[tuple[RocketLeagueEvent, int, str]]:
            event_key = (
                "CASE WHEN e.event_type = 'match' THEN 'match' "
                "WHEN e.event_type IN ('rank_up', 'rank_down') THEN 'rank_change' "
                "WHEN e.event_type = 'peak_mmr' THEN 'peak_mmr' "
                "WHEN e.event_type IN ('streak_completed', 'session_streak_completed') "
                "THEN 'session_streak' "
                "WHEN e.event_type = 'all_time_streak_completed' THEN 'all_time_streak' "
                "WHEN e.event_type IN ('leaderboard_update', 'leaderboard_movement') "
                "THEN 'leaderboard_movement' "
                "WHEN e.event_type = 'streak_leaderboard_movement' AND e.scope = 'main' "
                "THEN 'streak_leaderboard_competitive' "
                "WHEN e.event_type = 'streak_leaderboard_movement' AND e.scope = 'all' "
                "THEN 'streak_leaderboard_all_ranked' END"
            )
            rows = connection.execute(
                "SELECT e.event_type, e.discord_user_id, e.player_id, e.player_name, e.playlist, "
                "e.occurred_at, e.result, e.event_count, e.mmr_before, e.mmr_after, "
                "e.rank_before, e.rank_after, e.position_before, e.position_after, e.scope, "
                "e.total_count, e.id, c.channel_id, c.event_key, e.notification_text "
                "FROM rocket_league_events AS e "
                "JOIN rocket_league_event_notification_config AS c ON c.guild_id = e.guild_id "
                f"AND c.event_key = {event_key} "
                "LEFT JOIN rocket_league_event_notification_deliveries AS d "
                "ON d.event_id = e.id AND d.event_key = c.event_key "
                "WHERE d.event_id IS NULL AND e.notification_eligible=1 "
                "AND e.occurred_at >= c.configured_at "
                "AND (e.event_type != 'leaderboard_movement' OR NOT EXISTS ("
                "SELECT 1 FROM rocket_league_events AS summary "
                "WHERE summary.guild_id = e.guild_id AND summary.playlist = e.playlist "
                "AND summary.occurred_at = e.occurred_at "
                "AND summary.event_type = 'leaderboard_update')) "
                "AND CASE WHEN c.event_key IN ('session_streak', 'all_time_streak') "
                "THEN COALESCE(e.total_count, e.event_count) ELSE 1 END >= c.minimum_count "
                "ORDER BY e.id LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                (
                    RocketLeagueEvent(
                        cast(str, row[0]),
                        cast(int, row[1]),
                        cast(str, row[2]),
                        cast(str, row[3]),
                        cast(int, row[4]),
                        cast(int, row[5]),
                        cast(str | None, row[6]),
                        cast(int, row[7]),
                        cast(float | None, row[8]),
                        cast(float | None, row[9]),
                        cast(str | None, row[10]),
                        cast(str | None, row[11]),
                        cast(int | None, row[12]),
                        cast(int | None, row[13]),
                        cast(str | None, row[14]),
                        cast(int | None, row[15]),
                        cast(int, row[16]),
                        cast(str | None, row[19]),
                    ),
                    cast(int, row[17]),
                    cast(str, row[18]),
                )
                for row in rows
            ]

        return await self._run(operation)

    async def mark_rocket_league_event_notification_delivered(
        self, *, event_id: int, event_key: str, channel_id: int, delivered_at: int
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT OR IGNORE INTO rocket_league_event_notification_deliveries "
                "(event_id, event_key, channel_id, delivered_at) VALUES (?, ?, ?, ?)",
                (event_id, event_key, channel_id, delivered_at),
            )
            connection.commit()

        await self._run(operation)

    async def prune_rocket_league_match_history(self, *, before: int) -> int:
        def operation(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                "DELETE FROM rocket_league_match_history WHERE observed_at < ?", (before,)
            )
            connection.commit()
            return cursor.rowcount

        return await self._run(operation)

    async def prune_rocket_league_events(self, *, before: int) -> int:
        def operation(connection: sqlite3.Connection) -> int:
            connection.execute(
                "DELETE FROM rocket_league_event_notification_deliveries WHERE event_id IN "
                "(SELECT id FROM rocket_league_events WHERE occurred_at < ?)",
                (before,),
            )
            cursor = connection.execute(
                "DELETE FROM rocket_league_events WHERE occurred_at < ?", (before,)
            )
            connection.commit()
            return cursor.rowcount

        return await self._run(operation)

    async def get_rocket_league_leaderboard_snapshot(
        self, *, guild_id: int, playlist: int
    ) -> list[tuple[int, int, float]] | None:
        def operation(connection: sqlite3.Connection) -> list[tuple[int, int, float]] | None:
            rows = connection.execute(
                "SELECT discord_user_id, position, mmr FROM rocket_league_leaderboard_snapshot "
                "WHERE guild_id = ? AND playlist = ? ORDER BY position",
                (guild_id, playlist),
            ).fetchall()
            return [(cast(int, r[0]), cast(int, r[1]), cast(float, r[2])) for r in rows] or None

        return await self._run(operation)

    @staticmethod
    def _replace_leaderboard_snapshot_sync(
        connection: sqlite3.Connection,
        guild_id: int,
        playlist: int,
        entries: Sequence[tuple[int, int, float]],
    ) -> None:
        connection.execute(
            "DELETE FROM rocket_league_leaderboard_snapshot WHERE guild_id = ? AND playlist = ?",
            (guild_id, playlist),
        )
        connection.executemany(
            "INSERT INTO rocket_league_leaderboard_snapshot "
            "(guild_id, playlist, discord_user_id, position, mmr) VALUES (?, ?, ?, ?, ?)",
            [(guild_id, playlist, user_id, position, mmr) for user_id, position, mmr in entries],
        )

    async def replace_rocket_league_leaderboard_snapshot(
        self,
        *,
        guild_id: int,
        playlist: int,
        entries: Sequence[tuple[int, int, float]],
    ) -> list[tuple[int, int, float]] | None:
        def operation(connection: sqlite3.Connection) -> list[tuple[int, int, float]] | None:
            rows = connection.execute(
                "SELECT discord_user_id, position, mmr "
                "FROM rocket_league_leaderboard_snapshot "
                "WHERE guild_id = ? AND playlist = ? ORDER BY position",
                (guild_id, playlist),
            ).fetchall()
            previous = [(cast(int, row[0]), cast(int, row[1]), cast(float, row[2])) for row in rows]
            self._replace_leaderboard_snapshot_sync(connection, guild_id, playlist, entries)
            connection.commit()
            return previous if rows else None

        return await self._run(operation)

    async def save_tournament_user_alert(
        self,
        *,
        guild_id: int,
        user_id: int,
        region: str,
        lead_minutes: int,
        team_sizes: tuple[int, ...],
        game_modes: tuple[str, ...],
    ) -> TournamentUserAlert:
        validate_tournament_alert(
            region=region,
            lead_minutes=lead_minutes,
            team_sizes=team_sizes,
            game_modes=game_modes,
        )

        def operation(connection: sqlite3.Connection) -> TournamentUserAlert:
            connection.execute(
                """
                INSERT INTO tournament_user_alerts
                    (guild_id, user_id, region, lead_minutes, team_size, team_sizes, game_modes)
                VALUES (?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT (guild_id, user_id) DO UPDATE SET
                    region = excluded.region,
                    lead_minutes = excluded.lead_minutes,
                    team_size = 0,
                    team_sizes = excluded.team_sizes,
                    game_modes = excluded.game_modes,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    guild_id,
                    user_id,
                    region,
                    lead_minutes,
                    json.dumps(team_sizes, separators=(",", ":")),
                    json.dumps(game_modes, separators=(",", ":")),
                ),
            )
            connection.commit()
            return TournamentUserAlert(
                guild_id, user_id, region, lead_minutes, team_sizes, game_modes
            )

        return await self._run(operation)

    async def get_tournament_user_alert(
        self, *, guild_id: int, user_id: int
    ) -> TournamentUserAlert | None:
        def operation(connection: sqlite3.Connection) -> TournamentUserAlert | None:
            row = connection.execute(
                "SELECT guild_id, user_id, region, lead_minutes, team_sizes, game_modes "
                "FROM tournament_user_alerts WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            if row is None:
                return None
            guild, user, region, lead, sizes, modes = cast(tuple[int, int, str, int, str, str], row)
            return TournamentUserAlert(
                guild, user, region, lead, _decode_team_sizes(sizes), _decode_game_modes(modes)
            )

        return await self._run(operation)

    async def list_tournament_user_alerts(self) -> list[TournamentUserAlert]:
        def operation(connection: sqlite3.Connection) -> list[TournamentUserAlert]:
            rows = connection.execute(
                "SELECT guild_id, user_id, region, lead_minutes, team_sizes, game_modes "
                "FROM tournament_user_alerts ORDER BY guild_id, user_id"
            ).fetchall()
            return [
                TournamentUserAlert(
                    cast(int, row[0]),
                    cast(int, row[1]),
                    cast(str, row[2]),
                    cast(int, row[3]),
                    _decode_team_sizes(cast(str, row[4])),
                    _decode_game_modes(cast(str, row[5])),
                )
                for row in rows
            ]

        return await self._run(operation)

    async def delete_tournament_user_alert(self, *, guild_id: int, user_id: int) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                "DELETE FROM tournament_user_alerts WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            connection.execute(
                "DELETE FROM tournament_alert_deliveries "
                "WHERE target_type = 'user' AND guild_id = ? AND target_id = ?",
                (guild_id, user_id),
            )
            connection.commit()
            return cursor.rowcount == 1

        return await self._run(operation)

    async def save_tournament_channel_alert(
        self,
        *,
        guild_id: int,
        channel_id: int,
        configured_by_user_id: int,
        region: str,
        lead_minutes: int,
        team_sizes: tuple[int, ...],
        game_modes: tuple[str, ...],
        opt_in_lead_minutes: int = 15,
    ) -> TournamentChannelAlert:
        validate_tournament_alert(
            region=region,
            lead_minutes=lead_minutes,
            team_sizes=team_sizes,
            game_modes=game_modes,
        )
        if not 5 <= opt_in_lead_minutes <= 120:
            raise ValueError("opt-in lead time must be between 5 and 120")

        def operation(connection: sqlite3.Connection) -> TournamentChannelAlert:
            connection.execute(
                """
                INSERT INTO tournament_channel_alerts
                    (guild_id, channel_id, configured_by_user_id, region,
                     lead_minutes, team_size, team_sizes, game_modes, opt_in_lead_minutes)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)
                ON CONFLICT (guild_id, channel_id) DO UPDATE SET
                    configured_by_user_id = excluded.configured_by_user_id,
                    region = excluded.region,
                    lead_minutes = excluded.lead_minutes,
                    team_size = 0,
                    team_sizes = excluded.team_sizes,
                    game_modes = excluded.game_modes,
                    opt_in_lead_minutes = excluded.opt_in_lead_minutes,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    guild_id,
                    channel_id,
                    configured_by_user_id,
                    region,
                    lead_minutes,
                    json.dumps(team_sizes, separators=(",", ":")),
                    json.dumps(game_modes, separators=(",", ":")),
                    opt_in_lead_minutes,
                ),
            )
            connection.commit()
            return TournamentChannelAlert(
                guild_id,
                channel_id,
                configured_by_user_id,
                region,
                lead_minutes,
                team_sizes,
                game_modes,
                opt_in_lead_minutes,
            )

        return await self._run(operation)

    async def get_tournament_channel_alert(
        self, *, guild_id: int, channel_id: int
    ) -> TournamentChannelAlert | None:
        def operation(connection: sqlite3.Connection) -> TournamentChannelAlert | None:
            row = connection.execute(
                "SELECT guild_id, channel_id, configured_by_user_id, region, "
                "lead_minutes, team_sizes, game_modes, opt_in_lead_minutes "
                "FROM tournament_channel_alerts "
                "WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            ).fetchone()
            if row is None:
                return None
            guild, channel, configured_by, region, lead, sizes, modes, opt_in_lead = cast(
                tuple[int, int, int, str, int, str, str, int], row
            )
            return TournamentChannelAlert(
                guild,
                channel,
                configured_by,
                region,
                lead,
                _decode_team_sizes(sizes),
                _decode_game_modes(modes),
                opt_in_lead,
            )

        return await self._run(operation)

    async def list_tournament_channel_alerts(self) -> list[TournamentChannelAlert]:
        def operation(connection: sqlite3.Connection) -> list[TournamentChannelAlert]:
            rows = connection.execute(
                "SELECT guild_id, channel_id, configured_by_user_id, region, "
                "lead_minutes, team_sizes, game_modes, opt_in_lead_minutes "
                "FROM tournament_channel_alerts "
                "ORDER BY guild_id, channel_id"
            ).fetchall()
            return [
                TournamentChannelAlert(
                    cast(int, row[0]),
                    cast(int, row[1]),
                    cast(int, row[2]),
                    cast(str, row[3]),
                    cast(int, row[4]),
                    _decode_team_sizes(cast(str, row[5])),
                    _decode_game_modes(cast(str, row[6])),
                    cast(int, row[7]),
                )
                for row in rows
            ]

        return await self._run(operation)

    async def list_tournament_channel_alerts_for_guild(
        self, *, guild_id: int
    ) -> list[TournamentChannelAlert]:
        def operation(connection: sqlite3.Connection) -> list[TournamentChannelAlert]:
            rows = connection.execute(
                "SELECT guild_id, channel_id, configured_by_user_id, region, "
                "lead_minutes, team_sizes, game_modes, opt_in_lead_minutes "
                "FROM tournament_channel_alerts "
                "WHERE guild_id = ? ORDER BY channel_id",
                (guild_id,),
            ).fetchall()
            return [
                TournamentChannelAlert(
                    cast(int, row[0]),
                    cast(int, row[1]),
                    cast(int, row[2]),
                    cast(str, row[3]),
                    cast(int, row[4]),
                    _decode_team_sizes(cast(str, row[5])),
                    _decode_game_modes(cast(str, row[6])),
                    cast(int, row[7]),
                )
                for row in rows
            ]

        return await self._run(operation)

    async def delete_tournament_channel_alert(self, *, guild_id: int, channel_id: int) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                "DELETE FROM tournament_channel_alerts WHERE guild_id = ? AND channel_id = ?",
                (guild_id, channel_id),
            )
            connection.execute(
                "DELETE FROM tournament_alert_deliveries "
                "WHERE target_type = 'channel' AND guild_id = ? AND target_id = ?",
                (guild_id, channel_id),
            )
            connection.commit()
            return cursor.rowcount == 1

        return await self._run(operation)

    async def claim_tournament_delivery(
        self,
        *,
        target_type: str,
        guild_id: int,
        target_id: int,
        tournament_id: int,
        start_time: int,
        lead_minutes: int,
        now: int,
    ) -> bool:
        if target_type not in {"user", "channel"}:
            raise ValueError("target_type must be user or channel")

        def operation(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO tournament_alert_deliveries
                    (target_type, guild_id, target_id, tournament_id, start_time,
                     lead_minutes, created_at, delivered)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    target_type,
                    guild_id,
                    target_id,
                    tournament_id,
                    start_time,
                    lead_minutes,
                    now,
                ),
            )
            if cursor.rowcount == 0:
                cursor = connection.execute(
                    "UPDATE tournament_alert_deliveries SET created_at = ? "
                    "WHERE target_type = ? AND guild_id = ? AND target_id = ? "
                    "AND tournament_id = ? AND start_time = ? AND lead_minutes = ? "
                    "AND delivered = 0 AND created_at <= ?",
                    (
                        now,
                        target_type,
                        guild_id,
                        target_id,
                        tournament_id,
                        start_time,
                        lead_minutes,
                        now - 300,
                    ),
                )
            connection.commit()
            return cursor.rowcount == 1

        return await self._run(operation)

    async def release_tournament_delivery(
        self,
        *,
        target_type: str,
        guild_id: int,
        target_id: int,
        tournament_id: int,
        start_time: int,
        lead_minutes: int,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "DELETE FROM tournament_alert_deliveries WHERE target_type = ? "
                "AND guild_id = ? AND target_id = ? AND tournament_id = ? "
                "AND start_time = ? AND lead_minutes = ?",
                (
                    target_type,
                    guild_id,
                    target_id,
                    tournament_id,
                    start_time,
                    lead_minutes,
                ),
            )
            connection.commit()

        await self._run(operation)

    async def finish_tournament_delivery(
        self,
        *,
        target_type: str,
        guild_id: int,
        target_id: int,
        tournament_id: int,
        start_time: int,
        lead_minutes: int,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE tournament_alert_deliveries SET delivered = 1 "
                "WHERE target_type = ? AND guild_id = ? AND target_id = ? "
                "AND tournament_id = ? AND start_time = ? AND lead_minutes = ?",
                (
                    target_type,
                    guild_id,
                    target_id,
                    tournament_id,
                    start_time,
                    lead_minutes,
                ),
            )
            connection.commit()

        await self._run(operation)

    async def prune_tournament_deliveries(self, *, before: int) -> int:
        def operation(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                "DELETE FROM tournament_alert_deliveries WHERE start_time < ?", (before,)
            )
            connection.commit()
            return cursor.rowcount

        return await self._run(operation)

    async def save_tournament_opt_in_offer(
        self,
        *,
        token: str,
        guild_id: int,
        channel_id: int,
        tournament_id: int,
        tournament_title: str,
        start_time: int,
        region: str,
        lead_minutes: int,
        now: int,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO tournament_opt_in_offers
                    (token, guild_id, channel_id, tournament_id, tournament_title,
                     start_time, region, lead_minutes, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (token) DO UPDATE SET
                    tournament_title = excluded.tournament_title,
                    region = excluded.region
                """,
                (
                    token,
                    guild_id,
                    channel_id,
                    tournament_id,
                    tournament_title,
                    start_time,
                    region,
                    lead_minutes,
                    now,
                ),
            )
            connection.commit()

        await self._run(operation)

    async def subscribe_tournament_opt_in(
        self, *, token: str, guild_id: int, user_id: int, now: int
    ) -> str:
        def operation(connection: sqlite3.Connection) -> str:
            offer = connection.execute(
                "SELECT start_time, lead_minutes FROM tournament_opt_in_offers "
                "WHERE token = ? AND guild_id = ?",
                (token, guild_id),
            ).fetchone()
            if offer is None or cast(int, offer[0]) <= now:
                return "expired"
            cursor = connection.execute(
                "INSERT OR IGNORE INTO tournament_opt_in_subscriptions "
                "(token, user_id, next_attempt_at) VALUES (?, ?, ?)",
                (token, user_id, cast(int, offer[0]) - cast(int, offer[1]) * 60),
            )
            connection.commit()
            return "created" if cursor.rowcount == 1 else "existing"

        return await self._run(operation)

    async def list_due_tournament_opt_ins(
        self, *, now: int, limit: int = 100
    ) -> list[TournamentOptInDelivery]:
        def operation(connection: sqlite3.Connection) -> list[TournamentOptInDelivery]:
            rows = connection.execute(
                """
                SELECT o.token, o.guild_id, s.user_id, o.tournament_id,
                       o.tournament_title, o.start_time, o.region, o.lead_minutes,
                       s.attempt_count
                FROM tournament_opt_in_subscriptions AS s
                JOIN tournament_opt_in_offers AS o ON o.token = s.token
                WHERE s.delivered = 0 AND s.next_attempt_at <= ? AND o.start_time > ?
                ORDER BY s.next_attempt_at, o.start_time
                LIMIT ?
                """,
                (now, now, limit),
            ).fetchall()
            return [
                TournamentOptInDelivery(
                    cast(str, row[0]),
                    cast(int, row[1]),
                    cast(int, row[2]),
                    cast(int, row[3]),
                    cast(str, row[4]),
                    cast(int, row[5]),
                    cast(str, row[6]),
                    cast(int, row[7]),
                    cast(int, row[8]),
                )
                for row in rows
            ]

        return await self._run(operation)

    async def finish_tournament_opt_in(self, *, token: str, user_id: int) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE tournament_opt_in_subscriptions SET delivered = 1 "
                "WHERE token = ? AND user_id = ?",
                (token, user_id),
            )
            connection.commit()

        await self._run(operation)

    async def retry_tournament_opt_in(
        self, *, token: str, user_id: int, next_attempt_at: int, error: str
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE tournament_opt_in_subscriptions SET attempt_count = attempt_count + 1, "
                "next_attempt_at = ?, last_error = ? WHERE token = ? AND user_id = ?",
                (next_attempt_at, error[:500], token, user_id),
            )
            connection.commit()

        await self._run(operation)

    async def prune_tournament_opt_ins(self, *, before: int) -> int:
        def operation(connection: sqlite3.Connection) -> int:
            cursor = connection.execute(
                "DELETE FROM tournament_opt_in_offers WHERE start_time < ?", (before,)
            )
            connection.commit()
            return cursor.rowcount

        return await self._run(operation)

    async def healthcheck(self) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            row = connection.execute("SELECT 1").fetchone()
            return row is not None and cast(int, row[0]) == 1

        try:
            return await self._run(operation)
        except sqlite3.Error:
            return False

    async def set_slopbot_status_user_alert(
        self, *, guild_id: int, user_id: int, enabled: bool
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            if enabled:
                connection.execute(
                    "INSERT OR IGNORE INTO slopbot_status_user_alerts (guild_id, user_id) "
                    "VALUES (?, ?)",
                    (guild_id, user_id),
                )
            else:
                connection.execute(
                    "DELETE FROM slopbot_status_user_alerts WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                )
            connection.commit()

        await self._run(operation)

    async def has_slopbot_status_user_alert(self, *, guild_id: int, user_id: int) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            return (
                connection.execute(
                    "SELECT 1 FROM slopbot_status_user_alerts WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                ).fetchone()
                is not None
            )

        return await self._run(operation)

    async def list_slopbot_status_user_alerts(self) -> list[tuple[int, int]]:
        def operation(connection: sqlite3.Connection) -> list[tuple[int, int]]:
            return [
                (cast(int, row[0]), cast(int, row[1]))
                for row in connection.execute(
                    "SELECT guild_id, user_id FROM slopbot_status_user_alerts"
                ).fetchall()
            ]

        return await self._run(operation)

    async def set_slopbot_status_channel_alert(
        self, *, guild_id: int, channel_id: int, configured_by_user_id: int, enabled: bool
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            if enabled:
                connection.execute(
                    "INSERT INTO slopbot_status_channel_alerts "
                    "(guild_id, channel_id, configured_by_user_id) VALUES (?, ?, ?) "
                    "ON CONFLICT (guild_id, channel_id) DO UPDATE SET "
                    "configured_by_user_id = excluded.configured_by_user_id",
                    (guild_id, channel_id, configured_by_user_id),
                )
            else:
                connection.execute(
                    "DELETE FROM slopbot_status_channel_alerts "
                    "WHERE guild_id = ? AND channel_id = ?",
                    (guild_id, channel_id),
                )
            connection.commit()

        await self._run(operation)

    async def list_slopbot_status_channel_alerts(self) -> list[tuple[int, int]]:
        def operation(connection: sqlite3.Connection) -> list[tuple[int, int]]:
            return [
                (cast(int, row[0]), cast(int, row[1]))
                for row in connection.execute(
                    "SELECT guild_id, channel_id FROM slopbot_status_channel_alerts"
                ).fetchall()
            ]

        return await self._run(operation)

    async def list_slopbot_status_channel_alerts_for_guild(
        self, *, guild_id: int
    ) -> tuple[int, ...]:
        def operation(connection: sqlite3.Connection) -> tuple[int, ...]:
            return tuple(
                cast(int, row[0])
                for row in connection.execute(
                    "SELECT channel_id FROM slopbot_status_channel_alerts "
                    "WHERE guild_id = ? ORDER BY channel_id",
                    (guild_id,),
                ).fetchall()
            )

        return await self._run(operation)

    async def replace_slopbot_status_state(self, *, fingerprint: str) -> str | None:
        def operation(connection: sqlite3.Connection) -> str | None:
            row = connection.execute(
                "SELECT fingerprint FROM slopbot_status_state WHERE id = 1"
            ).fetchone()
            previous = cast(str, row[0]) if row is not None else None
            connection.execute(
                "INSERT INTO slopbot_status_state (id, fingerprint, checked_at) "
                "VALUES (1, ?, ?) ON CONFLICT (id) DO UPDATE SET "
                "fingerprint = excluded.fingerprint, checked_at = excluded.checked_at",
                (fingerprint, int(time.time())),
            )
            connection.commit()
            return previous

        return await self._run(operation)

    async def get_keyword_profile(self, *, guild_id: int, user_id: int) -> KeywordProfile:
        def operation(connection: sqlite3.Connection) -> KeywordProfile:
            profile_row = connection.execute(
                "SELECT enabled, notification_mode FROM keyword_profiles "
                "WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            keyword_rows = connection.execute(
                "SELECT keyword FROM keyword_terms WHERE guild_id = ? AND user_id = ? "
                "ORDER BY normalized_keyword",
                (guild_id, user_id),
            ).fetchall()
            channel_rows = connection.execute(
                "SELECT channel_id FROM keyword_channels WHERE guild_id = ? AND user_id = ? "
                "ORDER BY channel_id",
                (guild_id, user_id),
            ).fetchall()
            return KeywordProfile(
                enabled=bool(profile_row[0]) if profile_row is not None else True,
                notification_mode=(
                    KeywordNotificationMode(cast(str, profile_row[1]))
                    if profile_row is not None
                    else KeywordNotificationMode.ALWAYS
                ),
                keywords=tuple(cast(str, row[0]) for row in keyword_rows),
                channel_ids=tuple(cast(int, row[0]) for row in channel_rows),
            )

        return await self._run(operation)

    async def set_keyword_enabled(self, *, guild_id: int, user_id: int, enabled: bool) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO keyword_profiles (guild_id, user_id, enabled)
                VALUES (?, ?, ?)
                ON CONFLICT (guild_id, user_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id, user_id, int(enabled)),
            )
            connection.commit()

        await self._run(operation)

    async def set_keyword_notification_mode(
        self,
        *,
        guild_id: int,
        user_id: int,
        notification_mode: KeywordNotificationMode,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO keyword_profiles (guild_id, user_id, notification_mode)
                VALUES (?, ?, ?)
                ON CONFLICT (guild_id, user_id) DO UPDATE SET
                    notification_mode = excluded.notification_mode,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id, user_id, notification_mode.value),
            )
            connection.commit()

        await self._run(operation)

    async def add_keyword(self, *, guild_id: int, user_id: int, keyword: str) -> bool:
        normalized = keyword.casefold()

        def operation(connection: sqlite3.Connection) -> bool:
            connection.execute(
                "INSERT OR IGNORE INTO keyword_profiles (guild_id, user_id) VALUES (?, ?)",
                (guild_id, user_id),
            )
            count = cast(
                int,
                connection.execute(
                    "SELECT COUNT(*) FROM keyword_terms WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                ).fetchone()[0],
            )
            if count >= 25:
                raise ValueError("You can configure up to 25 keywords per server.")
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO keyword_terms
                    (guild_id, user_id, keyword, normalized_keyword)
                VALUES (?, ?, ?, ?)
                """,
                (guild_id, user_id, keyword, normalized),
            )
            connection.commit()
            return cursor.rowcount == 1

        return await self._run(operation)

    async def remove_keyword(self, *, guild_id: int, user_id: int, keyword: str) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                "DELETE FROM keyword_terms "
                "WHERE guild_id = ? AND user_id = ? AND normalized_keyword = ?",
                (guild_id, user_id, keyword.casefold()),
            )
            connection.commit()
            return cursor.rowcount == 1

        return await self._run(operation)

    async def set_keyword_channels(
        self, *, guild_id: int, user_id: int, channel_ids: Sequence[int]
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT OR IGNORE INTO keyword_profiles (guild_id, user_id) VALUES (?, ?)",
                (guild_id, user_id),
            )
            connection.execute(
                "DELETE FROM keyword_channels WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            connection.executemany(
                "INSERT INTO keyword_channels (guild_id, user_id, channel_id) VALUES (?, ?, ?)",
                ((guild_id, user_id, channel_id) for channel_id in dict.fromkeys(channel_ids)),
            )
            connection.commit()

        await self._run(operation)

    async def list_keyword_watchers(
        self, *, guild_id: int, channel_id: int
    ) -> list[KeywordWatcher]:
        def operation(connection: sqlite3.Connection) -> list[KeywordWatcher]:
            rows = connection.execute(
                """
                SELECT p.user_id, p.notification_mode, t.keyword
                FROM keyword_profiles AS p
                JOIN keyword_channels AS c
                  ON c.guild_id = p.guild_id AND c.user_id = p.user_id
                JOIN keyword_terms AS t
                  ON t.guild_id = p.guild_id AND t.user_id = p.user_id
                WHERE p.guild_id = ? AND c.channel_id = ? AND p.enabled = 1
                ORDER BY p.user_id, t.normalized_keyword
                """,
                (guild_id, channel_id),
            ).fetchall()
            grouped: dict[int, tuple[KeywordNotificationMode, list[str]]] = {}
            for user_id, notification_mode, keyword in rows:
                typed_user_id = cast(int, user_id)
                if typed_user_id not in grouped:
                    grouped[typed_user_id] = (
                        KeywordNotificationMode(cast(str, notification_mode)),
                        [],
                    )
                grouped[typed_user_id][1].append(cast(str, keyword))
            return [
                KeywordWatcher(
                    user_id=user_id,
                    notification_mode=notification_mode,
                    keywords=tuple(keywords),
                )
                for user_id, (notification_mode, keywords) in grouped.items()
            ]

        return await self._run(operation)

    async def get_geton_enabled(self, *, guild_id: int, user_id: int) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            row = connection.execute(
                "SELECT enabled FROM geton_preferences WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            return bool(row[0]) if row is not None else True

        return await self._run(operation)

    async def set_geton_enabled(self, *, guild_id: int, user_id: int, enabled: bool) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                INSERT INTO geton_preferences (guild_id, user_id, enabled)
                VALUES (?, ?, ?)
                ON CONFLICT (guild_id, user_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (guild_id, user_id, int(enabled)),
            )
            connection.commit()

        await self._run(operation)

    async def schedule_geton(
        self,
        *,
        guild_id: int,
        sender_id: int,
        sender_name: str,
        message: str,
        deliver_at: int,
        recipient_ids: Sequence[int],
    ) -> tuple[int, ...]:
        def operation(connection: sqlite3.Connection) -> tuple[int, ...]:
            accepted: list[int] = []
            for user_id in dict.fromkeys(recipient_ids):
                row = connection.execute(
                    "SELECT enabled FROM geton_preferences WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                ).fetchone()
                if row is None or bool(row[0]):
                    accepted.append(user_id)
            if not accepted:
                return ()
            recently_created = connection.execute(
                """
                SELECT 1
                FROM geton_requests
                WHERE guild_id = ? AND sender_id = ?
                  AND created_at >= datetime('now', ?)
                LIMIT 1
                """,
                (guild_id, sender_id, f"-{GETON_COOLDOWN_SECONDS} seconds"),
            ).fetchone()
            if recently_created is not None:
                raise GetOnRateLimitError(
                    f"Wait {GETON_COOLDOWN_SECONDS} seconds before scheduling another request."
                )
            pending_count = cast(
                int,
                connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM geton_requests AS r
                    WHERE r.guild_id = ? AND r.sender_id = ?
                      AND EXISTS (
                          SELECT 1
                          FROM geton_recipients AS p
                          WHERE p.request_id = r.id AND p.delivered = 0
                      )
                    """,
                    (guild_id, sender_id),
                ).fetchone()[0],
            )
            if pending_count >= MAX_PENDING_GETON_REQUESTS_PER_SENDER:
                raise GetOnRateLimitError(
                    "You already have too many pending get-on requests. Let some deliver first."
                )
            cursor = connection.execute(
                """
                INSERT INTO geton_requests
                    (guild_id, sender_id, sender_name, message, deliver_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (guild_id, sender_id, sender_name, message, deliver_at),
            )
            request_id = cast(int, cursor.lastrowid)
            connection.executemany(
                "INSERT INTO geton_recipients (request_id, user_id) VALUES (?, ?)",
                ((request_id, user_id) for user_id in accepted),
            )
            connection.commit()
            return tuple(accepted)

        return await self._run(operation)

    async def list_due_geton_deliveries(self, *, now: int, limit: int = 100) -> list[GetOnDelivery]:
        def operation(connection: sqlite3.Connection) -> list[GetOnDelivery]:
            rows = connection.execute(
                """
                SELECT r.id, r.guild_id, p.user_id, r.sender_name, r.message,
                       p.attempt_count
                FROM geton_requests AS r
                JOIN geton_recipients AS p ON p.request_id = r.id
                WHERE r.deliver_at <= ? AND p.next_attempt_at <= ? AND p.delivered = 0
                ORDER BY r.deliver_at, r.id, p.user_id
                LIMIT ?
                """,
                (now, now, limit),
            ).fetchall()
            return [GetOnDelivery(*cast(tuple[int, int, int, str, str, int], row)) for row in rows]

        return await self._run(operation)

    async def mark_geton_delivered(self, *, request_id: int, user_id: int) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                "UPDATE geton_recipients SET delivered = 1 WHERE request_id = ? AND user_id = ?",
                (request_id, user_id),
            )
            connection.execute(
                "DELETE FROM geton_requests WHERE id = ? AND NOT EXISTS "
                "(SELECT 1 FROM geton_recipients WHERE request_id = ? AND delivered = 0)",
                (request_id, request_id),
            )
            connection.commit()

        await self._run(operation)

    async def retry_geton_delivery(
        self,
        *,
        request_id: int,
        user_id: int,
        next_attempt_at: int,
        error: str,
    ) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(
                """
                UPDATE geton_recipients
                SET attempt_count = attempt_count + 1,
                    next_attempt_at = ?,
                    last_error = ?
                WHERE request_id = ? AND user_id = ? AND delivered = 0
                """,
                (next_attempt_at, error[:500], request_id, user_id),
            )
            connection.commit()

        await self._run(operation)

    async def _run(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        async with self._lock:
            worker = asyncio.create_task(asyncio.to_thread(self._with_connection, operation))
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Cancelling an await cannot stop SQLite's thread. Keep serialization until
                # it has closed its connection, including during repeated shutdown cancels.
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not worker.cancelled():
                    worker.exception()  # Retrieve errors even when the caller has gone away.
                raise

    def _with_connection(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        connection = sqlite3.connect(self._database_path, timeout=5)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            return operation(connection)
        finally:
            connection.close()

    @staticmethod
    def _initialize_sync(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA journal_mode = WAL")
        had_leaderboard_playlists = (
            connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'rocket_league_leaderboard_playlists'"
            ).fetchone()
            is not None
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                threshold INTEGER NOT NULL CHECK (threshold BETWEEN 2 AND 25),
                notify_on_reach INTEGER NOT NULL CHECK (notify_on_reach IN (0, 1)),
                notify_on_drop INTEGER NOT NULL CHECK (notify_on_drop IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (guild_id, user_id, channel_id),
                CHECK (notify_on_reach = 1 OR notify_on_drop = 1)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_preferences (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                delivery_method TEXT NOT NULL DEFAULT 'discord'
                    CHECK (delivery_method IN ('discord', 'telegram', 'pwa', 'both',
                        'discord_pwa', 'telegram_pwa', 'all')),
                downtime_enabled INTEGER NOT NULL DEFAULT 0
                    CHECK (downtime_enabled IN (0, 1)),
                downtime_start_minute INTEGER NOT NULL DEFAULT 540
                    CHECK (downtime_start_minute BETWEEN 0 AND 1439),
                downtime_end_minute INTEGER NOT NULL DEFAULT 1020
                    CHECK (downtime_end_minute BETWEEN 0 AND 1439),
                downtime_timezone TEXT NOT NULL DEFAULT 'America/New_York',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        notification_schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='notification_preferences'"
        ).fetchone()[0]
        if "'pwa'" not in notification_schema:
            connection.executescript(
                """
                ALTER TABLE notification_preferences RENAME TO notification_preferences_legacy;
                CREATE TABLE notification_preferences (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    delivery_method TEXT NOT NULL DEFAULT 'discord'
                        CHECK (delivery_method IN ('discord', 'telegram', 'pwa', 'both',
                            'discord_pwa', 'telegram_pwa', 'all')),
                    downtime_enabled INTEGER NOT NULL DEFAULT 0
                        CHECK (downtime_enabled IN (0, 1)),
                    downtime_start_minute INTEGER NOT NULL DEFAULT 540
                        CHECK (downtime_start_minute BETWEEN 0 AND 1439),
                    downtime_end_minute INTEGER NOT NULL DEFAULT 1020
                        CHECK (downtime_end_minute BETWEEN 0 AND 1439),
                    downtime_timezone TEXT NOT NULL DEFAULT 'America/New_York',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, user_id)
                );
                INSERT INTO notification_preferences
                    (guild_id, user_id, delivery_method, created_at, updated_at)
                SELECT guild_id, user_id, delivery_method, created_at, updated_at
                FROM notification_preferences_legacy;
                DROP TABLE notification_preferences_legacy;
                """
            )
        notification_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(notification_preferences)")
        }
        for column, declaration in (
            (
                "downtime_enabled",
                "INTEGER NOT NULL DEFAULT 0 CHECK (downtime_enabled IN (0, 1))",
            ),
            (
                "downtime_start_minute",
                "INTEGER NOT NULL DEFAULT 540 CHECK (downtime_start_minute BETWEEN 0 AND 1439)",
            ),
            (
                "downtime_end_minute",
                "INTEGER NOT NULL DEFAULT 1020 CHECK (downtime_end_minute BETWEEN 0 AND 1439)",
            ),
            (
                "downtime_timezone",
                "TEXT NOT NULL DEFAULT 'America/New_York'",
            ),
        ):
            if column not in notification_columns:
                connection.execute(
                    f"ALTER TABLE notification_preferences ADD COLUMN {column} {declaration}"
                )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_links (
                discord_user_id INTEGER PRIMARY KEY,
                telegram_chat_id INTEGER NOT NULL UNIQUE,
                linked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS telegram_link_codes (
                code_hash TEXT PRIMARY KEY,
                discord_user_id INTEGER NOT NULL UNIQUE,
                expires_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rocket_league_accounts (
                guild_id INTEGER NOT NULL,
                discord_user_id INTEGER NOT NULL,
                player_id TEXT NOT NULL,
                player_name TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, discord_user_id),
                UNIQUE (guild_id, player_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rocket_league_unlinked_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                player_id TEXT NOT NULL,
                player_name TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (guild_id, player_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rocket_league_tracking_config (
                guild_id INTEGER PRIMARY KEY,
                streak_channel_id INTEGER,
                streak_threshold INTEGER NOT NULL DEFAULT 3
                    CHECK (streak_threshold BETWEEN 3 AND 20),
                inactivity_seconds INTEGER NOT NULL DEFAULT 1800
                    CHECK (inactivity_seconds BETWEEN 300 AND 86400),
                leaderboard_channel_id INTEGER,
                leaderboard_playlist INTEGER NOT NULL DEFAULT 11,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rocket_league_leaderboard_playlists (
                guild_id INTEGER NOT NULL,
                playlist INTEGER NOT NULL,
                PRIMARY KEY (guild_id, playlist)
            )
            """
        )
        if not had_leaderboard_playlists:
            connection.execute(
                "INSERT OR IGNORE INTO rocket_league_leaderboard_playlists "
                "(guild_id, playlist) "
                "SELECT guild_id, leaderboard_playlist FROM rocket_league_tracking_config "
                "WHERE leaderboard_channel_id IS NOT NULL"
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rocket_league_activity_samples (
                guild_id INTEGER NOT NULL,
                discord_user_id INTEGER NOT NULL,
                player_id TEXT NOT NULL,
                playlist INTEGER NOT NULL,
                matches_played INTEGER NOT NULL,
                win_streak INTEGER NOT NULL,
                mmr REAL NOT NULL,
                tier INTEGER,
                division INTEGER,
                rank TEXT,
                observed_at INTEGER NOT NULL,
                PRIMARY KEY (guild_id, discord_user_id, playlist)
            )
            """
        )
        activity_sample_columns = {
            cast(str, row[1])
            for row in connection.execute("PRAGMA table_info(rocket_league_activity_samples)")
        }
        for column, declaration in (
            ("tier", "INTEGER"),
            ("division", "INTEGER"),
            ("rank", "TEXT"),
        ):
            if column not in activity_sample_columns:
                connection.execute(
                    f"ALTER TABLE rocket_league_activity_samples ADD COLUMN {column} {declaration}"
                )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rocket_league_mmr_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                discord_user_id INTEGER NOT NULL,
                player_id TEXT NOT NULL,
                player_name TEXT NOT NULL,
                playlist INTEGER NOT NULL,
                mmr REAL NOT NULL CHECK (mmr >= 0),
                rank TEXT NOT NULL,
                observed_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_rocket_league_mmr_history_player "
            "ON rocket_league_mmr_history "
            "(guild_id, player_id, playlist, observed_at ASC, id ASC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_rocket_league_mmr_history_movers "
            "ON rocket_league_mmr_history "
            "(guild_id, playlist, observed_at ASC, player_id)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rocket_league_rank_boundaries (
                playlist INTEGER NOT NULL,
                from_tier INTEGER NOT NULL,
                from_division INTEGER NOT NULL,
                to_tier INTEGER NOT NULL,
                to_division INTEGER NOT NULL,
                direction TEXT NOT NULL CHECK (direction IN ('win','loss')),
                mean_mmr REAL NOT NULL,
                mean_results REAL,
                result_sample_count INTEGER NOT NULL DEFAULT 0,
                sample_count INTEGER NOT NULL CHECK (sample_count > 0),
                last_observed INTEGER NOT NULL,
                PRIMARY KEY (
                    playlist,from_tier,from_division,to_tier,to_division,direction
                )
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rocket_league_rank_state_visits (
                guild_id INTEGER NOT NULL,
                discord_user_id INTEGER NOT NULL,
                playlist INTEGER NOT NULL,
                player_id TEXT NOT NULL,
                tier INTEGER NOT NULL,
                division INTEGER NOT NULL,
                entered_matches INTEGER NOT NULL,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                unknown INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id,discord_user_id,playlist)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rocket_league_match_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                discord_user_id INTEGER NOT NULL,
                player_id TEXT NOT NULL,
                playlist INTEGER NOT NULL,
                result TEXT NOT NULL CHECK (result IN ('win', 'loss', 'gap')),
                match_count INTEGER NOT NULL DEFAULT 1 CHECK (match_count > 0),
                observed_at INTEGER NOT NULL,
                mmr_before REAL,
                mmr_after REAL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_rocket_league_match_history_lookup "
            "ON rocket_league_match_history "
            "(guild_id, discord_user_id, observed_at DESC)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rocket_league_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                discord_user_id INTEGER NOT NULL,
                player_id TEXT NOT NULL,
                player_name TEXT NOT NULL,
                event_type TEXT NOT NULL,
                playlist INTEGER NOT NULL,
                occurred_at INTEGER NOT NULL,
                result TEXT,
                event_count INTEGER NOT NULL DEFAULT 1 CHECK (event_count > 0),
                mmr_before REAL,
                mmr_after REAL,
                rank_before TEXT,
                rank_after TEXT,
                position_before INTEGER,
                position_after INTEGER,
                scope TEXT,
                total_count INTEGER,
                source_match_history_id INTEGER UNIQUE
            )
            """
        )
        event_columns = {
            cast(str, row[1])
            for row in connection.execute("PRAGMA table_info(rocket_league_events)")
        }
        if "notification_text" not in event_columns:
            connection.execute("ALTER TABLE rocket_league_events ADD COLUMN notification_text TEXT")
        if "total_count" not in event_columns:
            connection.execute("ALTER TABLE rocket_league_events ADD COLUMN total_count INTEGER")
        if "notification_eligible" not in event_columns:
            connection.execute(
                "ALTER TABLE rocket_league_events "
                "ADD COLUMN notification_eligible INTEGER NOT NULL DEFAULT 1"
            )
        connection.execute(
            """
            INSERT OR IGNORE INTO rocket_league_events
                (guild_id, discord_user_id, player_id, player_name, event_type, playlist,
                 occurred_at, result, event_count, mmr_before, mmr_after,
                 source_match_history_id)
            SELECT h.guild_id, h.discord_user_id, h.player_id,
                   COALESCE(a.player_name, u.player_name, h.player_id), 'match', h.playlist,
                   h.observed_at, h.result, h.match_count, h.mmr_before, h.mmr_after, h.id
            FROM rocket_league_match_history AS h
            LEFT JOIN rocket_league_accounts AS a
              ON a.guild_id = h.guild_id AND a.discord_user_id = h.discord_user_id
            LEFT JOIN rocket_league_unlinked_accounts AS u
              ON u.guild_id = h.guild_id AND -u.id = h.discord_user_id
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_rocket_league_events_lookup "
            "ON rocket_league_events (guild_id, occurred_at DESC, id DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_rocket_league_events_user_type "
            "ON rocket_league_events "
            "(guild_id, discord_user_id, event_type, occurred_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_rocket_league_events_playlist_type "
            "ON rocket_league_events (guild_id, playlist, event_type, occurred_at DESC)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rocket_league_event_notification_config (
                guild_id INTEGER NOT NULL,
                event_key TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                minimum_count INTEGER NOT NULL DEFAULT 1 CHECK (minimum_count BETWEEN 1 AND 100),
                configured_at INTEGER NOT NULL,
                PRIMARY KEY (guild_id, event_key)
            )
            """
        )
        migrated_notification_time = int(time.time())
        connection.execute(
            "INSERT OR IGNORE INTO rocket_league_event_notification_config "
            "(guild_id, event_key, channel_id, minimum_count, configured_at) "
            "SELECT guild_id, 'session_streak', streak_channel_id, streak_threshold, ? "
            "FROM rocket_league_tracking_config WHERE streak_channel_id IS NOT NULL",
            (migrated_notification_time,),
        )
        connection.execute(
            "INSERT OR IGNORE INTO rocket_league_event_notification_config "
            "(guild_id, event_key, channel_id, minimum_count, configured_at) "
            "SELECT guild_id, 'leaderboard_movement', leaderboard_channel_id, 1, ? "
            "FROM rocket_league_tracking_config WHERE leaderboard_channel_id IS NOT NULL",
            (migrated_notification_time,),
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS slopbot_migrations ("
            "name TEXT PRIMARY KEY, applied_at INTEGER NOT NULL)"
        )
        migration = connection.execute(
            "INSERT OR IGNORE INTO slopbot_migrations (name, applied_at) "
            "VALUES ('split_streak_leaderboard_notifications_by_scope', ?)",
            (migrated_notification_time,),
        )
        if migration.rowcount:
            connection.execute(
                "INSERT OR IGNORE INTO rocket_league_event_notification_config "
                "(guild_id, event_key, channel_id, minimum_count, configured_at) "
                "SELECT guild_id, 'streak_leaderboard_competitive', channel_id, 1, configured_at "
                "FROM rocket_league_event_notification_config "
                "WHERE event_key = 'leaderboard_movement'"
            )
            connection.execute(
                "INSERT OR IGNORE INTO rocket_league_event_notification_config "
                "(guild_id, event_key, channel_id, minimum_count, configured_at) "
                "SELECT guild_id, 'streak_leaderboard_all_ranked', channel_id, 1, configured_at "
                "FROM rocket_league_event_notification_config "
                "WHERE event_key = 'leaderboard_movement'"
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rocket_league_event_notification_deliveries (
                event_id INTEGER NOT NULL,
                event_key TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                delivered_at INTEGER NOT NULL,
                PRIMARY KEY (event_id, event_key)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rocket_league_streak_state (
                guild_id INTEGER NOT NULL,
                discord_user_id INTEGER NOT NULL,
                playlist INTEGER NOT NULL,
                result TEXT CHECK (result IN ('win', 'loss')),
                streak_count INTEGER NOT NULL DEFAULT 0 CHECK (streak_count >= 0),
                last_match_at INTEGER,
                PRIMARY KEY (guild_id, discord_user_id, playlist)
            )
            """
        )
        streak_state_columns = {
            cast(str, row[1])
            for row in connection.execute("PRAGMA table_info(rocket_league_streak_state)")
        }
        if "notification_sent" in streak_state_columns:
            # v1.5.0 briefly retained already-announced streaks for a grace period. End those
            # streaks during the strict-timeout migration so they cannot notify a second time.
            connection.execute("DELETE FROM rocket_league_streak_state WHERE notification_sent = 1")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rocket_league_leaderboard_snapshot (
                guild_id INTEGER NOT NULL,
                playlist INTEGER NOT NULL,
                discord_user_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                mmr REAL NOT NULL,
                PRIMARY KEY (guild_id, playlist, discord_user_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rocket_league_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                discord_user_id INTEGER NOT NULL,
                player_name TEXT NOT NULL,
                playlist INTEGER NOT NULL,
                category TEXT NOT NULL
                    CHECK (category IN ('win_streak', 'loss_streak')),
                value REAL NOT NULL CHECK (value > 0),
                match_count INTEGER NOT NULL CHECK (match_count > 0),
                observed_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_rocket_league_records_board "
            "ON rocket_league_records "
            "(guild_id, playlist, category, value DESC, observed_at ASC)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rocket_league_combined_streak_state (
                guild_id INTEGER NOT NULL,
                discord_user_id INTEGER NOT NULL,
                streak_class TEXT NOT NULL CHECK (streak_class IN ('main', 'special')),
                result TEXT NOT NULL CHECK (result IN ('win', 'loss')),
                streak_count INTEGER NOT NULL CHECK (streak_count > 0),
                last_match_at INTEGER NOT NULL,
                PRIMARY KEY (guild_id, discord_user_id, streak_class)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rocket_league_combined_streak_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                discord_user_id INTEGER NOT NULL,
                player_id TEXT NOT NULL,
                player_name TEXT NOT NULL,
                streak_class TEXT NOT NULL CHECK (streak_class IN ('main', 'special')),
                result TEXT NOT NULL CHECK (result IN ('win', 'loss')),
                streak_count INTEGER NOT NULL CHECK (streak_count > 0),
                observed_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_rocket_league_combined_streak_records_board "
            "ON rocket_league_combined_streak_records "
            "(guild_id, streak_class, result, streak_count DESC, observed_at ASC)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rocket_league_all_time_streak_state (
                guild_id INTEGER NOT NULL,
                discord_user_id INTEGER NOT NULL,
                scope TEXT NOT NULL CHECK (scope IN ('main', 'all')),
                result TEXT NOT NULL CHECK (result IN ('win', 'loss')),
                streak_count INTEGER NOT NULL CHECK (streak_count > 0),
                PRIMARY KEY (guild_id, discord_user_id, scope)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rocket_league_all_time_streak_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                discord_user_id INTEGER NOT NULL,
                player_id TEXT NOT NULL,
                player_name TEXT NOT NULL,
                scope TEXT NOT NULL CHECK (scope IN ('main', 'all')),
                result TEXT NOT NULL CHECK (result IN ('win', 'loss')),
                streak_count INTEGER NOT NULL CHECK (streak_count > 0),
                observed_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_rocket_league_all_time_streak_records_board "
            "ON rocket_league_all_time_streak_records "
            "(guild_id, scope, result, streak_count DESC, observed_at ASC, id ASC)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rocket_league_peak_mmr (
                guild_id INTEGER NOT NULL,
                discord_user_id INTEGER NOT NULL,
                player_id TEXT NOT NULL,
                player_name TEXT NOT NULL,
                playlist INTEGER NOT NULL,
                mmr REAL NOT NULL CHECK (mmr >= 0),
                rank TEXT NOT NULL,
                observed_at INTEGER NOT NULL,
                PRIMARY KEY (guild_id, player_id, playlist)
            )
            """
        )
        peak_mmr_columns = {
            cast(str, row[1])
            for row in connection.execute("PRAGMA table_info(rocket_league_peak_mmr)")
        }
        if "rank" not in peak_mmr_columns:
            connection.execute(
                "ALTER TABLE rocket_league_peak_mmr "
                "ADD COLUMN rank TEXT NOT NULL DEFAULT 'Unknown rank'"
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_rocket_league_peak_mmr_board "
            "ON rocket_league_peak_mmr (guild_id, playlist, mmr DESC, observed_at ASC)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS item_shop_watches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                query TEXT NOT NULL DEFAULT '',
                normalized_query TEXT NOT NULL DEFAULT '',
                max_price INTEGER NOT NULL DEFAULT -1 CHECK (max_price >= -1),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (guild_id, user_id, normalized_query, max_price)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS item_shop_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                initialized INTEGER NOT NULL DEFAULT 0 CHECK (initialized IN (0, 1)),
                checked_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS item_shop_snapshot (
                item_key TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                payload TEXT NOT NULL,
                searchable_text TEXT NOT NULL,
                observed_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS item_shop_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                change_type TEXT NOT NULL CHECK (change_type IN ('added', 'updated', 'removed')),
                payload TEXT NOT NULL,
                observed_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_item_shop_changes_observed "
            "ON item_shop_changes (observed_at DESC)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS command_policies (
                guild_id INTEGER NOT NULL,
                command_name TEXT NOT NULL,
                default_visibility TEXT NOT NULL DEFAULT 'public'
                    CHECK (default_visibility IN ('public', 'private')),
                channel_restricted INTEGER NOT NULL DEFAULT 0
                    CHECK (channel_restricted IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, command_name)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS command_policy_channels (
                guild_id INTEGER NOT NULL,
                command_name TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, command_name, channel_id),
                FOREIGN KEY (guild_id, command_name)
                    REFERENCES command_policies (guild_id, command_name) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS item_shop_deliveries (
                change_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0 CHECK (delivered IN (0, 1)),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                PRIMARY KEY (change_id, guild_id, user_id),
                FOREIGN KEY (change_id) REFERENCES item_shop_changes (id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS keyword_profiles (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                notification_mode TEXT NOT NULL DEFAULT 'always'
                    CHECK (notification_mode IN ('always', 'offline_only')),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS keyword_terms (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                keyword TEXT NOT NULL,
                normalized_keyword TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id, normalized_keyword),
                FOREIGN KEY (guild_id, user_id)
                    REFERENCES keyword_profiles (guild_id, user_id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS keyword_channels (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id, channel_id),
                FOREIGN KEY (guild_id, user_id)
                    REFERENCES keyword_profiles (guild_id, user_id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS geton_preferences (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS geton_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                sender_name TEXT NOT NULL,
                message TEXT NOT NULL,
                deliver_at INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS geton_recipients (
                request_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0 CHECK (delivered IN (0, 1)),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                PRIMARY KEY (request_id, user_id),
                FOREIGN KEY (request_id) REFERENCES geton_requests (id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tournament_user_alerts (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                region TEXT NOT NULL DEFAULT 'auto',
                lead_minutes INTEGER NOT NULL DEFAULT 15
                    CHECK (lead_minutes BETWEEN 5 AND 120),
                team_size INTEGER NOT NULL DEFAULT 0
                    CHECK (team_size IN (0, 1, 2, 3)),
                team_sizes TEXT NOT NULL DEFAULT '[1,2,3]',
                game_modes TEXT NOT NULL DEFAULT
                    '["soccar","rumble","hoops","dropshot","snow_day","heatseeker","pentathlon","other"]',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tournament_channel_alerts (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                configured_by_user_id INTEGER NOT NULL,
                region TEXT NOT NULL DEFAULT 'auto',
                lead_minutes INTEGER NOT NULL DEFAULT 15
                    CHECK (lead_minutes BETWEEN 5 AND 120),
                team_size INTEGER NOT NULL DEFAULT 0
                    CHECK (team_size IN (0, 1, 2, 3)),
                team_sizes TEXT NOT NULL DEFAULT '[1,2,3]',
                game_modes TEXT NOT NULL DEFAULT
                    '["soccar","rumble","hoops","dropshot","snow_day","heatseeker","pentathlon","other"]',
                opt_in_lead_minutes INTEGER NOT NULL DEFAULT 15
                    CHECK (opt_in_lead_minutes BETWEEN 5 AND 120),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, channel_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tournament_alert_deliveries (
                target_type TEXT NOT NULL CHECK (target_type IN ('user', 'channel')),
                guild_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                tournament_id INTEGER NOT NULL,
                start_time INTEGER NOT NULL,
                lead_minutes INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0 CHECK (delivered IN (0, 1)),
                PRIMARY KEY (
                    target_type, guild_id, target_id, tournament_id, start_time, lead_minutes
                )
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_tournament_deliveries_start "
            "ON tournament_alert_deliveries (start_time)"
        )
        tournament_delivery_columns = {
            cast(str, row[1])
            for row in connection.execute("PRAGMA table_info(tournament_alert_deliveries)")
        }
        if "delivered" not in tournament_delivery_columns:
            # Pre-migration rows represented completed deliveries. Preserve their deduplication
            # semantics while allowing new rows to act as reclaimable in-progress leases.
            connection.execute(
                "ALTER TABLE tournament_alert_deliveries ADD COLUMN delivered "
                "INTEGER NOT NULL DEFAULT 1 CHECK (delivered IN (0, 1))"
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tournament_opt_in_offers (
                token TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                tournament_id INTEGER NOT NULL,
                tournament_title TEXT NOT NULL,
                start_time INTEGER NOT NULL,
                region TEXT NOT NULL,
                lead_minutes INTEGER NOT NULL CHECK (lead_minutes BETWEEN 5 AND 120),
                created_at INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tournament_opt_in_subscriptions (
                token TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0 CHECK (delivered IN (0, 1)),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at INTEGER NOT NULL,
                last_error TEXT,
                PRIMARY KEY (token, user_id),
                FOREIGN KEY (token) REFERENCES tournament_opt_in_offers (token) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_tournament_opt_ins_due "
            "ON tournament_opt_in_subscriptions (delivered, next_attempt_at)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS slopbot_status_user_alerts (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS slopbot_status_channel_alerts (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                configured_by_user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, channel_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS slopbot_status_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                fingerprint TEXT NOT NULL,
                checked_at INTEGER NOT NULL
            )
            """
        )
        default_modes = json.dumps(TOURNAMENT_GAME_MODES, separators=(",", ":"))
        for table in ("tournament_user_alerts", "tournament_channel_alerts"):
            columns = {
                cast(str, row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
            }
            if "team_sizes" not in columns:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN team_sizes TEXT NOT NULL DEFAULT '[1,2,3]'"
                )
                connection.execute(
                    f"UPDATE {table} SET team_sizes = CASE team_size "
                    "WHEN 1 THEN '[1]' WHEN 2 THEN '[2]' WHEN 3 THEN '[3]' "
                    "ELSE '[1,2,3]' END"
                )
            if "game_modes" not in columns:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN game_modes "
                    f"TEXT NOT NULL DEFAULT '{default_modes}'"
                )
        channel_columns = {
            cast(str, row[1])
            for row in connection.execute("PRAGMA table_info(tournament_channel_alerts)")
        }
        if "opt_in_lead_minutes" not in channel_columns:
            connection.execute(
                "ALTER TABLE tournament_channel_alerts ADD COLUMN opt_in_lead_minutes "
                "INTEGER NOT NULL DEFAULT 15 CHECK (opt_in_lead_minutes BETWEEN 5 AND 120)"
            )
        geton_recipient_columns = {
            cast(str, row[1]) for row in connection.execute("PRAGMA table_info(geton_recipients)")
        }
        if "attempt_count" not in geton_recipient_columns:
            connection.execute(
                "ALTER TABLE geton_recipients ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0"
            )
        if "next_attempt_at" not in geton_recipient_columns:
            connection.execute(
                "ALTER TABLE geton_recipients ADD COLUMN next_attempt_at INTEGER NOT NULL DEFAULT 0"
            )
        if "last_error" not in geton_recipient_columns:
            connection.execute("ALTER TABLE geton_recipients ADD COLUMN last_error TEXT")
        keyword_profile_columns = {
            cast(str, row[1]) for row in connection.execute("PRAGMA table_info(keyword_profiles)")
        }
        if "notification_mode" not in keyword_profile_columns:
            connection.execute(
                "ALTER TABLE keyword_profiles ADD COLUMN notification_mode "
                "TEXT NOT NULL DEFAULT 'always'"
            )
        # Older account claims transferred match history but left MMR rows under the
        # negative tracking ID. Repair only rows with a verified link in the same guild.
        for table in ("rocket_league_mmr_history", "rocket_league_peak_mmr"):
            connection.execute(
                f"UPDATE {table} SET discord_user_id = ("
                "SELECT a.discord_user_id FROM rocket_league_accounts AS a "
                f"WHERE a.guild_id = {table}.guild_id AND a.player_id = {table}.player_id) "
                "WHERE discord_user_id < 0 AND EXISTS ("
                "SELECT 1 FROM rocket_league_accounts AS a "
                f"WHERE a.guild_id = {table}.guild_id AND a.player_id = {table}.player_id)"
            )
        connection.execute(
            "DELETE FROM rocket_league_all_time_streak_records "
            "WHERE id IN ("
            "SELECT id FROM ("
            "SELECT id, ROW_NUMBER() OVER ("
            "PARTITION BY guild_id, scope, result "
            "ORDER BY streak_count DESC, observed_at ASC, id ASC"
            ") AS position FROM rocket_league_all_time_streak_records"
            ") WHERE position > ?"
            ")",
            (ROCKET_LEAGUE_STREAK_LEADERBOARD_SIZE,),
        )
        # Obsolete FOMO tables, if present, are retained to avoid destructive migrations.
        sessions.initialize(connection)
        summaries.initialize(connection)
        connection.commit()

    @staticmethod
    def _encode_item_shop_item(item: ItemShopItem) -> str:
        return json.dumps(
            {
                "key": item.key,
                "shop_id": item.shop_id,
                "shop_name": item.shop_name,
                "shop_type": item.shop_type,
                "shop_item_id": item.shop_item_id,
                "title": item.title,
                "description": item.description,
                "image_url": item.image_url,
                "starts_at": item.starts_at,
                "ends_at": item.ends_at,
                "product_ids": item.product_ids,
                "attributes": item.attributes,
                "prices": [
                    {"currency_id": price.currency_id, "amount": price.amount}
                    for price in item.prices
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _decode_item_shop_item(payload: str) -> ItemShopItem:
        raw = json.loads(payload)
        return ItemShopItem(
            key=str(raw["key"]),
            shop_id=int(raw["shop_id"]),
            shop_name=str(raw["shop_name"]),
            shop_type=str(raw["shop_type"]),
            shop_item_id=int(raw["shop_item_id"]),
            title=str(raw["title"]),
            description=str(raw["description"]),
            image_url=str(raw["image_url"]),
            starts_at=int(raw["starts_at"]),
            ends_at=int(raw["ends_at"]) if raw["ends_at"] is not None else None,
            product_ids=tuple(int(value) for value in raw["product_ids"]),
            attributes=tuple((str(key), str(value)) for key, value in raw["attributes"]),
            prices=tuple(
                ItemShopPrice(int(price["currency_id"]), int(price["amount"]))
                for price in raw["prices"]
            ),
        )

    @staticmethod
    def _item_shop_watch_matches(item: ItemShopItem, *, query: str, max_price: int) -> bool:
        if query and not all(term in item.searchable_text for term in query.split()):
            return False
        if max_price < 0:
            return True
        return any(price.amount <= max_price for price in item.prices)

    @staticmethod
    def _finish_item_shop_delivery_sync(
        connection: sqlite3.Connection,
        *,
        change_id: int,
        guild_id: int,
        user_id: int,
        detail: str,
    ) -> None:
        connection.execute(
            "UPDATE item_shop_deliveries SET delivered = 1, last_error = ? "
            "WHERE change_id = ? AND guild_id = ? AND user_id = ?",
            (detail[:500], change_id, guild_id, user_id),
        )
        connection.commit()

    @classmethod
    def _save_sync(
        cls,
        connection: sqlite3.Connection,
        *,
        guild_id: int,
        user_id: int,
        channel_id: int,
        threshold: int,
        notify_on_reach: bool,
        notify_on_drop: bool,
    ) -> Subscription:
        existing = connection.execute(
            """
            SELECT 1 FROM subscriptions
            WHERE guild_id = ? AND user_id = ? AND channel_id = ?
            """,
            (guild_id, user_id, channel_id),
        ).fetchone()
        if existing is None:
            count = cast(
                int,
                connection.execute(
                    "SELECT COUNT(*) FROM subscriptions WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                ).fetchone()[0],
            )
            if count >= MAX_SUBSCRIPTIONS_PER_USER:
                raise SubscriptionLimitError(
                    f"You can have up to {MAX_SUBSCRIPTIONS_PER_USER} alerts in one server."
                )
        connection.execute(
            """
            INSERT INTO subscriptions (
                guild_id, user_id, channel_id, threshold, notify_on_reach, notify_on_drop
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (guild_id, user_id, channel_id) DO UPDATE SET
                threshold = excluded.threshold,
                notify_on_reach = excluded.notify_on_reach,
                notify_on_drop = excluded.notify_on_drop,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                guild_id,
                user_id,
                channel_id,
                threshold,
                int(notify_on_reach),
                int(notify_on_drop),
            ),
        )
        row = connection.execute(
            """
            SELECT id, guild_id, user_id, channel_id, threshold,
                   notify_on_reach, notify_on_drop
            FROM subscriptions
            WHERE guild_id = ? AND user_id = ? AND channel_id = ?
            """,
            (guild_id, user_id, channel_id),
        ).fetchone()
        connection.commit()
        if row is None:
            raise RuntimeError("saved subscription could not be read back")
        return cls._row_to_subscription(row)

    @classmethod
    def _select_many(
        cls, connection: sqlite3.Connection, query: str, parameters: tuple[int, ...]
    ) -> list[Subscription]:
        return [cls._row_to_subscription(row) for row in connection.execute(query, parameters)]

    @staticmethod
    def _row_to_subscription(row: sqlite3.Row | tuple[object, ...]) -> Subscription:
        values = cast(tuple[int, int, int, int, int, int, int], row)
        return Subscription(
            id=values[0],
            guild_id=values[1],
            user_id=values[2],
            channel_id=values[3],
            threshold=values[4],
            notify_on_reach=bool(values[5]),
            notify_on_drop=bool(values[6]),
        )
