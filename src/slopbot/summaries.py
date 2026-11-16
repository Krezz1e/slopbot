"""Calendar reports and durable, opt-in daily/weekly schedules over observed RL data."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import sqlite3
import time
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    from slopbot.database import SubscriptionStore

SECTIONS = {
    "results": "Games, wins, losses and uncertain results",
    "mmr": "Net MMR changes",
    "players": "Player breakdown and most active players",
    "playlists": "Playlist breakdown",
    "comparison": "Comparison with the previous period",
    "activity": "Activity by local calendar day",
    "rank_change": "Rank changes",
    "peak_mmr": "New MMR peaks",
    "session_streak": "Completed session streaks",
    "all_time_streak": "Completed all-time streaks",
    "leaderboard_movement": "Rank leaderboard movements",
    "streak_leaderboard_competitive": "Competitive streak leaderboard entries",
    "streak_leaderboard_all_ranked": "All-ranked streak leaderboard entries",
}
EVENT_TYPES = {
    "rank_change": ("rank_up", "rank_down"),
    "peak_mmr": ("peak_mmr",),
    "session_streak": ("session_streak_completed", "streak_completed"),
    "all_time_streak": ("all_time_streak_completed",),
    "leaderboard_movement": ("leaderboard_movement",),
    "streak_leaderboard_competitive": ("streak_leaderboard_movement",),
    "streak_leaderboard_all_ranked": ("streak_leaderboard_movement",),
}
PLAYLISTS = {
    10: "1v1",
    11: "2v2",
    13: "3v3",
    27: "Hoops",
    28: "Rumble",
    29: "Dropshot",
    30: "Snow Day",
}


def initialize(c: sqlite3.Connection) -> None:
    c.executescript("""
        CREATE TABLE IF NOT EXISTS summary_preferences (
            guild_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, cadence TEXT NOT NULL,
            scope TEXT NOT NULL, config TEXT NOT NULL, next_at INTEGER NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(guild_id,owner_id,cadence,scope)
        );
        CREATE INDEX IF NOT EXISTS summary_due ON summary_preferences(next_at);
        CREATE TABLE IF NOT EXISTS summary_deliveries (
            id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, owner_id INTEGER NOT NULL,
            cadence TEXT NOT NULL, scope TEXT NOT NULL, period_end INTEGER NOT NULL,
            revision INTEGER NOT NULL, due_at INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            retry_at INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
            report TEXT,
            UNIQUE(guild_id,owner_id,cadence,scope,period_end)
        );
        CREATE INDEX IF NOT EXISTS summary_pending ON summary_deliveries(status,retry_at);
        CREATE INDEX IF NOT EXISTS summary_history ON
            rocket_league_match_history(guild_id,observed_at,player_id,playlist);
    """)
    columns = {row[1] for row in c.execute("PRAGMA table_info(summary_deliveries)")}
    for name, declaration in (
        ("detail", "TEXT NOT NULL DEFAULT ''"),
        ("receipts", "TEXT NOT NULL DEFAULT '{}'"),
    ):
        if name not in columns:
            c.execute(f"ALTER TABLE summary_deliveries ADD COLUMN {name} {declaration}")


def defaults() -> dict[str, Any]:
    return dict(
        enabled=False,
        timezone="UTC",
        hour=9,
        minute=0,
        weekday=0,
        minimum=1,
        skip_empty=True,
        top=5,
        sections=list(SECTIONS),
        playlists=[],
        channel_id=None,
        minimum_streak=1,
        minimum_peak_gain=0,
        player_order="activity",
    )


def validate(cadence: str, scope: str, p: dict[str, Any], owner: int) -> dict[str, Any]:
    if cadence not in {"daily", "weekly"} or scope not in {"self", "server"}:
        raise ValueError("Choose daily or weekly, and self or server scope.")
    if owner <= 0 and scope != "server":
        raise ValueError("Channel summaries always cover the server.")
    if set(p) - set(defaults()):
        raise ValueError("Invalid summary settings.")
    p = {**defaults(), **p}
    for key, low, high in [
        ("hour", 0, 23),
        ("minute", 0, 59),
        ("weekday", 0, 6),
        ("minimum", 0, 10000),
        ("top", 1, 15),
        ("minimum_streak", 1, 1000),
        ("minimum_peak_gain", 0, 1000),
    ]:
        if type(p[key]) is not int or not low <= p[key] <= high:
            raise ValueError(f"{key} must be between {low} and {high}.")
    if p["player_order"] not in {"activity", "gains", "losses", "name"}:
        raise ValueError("Choose activity, gains, losses, or name order.")
    if type(p["enabled"]) is not bool or type(p["skip_empty"]) is not bool:
        raise ValueError("Choose valid enabled and skip-empty settings.")
    if not isinstance(p["timezone"], str) or len(p["timezone"]) > 100:
        raise ValueError("Choose an IANA timezone, such as America/La_Paz.")
    try:
        ZoneInfo(p["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Unknown timezone. Use an IANA name such as America/La_Paz.") from exc
    for key, allowed in (("sections", set(SECTIONS)), ("playlists", set(PLAYLISTS))):
        values = p[key]
        if (
            not isinstance(values, list)
            or len(values) > len(allowed)
            or any(type(v) is not (str if key == "sections" else int) for v in values)
            or any(v not in allowed for v in values)
            or len(set(values)) != len(values)
        ):
            raise ValueError(f"Choose supported {key} without duplicates.")
    if not p["sections"]:
        raise ValueError("Include at least one report section.")
    cid = p["channel_id"]
    if cid is not None and (type(cid) is not int or not 0 < cid < 2**63):
        raise ValueError("Choose a valid summary channel.")
    if owner > 0 and cid is not None:
        raise ValueError("Personal summaries use your notification delivery preference.")
    if owner <= 0 and p["enabled"] and cid is None:
        raise ValueError("Choose a channel before enabling server summaries.")
    return p.copy()


def next_delivery(now: int, cadence: str, p: dict[str, Any]) -> int:
    """First future wall-clock occurrence; DST folds fire once, gaps shift forward."""
    zone = ZoneInfo(p["timezone"])
    local = datetime.fromtimestamp(now, zone)
    for offset in range(9):
        day = local.date() + timedelta(days=offset)
        candidate = datetime(day.year, day.month, day.day, p["hour"], p["minute"], tzinfo=zone)
        if cadence == "weekly" and candidate.weekday() != p["weekday"]:
            continue
        stamp = int(candidate.timestamp())
        if stamp > now:
            return stamp
    raise ValueError("Could not calculate the next summary.")


def period(now: int, cadence: str, p: dict[str, Any], offset: int = 1) -> tuple[int, int]:
    """Offset 0 is this incomplete calendar period; 1 is the last completed period."""
    zone = ZoneInfo(p["timezone"])
    local = datetime.fromtimestamp(now, zone)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    days = 1 if cadence == "daily" else 7
    if cadence == "weekly":
        start -= timedelta(days=(start.weekday() - p["weekday"]) % 7)
    start -= timedelta(days=days * offset)
    return int(start.timestamp()), int((start + timedelta(days=days)).timestamp())


def _row(row: Any) -> dict[str, Any]:
    gid, owner, cadence, scope, config, next_at, revision = row
    return dict(
        guild_id=gid,
        owner_id=owner,
        cadence=cadence,
        scope=scope,
        config={**defaults(), **json.loads(config)},
        next_at=next_at,
        revision=revision,
    )


class Summaries:
    def __init__(self, store: SubscriptionStore) -> None:
        self.store = store
        self.cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.pending: dict[str, asyncio.Task[dict[str, Any]]] = {}

    async def close(self) -> None:
        tasks = list(self.pending.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.cache.clear()

    async def settings(self, gid: int, owner: int, cadence: str, scope: str) -> dict[str, Any]:
        def read(c: sqlite3.Connection) -> dict[str, Any]:
            row = c.execute(
                "SELECT guild_id,owner_id,cadence,scope,config,next_at,revision "
                "FROM summary_preferences WHERE guild_id=? AND owner_id=? "
                "AND cadence=? AND scope=?",
                (gid, owner, cadence, scope),
            ).fetchone()
            return (
                _row(row)
                if row
                else dict(
                    guild_id=gid,
                    owner_id=owner,
                    cadence=cadence,
                    scope=scope,
                    config=defaults(),
                    next_at=0,
                    revision=0,
                )
            )

        return await self.store._run(read)

    async def list_settings(self, gid: int, owner: int) -> list[dict[str, Any]]:
        return [
            await self.settings(gid, owner, cadence, scope)
            for cadence in ("daily", "weekly")
            for scope in (("server",) if owner <= 0 else ("self", "server"))
        ]

    async def channel_settings(self, gid: int) -> list[dict[str, Any]]:
        rows = await self.store._run(
            lambda c: [
                _row(r)
                for r in c.execute(
                    "SELECT * FROM summary_preferences WHERE guild_id=? AND owner_id<0 "
                    "ORDER BY cadence,owner_id",
                    (gid,),
                )
            ]
        )
        return await self.list_settings(gid, 0) + rows

    async def save(
        self,
        gid: int,
        owner: int,
        cadence: str,
        scope: str,
        config: dict[str, Any],
        now: int,
        *,
        replace_instant: bool = False,
    ) -> dict[str, Any]:
        p = validate(cadence, scope, config, owner)
        if type(replace_instant) is not bool or (
            replace_instant and (owner > 0 or not p["enabled"])
        ):
            raise ValueError("Replacing instant alerts requires an enabled server channel summary.")
        if gid < 0 and (p["enabled"] or scope != "self"):
            raise ValueError(
                "Personal tracking supports reports; choose a Discord server for alerts."
            )
        due = next_delivery(now, cadence, p) if p["enabled"] else 0

        def write(c: sqlite3.Connection) -> None:
            if (
                owner < 0
                and not c.execute(
                    "SELECT 1 FROM summary_preferences WHERE guild_id=? "
                    "AND owner_id=? AND cadence=? AND scope=?",
                    (gid, owner, cadence, scope),
                ).fetchone()
            ):
                if not p["channel_id"] or owner != -p["channel_id"]:
                    raise ValueError("A new channel schedule must use its destination channel ID.")
                if (
                    c.execute(
                        "SELECT COUNT(*) FROM summary_preferences WHERE guild_id=? AND owner_id<0",
                        (gid,),
                    ).fetchone()[0]
                    >= 20
                ):
                    raise ValueError("This server already has 20 additional channel schedules.")
            c.execute(
                "INSERT INTO summary_preferences VALUES(?,?,?,?,?,?,1) "
                "ON CONFLICT(guild_id,owner_id,cadence,scope) DO UPDATE SET "
                "config=excluded.config,next_at=excluded.next_at,revision=revision+1",
                (gid, owner, cadence, scope, json.dumps(p), due),
            )
            c.execute(
                "UPDATE summary_deliveries SET status='cancelled' WHERE guild_id=? "
                "AND owner_id=? AND cadence=? AND scope=? AND status='pending'",
                (gid, owner, cadence, scope),
            )
            if replace_instant:
                keys = [k for k in p["sections"] if k in EVENT_TYPES]
                if "results" in p["sections"]:
                    keys.append("match")
                c.executemany(
                    "DELETE FROM rocket_league_event_notification_config "
                    "WHERE guild_id=? AND event_key=?",
                    [(gid, key) for key in keys],
                )
                # Clear legacy migration inputs too, so a restart cannot recreate these routes.
                for key, column in (
                    ("session_streak", "streak_channel_id"),
                    ("leaderboard_movement", "leaderboard_channel_id"),
                ):
                    if key in keys:
                        c.execute(
                            f"UPDATE rocket_league_tracking_config SET {column}=NULL "
                            "WHERE guild_id=?",
                            (gid,),
                        )
            c.commit()

        await self.store._run(write)
        return await self.settings(gid, owner, cadence, scope)

    async def due(self, now: int) -> list[dict[str, Any]]:
        def write(c: sqlite3.Connection) -> list[dict[str, Any]]:
            for raw in c.execute(
                "SELECT * FROM summary_preferences WHERE next_at>0 AND next_at<=? "
                "ORDER BY next_at LIMIT 100",
                (now,),
            ).fetchall():
                s = _row(raw)
                p = s["config"]
                # Coalesce an outage to the latest scheduled occurrence, never a backlog flood.
                due = s["next_at"]
                following = next_delivery(due, s["cadence"], p)
                while following <= now:
                    due, following = following, next_delivery(following, s["cadence"], p)
                _, end = period(due, s["cadence"], p)
                c.execute(
                    "UPDATE summary_deliveries SET status='superseded' WHERE guild_id=? "
                    "AND owner_id=? AND cadence=? AND scope=? AND status='pending' AND due_at<?",
                    (s["guild_id"], s["owner_id"], s["cadence"], s["scope"], due),
                )
                c.execute(
                    "INSERT OR IGNORE INTO summary_deliveries "
                    "(guild_id,owner_id,cadence,scope,period_end,revision,due_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        s["guild_id"],
                        s["owner_id"],
                        s["cadence"],
                        s["scope"],
                        end,
                        s["revision"],
                        due,
                    ),
                )
                c.execute(
                    "UPDATE summary_preferences SET next_at=? WHERE guild_id=? "
                    "AND owner_id=? AND cadence=? AND scope=?",
                    (following, s["guild_id"], s["owner_id"], s["cadence"], s["scope"]),
                )
            c.execute(
                "UPDATE summary_deliveries SET status='expired' WHERE status='pending' "
                "AND due_at<?",
                (now - 2 * 86400,),
            )
            c.execute("DELETE FROM summary_deliveries WHERE due_at<?", (now - 30 * 86400,))
            rows = c.execute(
                "SELECT id,guild_id,owner_id,cadence,scope,period_end,revision,"
                "due_at,attempts,retry_at,status FROM summary_deliveries "
                "WHERE status='pending' AND retry_at<=? ORDER BY id LIMIT 25",
                (now,),
            ).fetchall()
            c.commit()
            return [
                dict(
                    zip(
                        (
                            "id",
                            "guild_id",
                            "owner_id",
                            "cadence",
                            "scope",
                            "period_end",
                            "revision",
                            "due_at",
                            "attempts",
                            "retry_at",
                            "status",
                        ),
                        r,
                        strict=True,
                    )
                )
                for r in rows
            ]

        return await self.store._run(write)

    async def finish(
        self,
        job: dict[str, Any],
        status: str,
        now: int,
        report: dict[str, Any] | None = None,
        *,
        detail: str = "",
        receipts: dict[str, Any] | None = None,
    ) -> None:
        retry = status == "retry" and job["attempts"] < 4

        def write(c: sqlite3.Connection) -> None:
            c.execute(
                "UPDATE summary_deliveries SET status=?,attempts=attempts+1,retry_at=?,"
                "report=COALESCE(report,?),detail=?,receipts=COALESCE(?,receipts) "
                "WHERE id=? AND status='pending'",
                (
                    "pending" if retry else "failed" if status == "retry" else status,
                    now + min(3600, 60 * 2 ** job["attempts"]) if retry else 0,
                    json.dumps(report) if report else None,
                    detail,
                    json.dumps(receipts) if receipts is not None else None,
                    job["id"],
                ),
            )
            c.commit()

        await self.store._run(write)

    async def deliveries(
        self, gid: int, owner: int, *, channels: bool = False
    ) -> list[dict[str, Any]]:
        return await self.store._run(
            lambda c: [
                dict(
                    zip(
                        (
                            "cadence",
                            "scope",
                            "period_end",
                            "due_at",
                            "status",
                            "attempts",
                            "id",
                            "has_report",
                            "owner_id",
                            "detail",
                            "receipts",
                        ),
                        r,
                        strict=True,
                    )
                )
                for r in c.execute(
                    "SELECT cadence,scope,period_end,due_at,status,attempts,id,report IS NOT NULL,"
                    "CAST(owner_id AS TEXT),detail,receipts "
                    "FROM summary_deliveries WHERE guild_id=? AND "
                    + ("owner_id<=? " if channels else "owner_id=? ")
                    + "ORDER BY due_at DESC LIMIT 30",
                    (gid, owner),
                )
            ]
        )

    async def receipts(self, job: dict[str, Any]) -> dict[str, Any]:
        return await self.store._run(
            lambda c: json.loads(
                c.execute(
                    "SELECT receipts FROM summary_deliveries WHERE id=?", (job["id"],)
                ).fetchone()[0]
            )
        )

    async def snapshot(self, gid: int, owner: int, delivery_id: int) -> dict[str, Any] | None:
        def read(c: sqlite3.Connection) -> dict[str, Any] | None:
            row = c.execute(
                "SELECT report FROM summary_deliveries WHERE id=? AND guild_id=? AND owner_id=?",
                (delivery_id, gid, owner),
            ).fetchone()
            return json.loads(row[0]) if row and row[0] else None

        return await self.store._run(read)

    async def prepare(self, job: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
        """Freeze the authorized report before sending so retries preserve its numbers."""

        def write(c: sqlite3.Connection) -> dict[str, Any]:
            c.execute(
                "UPDATE summary_deliveries SET report=COALESCE(report,?) "
                "WHERE id=? AND status='pending'",
                (json.dumps(report), job["id"]),
            )
            row = c.execute(
                "SELECT report FROM summary_deliveries WHERE id=?", (job["id"],)
            ).fetchone()
            c.commit()
            return json.loads(row[0]) if row and row[0] else report

        return await self.store._run(write)

    async def report(
        self,
        gid: int,
        accounts: dict[str, str],
        cadence: str,
        p: dict[str, Any],
        now: int,
        offset: int = 1,
        *,
        date_range: tuple[date, date] | None = None,
    ) -> dict[str, Any]:
        p = validate(cadence, "self", {**p, "channel_id": None}, 1)
        if type(offset) is not int or not 0 <= offset <= (30 if cadence == "daily" else 4):
            raise ValueError("Choose a period within the 30-day observation history.")
        start, end = period(now, cadence, p, offset)
        prior_start, _ = period(now, cadence, p, offset + 1)
        complete = offset != 0
        if date_range is not None:
            first, last = date_range
            zone = ZoneInfo(p["timezone"])
            today = datetime.fromtimestamp(now, zone).date()
            if not today - timedelta(days=29) <= first <= last <= today:
                raise ValueError("Choose ordered From and To dates within the last 30 days.")
            start_local = datetime.combine(first, datetime.min.time(), zone)
            start = int(start_local.timestamp())
            end = int(
                datetime.combine(last + timedelta(days=1), datetime.min.time(), zone).timestamp()
            )
            prior_start = int((start_local - timedelta(days=(last - first).days + 1)).timestamp())
            cadence = "custom"
            complete = end <= now
        key = json.dumps([gid, sorted(accounts.items()), start, end, cadence, p], sort_keys=True)
        cached = self.cache.get(key)
        if cached and cached[0] > time.monotonic():
            return deepcopy(cached[1])

        async def calculate() -> dict[str, Any]:
            result = await self.store._run(
                lambda c: build_report(
                    c,
                    gid,
                    accounts,
                    start,
                    min(end, now),
                    prior_start,
                    p,
                    now,
                    cadence=cadence,
                    complete=complete,
                    scheduled_end=end,
                )
            )
            self.cache = {k: v for k, v in self.cache.items() if v[0] > time.monotonic()}
            if len(self.cache) >= 32:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = (time.monotonic() + 30, result)
            return result

        task = self.pending.get(key)
        if task is None:
            task = asyncio.create_task(calculate())
            self.pending[key] = task
            task.add_done_callback(lambda _: self.pending.pop(key, None))
        return deepcopy(await asyncio.shield(task))


def _totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins, losses, unknown = (sum(r[k] for r in rows) for k in ("wins", "losses", "unknown"))
    changes = [r["mmr"] for r in rows]
    return dict(
        games=wins + losses + unknown,
        wins=wins,
        losses=losses,
        unknown=unknown,
        win_rate=round(100 * wins / (wins + losses), 1) if wins + losses else None,
        mmr=sum(changes) if changes and all(v is not None for v in changes) else None,
    )


def _matches(
    c: sqlite3.Connection,
    gid: int,
    accounts: dict[str, str],
    start: int,
    end: int,
    playlists: list[int],
) -> list[dict[str, Any]]:
    rows = c.execute(
        """
        WITH observations AS (
            SELECT *, FIRST_VALUE(mmr_before) OVER w AS first_mmr,
                LAST_VALUE(mmr_after) OVER w AS last_mmr
            FROM rocket_league_match_history WHERE guild_id=? AND observed_at>=? AND observed_at<?
            WINDOW w AS (PARTITION BY player_id,playlist ORDER BY observed_at,id
                         ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)
        ) SELECT player_id,playlist,
            SUM(CASE WHEN result='win' THEN match_count ELSE 0 END),
            SUM(CASE WHEN result='loss' THEN match_count ELSE 0 END),
            SUM(CASE WHEN result='gap' THEN match_count ELSE 0 END),
            first_mmr,last_mmr,MIN(observed_at),MAX(observed_at)
        FROM observations GROUP BY player_id,playlist
    """,
        (gid, start, end),
    ).fetchall()
    result = []
    for pid, playlist, wins, losses, unknown, before, after, first_at, last_at in rows:
        if pid not in accounts or (playlists and playlist not in playlists):
            continue
        result.append(
            dict(
                player_id=pid,
                name=accounts[pid],
                playlist=playlist,
                wins=wins,
                losses=losses,
                unknown=unknown,
                before=before,
                after=after,
                mmr=after - before if before is not None and after is not None else None,
                first_at=first_at,
                last_at=last_at,
            )
        )
    return result


def build_report(
    c: sqlite3.Connection,
    gid: int,
    accounts: dict[str, str],
    start: int,
    end: int,
    prior_start: int,
    p: dict[str, Any],
    now: int,
    *,
    cadence: str,
    complete: bool,
    scheduled_end: int,
) -> dict[str, Any]:
    rows = _matches(c, gid, accounts, start, end, p["playlists"])
    by_player: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_playlist: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_player[row["player_id"]].append(row)
        by_playlist[row["playlist"]].append(row)
    players = [
        dict(player_id=pid, name=accounts[pid], **_totals(rs), playlists=rs)
        for pid, rs in by_player.items()
    ]
    players.sort(key=lambda r: (-r["games"], r["name"], r["player_id"]))
    playlists = [
        dict(playlist=key, name=PLAYLISTS.get(key, str(key)), **_totals(rs))
        for key, rs in sorted(by_playlist.items())
    ]
    totals = _totals(rows)
    sections = p["sections"]
    highlights: dict[str, Any] = {}
    for category, event_types in EVENT_TYPES.items():
        if category not in sections:
            continue
        marks = ",".join("?" for _ in event_types)
        threshold_sql = ""
        thresholds: tuple[int, ...] = ()
        if "streak" in category:
            threshold_sql = " AND event_count>=? "
            thresholds = (p["minimum_streak"],)
        elif category == "peak_mmr" and p["minimum_peak_gain"]:
            threshold_sql = " AND mmr_after-mmr_before>=? "
            thresholds = (p["minimum_peak_gain"],)
        events = c.execute(
            "WITH events AS (SELECT *, FIRST_VALUE(rank_before) OVER w AS first_rank, "
            "LAST_VALUE(rank_after) OVER w AS last_rank, "
            "FIRST_VALUE(position_before) OVER w AS first_position, "
            "LAST_VALUE(position_after) OVER w AS last_position "
            "FROM rocket_league_events WHERE guild_id=? AND occurred_at>=? AND occurred_at<? "
            f"AND event_type IN ({marks}) {threshold_sql} WINDOW w AS "
            "(PARTITION BY player_id,playlist,result,scope "
            "ORDER BY occurred_at,id ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)) "
            "SELECT player_id,playlist,result,COUNT(*),MAX(event_count),MAX(mmr_after),"
            "MIN(position_after),MAX(occurred_at),scope,first_rank,last_rank,"
            "first_position,last_position FROM events GROUP BY player_id,playlist,result,scope",
            (gid, start, end, *event_types, *thresholds),
        ).fetchall()
        items = [
            dict(
                player_id=pid,
                name=accounts[pid],
                playlist=pl,
                result=res,
                events=count,
                best=best,
                mmr=mmr,
                position=pos,
                at=at,
                scope=scope,
                rank_before=first_rank,
                rank_after=last_rank,
                position_before=first_position,
                position_after=last_position,
            )
            for (
                pid,
                pl,
                res,
                count,
                best,
                mmr,
                pos,
                at,
                scope,
                first_rank,
                last_rank,
                first_position,
                last_position,
            ) in events
            if pid in accounts
            and (not p["playlists"] or pl in p["playlists"] or pl == 0)
            and (category != "streak_leaderboard_competitive" or scope == "main")
            and (category != "streak_leaderboard_all_ranked" or scope == "all")
        ]
        items.sort(key=lambda r: (-r["best"], -r["events"], -r["at"], r["name"]))
        if "streak" in category:
            items = [r for r in items if r["best"] >= p["minimum_streak"]]
        if category == "peak_mmr":
            gains = {
                (pid, pl): gain
                for pid, pl, gain in c.execute(
                    "SELECT player_id,playlist,MAX(mmr_after-mmr_before) FROM rocket_league_events "
                    "WHERE guild_id=? AND occurred_at>=? AND occurred_at<? "
                    "AND event_type='peak_mmr' "
                    "GROUP BY player_id,playlist",
                    (gid, start, end),
                )
            }
            for item in items:
                item["gain"] = gains.get((item["player_id"], item["playlist"]))
            items = [r for r in items if (r["gain"] or 0) >= p["minimum_peak_gain"]]
            items.sort(key=lambda r: (-(r["gain"] or 0), -r["events"], r["name"]))
        highlights[category] = dict(
            count=sum(r["events"] for r in items),
            items=items,
            omitted=max(0, len(items) - p["top"]),
        )
    previous = _totals(_matches(c, gid, accounts, prior_start, start, p["playlists"]))
    daily = []
    if "activity" in sections:
        zone = ZoneInfo(p["timezone"])
        day = datetime.fromtimestamp(start, zone)
        while day.timestamp() < end:
            following = day + timedelta(days=1)
            totals_for_day = _totals(
                _matches(
                    c,
                    gid,
                    accounts,
                    int(day.timestamp()),
                    min(end, int(following.timestamp())),
                    p["playlists"],
                )
            )
            daily.append(dict(date=day.strftime("%Y-%m-%d"), **totals_for_day))
            day = following
    # Totals are always available for schedule thresholds, but rendering obeys section selection.
    order = p["player_order"]
    if order == "name":
        players.sort(key=lambda r: r["name"].casefold())
    elif order in {"gains", "losses"}:
        players.sort(
            key=lambda r: (
                r["mmr"] is None,
                (r["mmr"] or 0) * (-1 if order == "gains" else 1),
                r["name"],
            )
        )
    return dict(
        guild_id=str(gid),
        profile_ids=sorted(accounts),
        config=p,
        cadence=cadence,
        start=start,
        end=end,
        scheduled_end=scheduled_end,
        timezone=p["timezone"],
        complete=complete,
        generated_at=now,
        sections=sections,
        totals=totals,
        active_players=len(players),
        tracked_players=len(accounts),
        players=players,
        playlists=playlists,
        highlights=highlights,
        previous=previous,
        comparison_available=prior_start >= now - 30 * 86400,
        retained_partial=start < now - 30 * 86400,
        top=p["top"],
        daily=daily,
    )


def highlight_text(category: str, item: dict[str, Any]) -> str:
    mode = PLAYLISTS.get(item["playlist"], item.get("scope") or "All playlists")
    prefix = f"{item['name']} · {mode}"
    if category == "peak_mmr":
        return f"{prefix}: peak {item['mmr']:.0f} MMR" if item["mmr"] is not None else prefix
    if "streak" in category:
        return f"{prefix}: {item['best']}-game {item['result'] or ''} streak"
    if category == "rank_change":
        return f"{prefix}: {item['rank_before']} → {item['rank_after']} ({item['events']} changes)"
    if category == "leaderboard_movement":
        return f"{prefix}: #{item['position_before'] or 'new'} → #{item['position_after']}"
    return f"{prefix}: {item['events']} change(s)"


def report_text(r: dict[str, Any], *, compact: bool = True) -> str:
    """Preserve every selected section; share formatting with full report downloads."""
    zone = ZoneInfo(r["timezone"])

    def clean(value: Any) -> str:
        text = " ".join(str(value).split())
        return text[:60] if compact else text

    def metrics(item: dict[str, Any]) -> str:
        parts = []
        if "results" in r["sections"]:
            parts.append(
                f"{item['games']} games · {item['wins']}W / {item['losses']}L / "
                f"{item['unknown']} uncertain"
            )
        if "mmr" in r["sections"]:
            parts.append(
                f"{item['mmr']:+.0f} MMR" if item["mmr"] is not None else "MMR unavailable"
            )
        return " · ".join(parts)

    start = datetime.fromtimestamp(r["start"], zone).strftime("%d %b %Y")
    end = datetime.fromtimestamp(r["scheduled_end"] - 1, zone).strftime("%d %b %Y")
    header = f"{start} to {end} · {r['timezone']}"
    if not r["complete"]:
        header += " · in progress"
    blocks: list[tuple[str, list[str]]] = []
    t = r["totals"]
    for section in r["sections"]:
        rows: list[str] = []
        title = SECTIONS[section]
        if section == "results":
            rows = [
                f"{t['games']} games · {t['wins']}W / {t['losses']}L / {t['unknown']} uncertain",
                f"{t['win_rate']}% known-result win rate"
                if t["win_rate"] is not None
                else "No known outcomes",
            ]
        elif section == "mmr":
            rows = [f"Net MMR: {t['mmr']:+.0f}" if t["mmr"] is not None else "Net MMR: unavailable"]
        elif section == "comparison":
            if r["comparison_available"] and r["complete"]:
                previous = r["previous"]
                if "results" in r["sections"]:
                    rows.append(
                        f"Games versus previous period: {t['games'] - previous['games']:+d}"
                    )
                if "mmr" in r["sections"] and t["mmr"] is not None and previous["mmr"] is not None:
                    rows.append(
                        f"Net MMR versus previous period: {t['mmr'] - previous['mmr']:+.0f}"
                    )
            if not rows:
                rows = ["Comparison unavailable for this period or selected metrics."]
        elif section in {"players", "playlists"}:
            title += f" ({len(r[section])})"
            rows = [f"{clean(item['name'])}: {metrics(item)}" for item in r[section]]
        elif section == "activity":
            rows = [
                f"{d['date']}: {metrics(d) or str(d['games']) + ' games'}"
                for d in r.get("daily", [])
            ]
        elif section in r["highlights"]:
            group = r["highlights"][section]
            title += f" ({group['count']})"
            rows = [
                highlight_text(section, {**item, "name": clean(item["name"])})
                for item in group["items"]
            ]
        blocks.append((title, rows or ["No matching observed activity."]))
    footer = "Observed ranked activity only; uncertain results are never guessed."
    if not compact:
        return (
            header
            + "\n\n"
            + "\n\n".join(title + "\n" + "\n".join(rows) for title, rows in blocks)
            + "\n\n"
            + footer
        )
    # All section headings and counts survive. Allocate detail fairly across sections,
    # rather than letting early player rows crowd out records and leaderboard changes.
    counts = [0] * len(blocks)

    def render() -> str:
        chunks = []
        for index, (title, rows) in enumerate(blocks):
            visible = rows[: counts[index]]
            omitted = len(rows) - counts[index]
            chunks.append(
                title
                + "\n"
                + "\n".join(visible + ([f"+ {omitted} detail(s) in Reports"] if omitted else []))
            )
        return header + "\n\n" + "\n\n".join(chunks) + "\n\n" + footer

    for _ in range(r["top"]):
        for index, (_, rows) in enumerate(blocks):
            if counts[index] >= len(rows):
                continue
            counts[index] += 1
            if len(render()) + sum(render().count(c) for c in "_*~`|>\\@") > 3500:
                counts[index] -= 1
    return render()


def report_csv(r: dict[str, Any]) -> str:
    """Export every selected row, with spreadsheet-formula-safe text cells."""
    output = io.StringIO(newline="")
    writer = csv.writer(output)

    def row(*values: Any) -> None:
        writer.writerow(
            [
                "'" + value
                if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@"))
                else value
                for value in values
            ]
        )

    row("Report", r["cadence"], "Timezone", r["timezone"], "Start", r["start"], "End", r["end"])
    row(
        "Status",
        "Complete" if r["complete"] else "In progress",
        "Partial retained history",
        r["retained_partial"],
    )
    results, mmr = "results" in r["sections"], "mmr" in r["sections"]
    for section in r["sections"]:
        row()
        row(SECTIONS[section])
        rows = []
        if section in {"results", "mmr"}:
            rows = [{"name": "Total", **r["totals"]}]
        elif section in {"players", "playlists"}:
            rows = r[section]
        elif section == "activity":
            rows = [{"name": d["date"], **d} for d in r["daily"]]
        elif section == "comparison":
            if r["complete"] and r["comparison_available"]:
                rows = [{"name": "Previous period", **r["previous"]}]
            else:
                row("Comparison unavailable")
        elif section in r["highlights"]:
            row("Player", "Playlist", "Details", "Events", "Observed at")
            for item in r["highlights"][section]["items"]:
                row(
                    item["name"],
                    PLAYLISTS.get(item["playlist"], item.get("scope") or "All"),
                    highlight_text(section, item),
                    item["events"],
                    item["at"],
                )
            continue
        use_results = results and section != "mmr"
        use_mmr = mmr and section != "results"
        games_only = section in {"players", "playlists", "activity"} and not results and not mmr
        row(
            "Name",
            *(["Games", "Wins", "Losses", "Uncertain", "Win rate"] if use_results else []),
            *(["Net MMR"] if use_mmr else []),
            *(["Games"] if games_only else []),
        )
        for item in rows:
            row(
                item["name"],
                *(
                    [item[k] for k in ("games", "wins", "losses", "unknown", "win_rate")]
                    if use_results
                    else []
                ),
                *([item["mmr"]] if use_mmr else []),
                *([item["games"]] if games_only else []),
            )
    return output.getvalue()
