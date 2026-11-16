"""Durable play sessions over the tracker's observed games, shared by web and Discord."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from slopbot.database import SubscriptionStore


def initialize(c: sqlite3.Connection) -> None:
    c.executescript("""
        CREATE TABLE IF NOT EXISTS play_session_preferences (
            guild_id INTEGER NOT NULL, owner_id INTEGER NOT NULL,
            cutoff INTEGER NOT NULL DEFAULT 1800, enabled INTEGER NOT NULL DEFAULT 0,
            channel_id INTEGER, minimum INTEGER NOT NULL DEFAULT 1,
            include_observations INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (guild_id, owner_id)
        );
        CREATE TABLE IF NOT EXISTS play_sessions (
            id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, owner_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL, player_id TEXT NOT NULL, player_name TEXT NOT NULL,
            started_at INTEGER NOT NULL, last_at INTEGER NOT NULL, cutoff INTEGER NOT NULL,
            closed INTEGER NOT NULL DEFAULT 0, notified INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            retry_at INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS play_sessions_lookup
            ON play_sessions(guild_id,owner_id,player_id,last_at);
        CREATE TABLE IF NOT EXISTS play_session_games (
            session_id INTEGER NOT NULL REFERENCES play_sessions(id) ON DELETE CASCADE,
            history_id INTEGER NOT NULL, playlist INTEGER NOT NULL, result TEXT NOT NULL,
            match_count INTEGER NOT NULL, observed_at INTEGER NOT NULL,
            mmr_before REAL, mmr_after REAL, PRIMARY KEY(session_id,history_id)
        );
    """)
    columns = {row[1] for row in c.execute("PRAGMA table_info(play_session_preferences)")}
    if "include_observations" not in columns:
        c.execute(
            "ALTER TABLE play_session_preferences ADD COLUMN include_observations "
            "INTEGER NOT NULL DEFAULT 1"
        )


def preferences(c: sqlite3.Connection, guild_id: int, owner_id: int) -> dict[str, Any]:
    row = c.execute(
        "SELECT cutoff,enabled,channel_id,minimum,include_observations "
        "FROM play_session_preferences "
        "WHERE guild_id=? AND owner_id=?",
        (guild_id, owner_id),
    ).fetchone()
    if row is None:
        config = (
            c.execute(
                "SELECT inactivity_seconds FROM rocket_league_tracking_config WHERE guild_id=?",
                (guild_id,),
            ).fetchone()
            if owner_id == 0
            else None
        )
        row = (config[0] if config else 1800, 0, None, 1, 1)
    result = dict(
        zip(
            ("cutoff", "enabled", "channel_id", "minimum", "include_observations"),
            row,
            strict=True,
        )
    )
    if owner_id == 0:
        config = c.execute(
            "SELECT inactivity_seconds FROM rocket_league_tracking_config WHERE guild_id=?",
            (guild_id,),
        ).fetchone()
        if config:
            result["cutoff"] = config[0]
    return result


def ingest(c: sqlite3.Connection, after_id: int, player_name: str) -> None:
    rows = c.execute(
        "SELECT id,guild_id,discord_user_id,player_id,playlist,result,match_count,"
        "observed_at,mmr_before,mmr_after FROM rocket_league_match_history WHERE id>? ORDER BY id",
        (after_id,),
    ).fetchall()
    for hid, gid, uid, pid, playlist, result, count, observed, before, after in rows:
        for owner in [0, uid] if uid > 0 and gid > 0 else [uid] if gid < 0 else [0]:
            current = c.execute(
                "SELECT id,last_at,cutoff FROM play_sessions WHERE guild_id=? AND owner_id=? "
                "AND player_id=? AND closed=0 ORDER BY id DESC LIMIT 1",
                (gid, owner, pid),
            ).fetchone()
            if current and observed > current[1] + current[2]:
                c.execute("UPDATE play_sessions SET closed=1 WHERE id=?", (current[0],))
                current = None
            if current is None:
                p = preferences(c, gid, owner)
                cursor = c.execute(
                    "INSERT INTO play_sessions(guild_id,owner_id,user_id,player_id,player_name,"
                    "started_at,last_at,cutoff,enabled) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        gid,
                        owner,
                        uid,
                        pid,
                        player_name,
                        observed,
                        observed,
                        p["cutoff"],
                        p["enabled"],
                    ),
                )
                sid = cursor.lastrowid
            else:
                sid = current[0]
            c.execute(
                "UPDATE play_sessions SET last_at=?,player_name=? WHERE id=?",
                (observed, player_name, sid),
            )
            c.execute(
                "INSERT OR IGNORE INTO play_session_games VALUES(?,?,?,?,?,?,?,?)",
                (sid, hid, playlist, result, count, observed, before, after),
            )


def session_data(c: sqlite3.Connection, row: Any) -> dict[str, Any]:
    keys = (
        "id",
        "guild_id",
        "owner_id",
        "user_id",
        "player_id",
        "player_name",
        "started_at",
        "last_at",
        "cutoff",
        "closed",
        "notified",
        "enabled",
        "attempts",
        "retry_at",
    )
    data = dict(zip(keys, row, strict=True))
    games = [
        dict(zip(("id", "playlist", "result", "count", "at", "before", "after"), r, strict=True))
        for r in c.execute(
            "SELECT history_id,playlist,result,match_count,observed_at,mmr_before,mmr_after "
            "FROM play_session_games WHERE session_id=? ORDER BY observed_at,history_id",
            (data["id"],),
        )
    ]
    playlists: dict[int, Any] = {}
    for g in games:
        p = playlists.setdefault(
            g["playlist"],
            {
                "playlist": g["playlist"],
                "win": 0,
                "loss": 0,
                "gap": 0,
                "before": g["before"],
                "after": None,
            },
        )
        p[g["result"]] += g["count"]
        p["after"] = g["after"]
    for p in playlists.values():
        p["change"] = (
            p["after"] - p["before"] if p["before"] is not None and p["after"] is not None else None
        )
    playlist_changes = [p["change"] for p in playlists.values()]
    observations: dict[tuple[Any, ...], dict[str, Any]] = {}
    for game in games:
        # Known and uncertain results from the same poll are stored as separate
        # history rows with identical MMR endpoints. Consolidate them before
        # exposing a delta so one observed change is never counted twice.
        key = (game["at"], game["playlist"], game["before"], game["after"])
        observation = observations.setdefault(
            key,
            {
                "at": game["at"],
                "playlist": game["playlist"],
                "total": 0,
                "wins": 0,
                "losses": 0,
                "unknown": 0,
                "before": game["before"],
                "after": game["after"],
            },
        )
        observation["total"] += game["count"]
        observation[{"win": "wins", "loss": "losses", "gap": "unknown"}[game["result"]]] += game[
            "count"
        ]
    for observation in observations.values():
        observation["change"] = (
            observation["after"] - observation["before"]
            if observation["before"] is not None and observation["after"] is not None
            else None
        )
        # Multi-game observations do not reveal exact individual changes.
        observation["per_game_change"] = (
            observation["change"] / observation["total"]
            if observation["change"] is not None and observation["total"] > 0
            else None
        )
    data.update(
        games=games,
        observations=list(observations.values()),
        playlists=list(playlists.values()),
        total=sum(g["count"] for g in games),
        wins=sum(g["count"] for g in games if g["result"] == "win"),
        losses=sum(g["count"] for g in games if g["result"] == "loss"),
        unknown=sum(g["count"] for g in games if g["result"] == "gap"),
        # MMR is tracked independently per playlist. A session total is only
        # reported when every playlist in the session has both endpoints.
        mmr_change=(
            sum(playlist_changes)
            if playlist_changes and all(change is not None for change in playlist_changes)
            else None
        ),
    )
    # Only extend streaks when the ordering is known. Mixed playlists in the same
    # poll have no reliable chronology and must not invent a streak.
    best = {"win": 0, "loss": 0}
    count = 0
    previous = ""
    batches: dict[int, list[Any]] = {}
    for g in games:
        batches.setdefault(g["at"], []).append(g)
    for batch in batches.values():
        ambiguous = (
            len({g["playlist"] for g in batch}) > 1 and len({g["result"] for g in batch}) > 1
        )
        if ambiguous:
            count, previous = 0, ""
            continue
        for g in batch:
            outcome = g["result"]
            count = count + g["count"] if previous == outcome else g["count"]
            previous = outcome
            if outcome in best:
                best[outcome] = max(best[outcome], count)
    data["streaks"] = best
    return data


class Sessions:
    def __init__(self, store: SubscriptionStore) -> None:
        self.store = store

    async def settings(self, guild_id: int, owner_id: int) -> dict[str, Any]:
        return await self.store._run(lambda c: preferences(c, guild_id, owner_id))

    async def save(
        self,
        guild_id: int,
        owner_id: int,
        *,
        cutoff: int,
        enabled: bool,
        channel_id: int | None = None,
        minimum: int = 1,
        include_observations: bool = True,
    ) -> None:
        if type(cutoff) is not int or not 300 <= cutoff <= 86400:
            raise ValueError("Choose a cutoff between 5 and 1,440 minutes.")
        if (
            type(enabled) is not bool
            or type(minimum) is not int
            or not 1 <= minimum <= 100
            or type(include_observations) is not bool
        ):
            raise ValueError("Choose a minimum between 1 and 100 games and an enabled setting.")
        if owner_id == 0 and enabled and not channel_id:
            raise ValueError("Choose a summary channel.")

        def write(c: sqlite3.Connection) -> None:
            c.execute(
                "INSERT INTO play_session_preferences "
                "(guild_id,owner_id,cutoff,enabled,channel_id,minimum,include_observations) "
                "VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(guild_id,owner_id) DO UPDATE SET cutoff=excluded.cutoff,"
                "enabled=excluded.enabled,channel_id=excluded.channel_id,minimum=excluded.minimum,"
                "include_observations=excluded.include_observations",
                (
                    guild_id,
                    owner_id,
                    cutoff,
                    enabled,
                    channel_id,
                    minimum,
                    include_observations,
                ),
            )
            if owner_id == 0 or guild_id < 0:
                c.execute(
                    "INSERT INTO rocket_league_tracking_config(guild_id,inactivity_seconds) "
                    "VALUES(?,?) ON CONFLICT(guild_id) DO UPDATE SET "
                    "inactivity_seconds=excluded.inactivity_seconds",
                    (guild_id, cutoff),
                )
            if not enabled:
                c.execute(
                    "UPDATE play_sessions SET notified=1 WHERE guild_id=? AND owner_id=?",
                    (guild_id, owner_id),
                )
            c.commit()

        await self.store._run(write)

    async def history(
        self, guild_id: int, player_id: str, owner_id: int, *, since: int = 0, limit: int = 200
    ) -> list[dict[str, Any]]:
        return await self.store._run(
            lambda c: [
                session_data(c, r)
                for r in c.execute(
                    "SELECT * FROM play_sessions WHERE guild_id=? AND player_id=? AND owner_id=? "
                    "AND last_at>=? ORDER BY last_at DESC,id DESC LIMIT ?",
                    (guild_id, player_id, owner_id, since, limit),
                )
            ]
        )

    async def latest_server_sessions(self, guild_id: int, *, now: int) -> list[dict[str, Any]]:
        """Return the current session or most recent completed session per server player."""

        def read(c: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = c.execute(
                "WITH ranked AS ("
                "SELECT ps.*, ROW_NUMBER() OVER (PARTITION BY player_id ORDER BY "
                "CASE WHEN closed=0 AND last_at+cutoff>=? THEN 0 ELSE 1 END, "
                "last_at DESC, id DESC) AS row_number "
                "FROM play_sessions ps WHERE guild_id=? AND owner_id=0) "
                "SELECT "
                "id,guild_id,owner_id,user_id,player_id,player_name,started_at,last_at,cutoff,"
                "closed,notified,enabled,attempts,retry_at "
                "FROM ranked WHERE row_number=1 ORDER BY last_at DESC,id DESC",
                (now, guild_id),
            ).fetchall()
            return [session_data(c, row) for row in rows]

        return await self.store._run(read)

    async def finish(self, now: int) -> list[dict[str, Any]]:
        def write(c: sqlite3.Connection) -> list[dict[str, Any]]:
            c.execute(
                "UPDATE play_sessions SET closed=1 WHERE closed=0 AND last_at+cutoff<?", (now,)
            )
            c.execute("DELETE FROM play_sessions WHERE closed=1 AND last_at<?", (now - 30 * 86400,))
            rows = c.execute(
                "SELECT * FROM play_sessions WHERE closed=1 AND notified=0 "
                "AND enabled=1 AND retry_at<=? ORDER BY id LIMIT 100",
                (now,),
            ).fetchall()
            c.commit()
            return [session_data(c, r) for r in rows]

        return await self.store._run(write)

    async def delivered(self, sid: int, *, retry_at: int = 0) -> None:
        def write(c: sqlite3.Connection) -> None:
            c.execute(
                "UPDATE play_sessions SET notified=?,attempts=attempts+1,retry_at=? WHERE id=?",
                (not retry_at, retry_at, sid),
            )
            c.commit()

        await self.store._run(write)


def summary(
    data: dict[str, Any], names: dict[int, str], *, include_observations: bool = True
) -> str:
    total_change = (
        f"{data['mmr_change']:+.0f} MMR" if data["mmr_change"] is not None else "MMR unavailable"
    )
    lines = [
        f"{data['total']} games · {data['wins']} wins · {data['losses']} losses"
        f" · {data['unknown']} uncertain",
        f"Total MMR change: {total_change}",
        f"Observed <t:{data['started_at']}:f> to <t:{data['last_at']}:t>",
        f"Cutoff: {data['cutoff'] // 60} minutes",
    ]
    for p in data["playlists"]:
        delta = f"{p['change']:+.0f} MMR" if p["change"] is not None else "MMR unavailable"
        lines.append(
            f"{names.get(p['playlist'], str(p['playlist']))}: "
            f"{p['win']}W / {p['loss']}L / {p['gap']}? · {delta}"
        )
    if not include_observations:
        return "\n".join(lines)
    lines.append("Game observations:")
    observations = data["observations"][-20:]
    if len(data["observations"]) > len(observations):
        lines.append(
            f"… {len(data['observations']) - len(observations)} earlier observations omitted"
        )
    for game in observations:
        result = f"{game['wins']}W / {game['losses']}L / {game['unknown']}?"
        mmr = "MMR unavailable"
        if game["change"] is not None:
            endpoints = f"{game['before']:.0f}→{game['after']:.0f}"
            mmr = f"{endpoints} · {game['change']:+.0f} MMR"
            if game["total"] > 1:
                mmr += f" total · {game['per_game_change']:+.1f} avg/game"
        lines.append(
            f"<t:{game['at']}:t> · {names.get(game['playlist'], str(game['playlist']))} · "
            f"{result} · {mmr}"
        )
    return "\n".join(lines)
