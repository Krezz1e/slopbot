"""Conservative near-term Rocket League rank-step estimates.

The game service is authoritative for the current tier/division. Slopbot learns
from anonymous aggregate crossings and observed results, and labels the weaker
time-in-step fallback separately from MMR-based estimates.
"""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import asdict, dataclass
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from slopbot.database import SubscriptionStore
    from slopbot.rocketleague import RocketLeagueSkill

EVIDENCE_DAYS = 30
MIN_PERSONAL_DELTAS = 3
MIN_GLOBAL_DELTAS = 8
MAX_REASONABLE_MMR_DELTA = 40

RANK_NAMES = (
    "Unranked",
    "Bronze I",
    "Bronze II",
    "Bronze III",
    "Silver I",
    "Silver II",
    "Silver III",
    "Gold I",
    "Gold II",
    "Gold III",
    "Platinum I",
    "Platinum II",
    "Platinum III",
    "Diamond I",
    "Diamond II",
    "Diamond III",
    "Champion I",
    "Champion II",
    "Champion III",
    "Grand Champion I",
    "Grand Champion II",
    "Grand Champion III",
    "Supersonic Legend",
)
DIVISIONS = ("I", "II", "III", "IV")


@dataclass(frozen=True, slots=True)
class RankStep:
    direction: Literal["win", "loss"]
    kind: Literal["division", "rank"]
    target: str
    games: int
    target_mmr: int | None
    boundary_samples: int
    delta_samples: int
    delta_source: Literal["personal", "playlist"]
    basis: Literal["boundary", "usual"] = "boundary"


@dataclass(frozen=True, slots=True)
class RankStepEstimates:
    playlist: int
    current_mmr: int
    current_rank: str
    observed_at: int | None
    steps: tuple[RankStep, ...]
    note: str

    def public(self) -> dict[str, Any]:
        return {**asdict(self), "steps": [asdict(step) for step in self.steps]}


def rank_state(rank: str | None) -> tuple[int, int] | None:
    if not rank:
        return None
    normalized = rank.strip()
    for tier, name in enumerate(RANK_NAMES[1:], 1):
        prefix = name + ", Division "
        if normalized.startswith(prefix):
            division = normalized[len(prefix) :].strip()
            if division in DIVISIONS:
                return tier, DIVISIONS.index(division)
    return None


def state_name(state: tuple[int, int]) -> str:
    tier, division = state
    return f"{RANK_NAMES[tier]}, Division {DIVISIONS[division]}"


def adjacent(state: tuple[int, int], offset: int) -> tuple[int, int] | None:
    tier, division = state
    position = (tier - 1) * 4 + division + offset
    highest = (len(RANK_NAMES) - 2) * 4
    if position < 0 or position > highest:
        return None
    next_tier, next_division = divmod(position, 4)
    return next_tier + 1, next_division


def rank_target(state: tuple[int, int], direction: int) -> tuple[int, int] | None:
    tier, _division = state
    target_tier = tier + direction
    if target_tier < 1 or target_tier >= len(RANK_NAMES):
        return None
    # Promotion enters Division I. Demotion enters the preceding rank at Division IV.
    return target_tier, 0 if direction > 0 else 3


def _median(values: list[float]) -> float:
    return float(statistics.median(values))


def _valid_delta(result: str, before: float | None, after: float | None) -> float | None:
    if before is None or after is None:
        return None
    signed = after - before
    if (result == "win" and signed <= 0) or (result == "loss" and signed >= 0):
        return None
    value = abs(signed)
    return value if 0 < value <= MAX_REASONABLE_MMR_DELTA else None


def _boundary_samples(
    transitions: list[tuple[str, str | None, str | None, float | None, float | None]],
    source: tuple[int, int],
    target: tuple[int, int],
    direction: Literal["win", "loss"],
) -> list[float]:
    event_type = "rank_up" if direction == "win" else "rank_down"
    return [
        (before + after) / 2 if before is not None else after
        for kind, before_rank, after_rank, before, after in transitions
        if kind == event_type
        and rank_state(before_rank) == source
        and rank_state(after_rank) == target
        and after is not None
    ]


def _state_position(state: tuple[int, int]) -> int:
    return (state[0] - 1) * 4 + state[1]


BoundaryRow = tuple[int, int, int, int, str, float, float | None, int, int, int]


def _stored_boundary(
    boundaries: list[BoundaryRow],
    source: tuple[int, int],
    target: tuple[int, int],
    direction: Literal["win", "loss"],
) -> tuple[float, int, float, int] | None:
    for row in boundaries:
        from_tier, from_division, to_tier, to_division, kind = row[:5]
        mmr, mean_results, result_count, count, _at = row[5:]
        if (
            (from_tier, from_division) == source
            and (to_tier, to_division) == target
            and kind == direction
        ):
            return mmr, count, mean_results or 0.0, result_count
    return None


def _sample_boundary(
    samples: list[tuple[int, int, float]],
    target: tuple[int, int],
    direction: Literal["win", "loss"],
) -> tuple[float, int] | None:
    values = [mmr for tier, division, mmr in samples if (tier, division) == target]
    if len(values) < 2:
        return None
    # The target-side edge is the most useful approximation when no crossing has
    # yet been recorded. Median the three edge samples to resist one stale outlier.
    values.sort()
    edge = values[:3] if direction == "win" else values[-3:]
    return _median(edge), len(values)


def _projected_boundary(
    boundaries: list[BoundaryRow],
    transitions: list[tuple[str, str | None, str | None, float | None, float | None]],
    samples: list[tuple[int, int, float]],
    source: tuple[int, int],
    target: tuple[int, int],
) -> tuple[float, int] | None:
    known: list[tuple[int, float, int]] = [
        (
            max(
                _state_position((from_tier, from_division)),
                _state_position((to_tier, to_division)),
            ),
            mmr,
            count,
        )
        for (
            from_tier,
            from_division,
            to_tier,
            to_division,
            _kind,
            mmr,
            _mean_results,
            _result_count,
            count,
            _at,
        ) in boundaries
    ]
    for _kind, before_rank, after_rank, before_mmr, after_mmr in transitions:
        before_state, after_state = rank_state(before_rank), rank_state(after_rank)
        if (
            before_state is None
            or after_state is None
            or before_mmr is None
            or after_mmr is None
            or abs(_state_position(before_state) - _state_position(after_state)) != 1
        ):
            continue
        known.append(
            (
                max(_state_position(before_state), _state_position(after_state)),
                (before_mmr + after_mmr) / 2,
                1,
            )
        )
    by_state: dict[int, list[float]] = {}
    for tier, division, mmr in samples:
        by_state.setdefault(_state_position((tier, division)), []).append(mmr)
    for position, values in by_state.items():
        if len(values) < 2:
            continue
        values.sort()
        known.append((position, _median(values[:3]), len(values)))
        known.append((position + 1, _median(values[-3:]), len(values)))
    if len(known) < 2:
        return None
    by_position: dict[int, list[float]] = {}
    for position, mmr, _count in known:
        by_position.setdefault(position, []).append(mmr)
    anchors = sorted((position, _median(values)) for position, values in by_position.items())
    widths = [
        (right_mmr - left_mmr) / (right_position - left_position)
        for (left_position, left_mmr), (right_position, right_mmr) in pairwise(anchors)
        if 0 < right_position - left_position <= 4 and right_mmr > left_mmr
    ]
    if not widths:
        return None
    wanted = max(_state_position(source), _state_position(target))
    position, mmr = min(anchors, key=lambda item: abs(item[0] - wanted))
    if abs(position - wanted) > 4:
        return None
    sample_count = sum(count for _position, _mmr, count in known)
    return mmr + (wanted - position) * _median(widths), sample_count


def _usual_results(
    boundaries: list[BoundaryRow], direction: Literal["win", "loss"]
) -> tuple[float, int] | None:
    values: list[float] = []
    sample_count = 0
    for row in boundaries:
        kind, mean_results, result_count = row[4], row[6], row[7]
        if kind != direction or mean_results is None or result_count <= 0:
            continue
        values.extend([mean_results] * min(result_count, 10))
        sample_count += result_count
    return (_median(values), sample_count) if sample_count >= 2 else None


def calculate_rank_steps(
    *,
    skill: RocketLeagueSkill,
    playlist: int,
    player_id: str,
    evidence: dict[str, Any],
    observed_at: int | None = None,
) -> RankStepEstimates | None:
    del player_id  # Retained in the API to make evidence scoping explicit at call sites.
    current = (skill.tier, skill.division)
    if skill.tier <= 0 or skill.tier >= len(RANK_NAMES) or not 0 <= skill.division <= 3:
        return None
    if 0 < skill.placement_matches_played < 10:
        return None
    transitions = cast(
        list[tuple[str, str | None, str | None, float | None, float | None]],
        evidence["transitions"],
    )
    boundaries = cast(list[BoundaryRow], evidence["boundaries"])
    samples = cast(list[tuple[int, int, float]], evidence["rank_samples"])
    has_personal_history = bool(evidence["has_personal_history"])
    personal = cast(list[tuple[str, float | None, float | None]], evidence["personal_deltas"])
    global_rows = cast(list[tuple[str, float | None, float | None]], evidence["global_deltas"])
    steps: list[RankStep] = []
    for direction, offset in (("win", 1), ("loss", -1)):
        typed_direction = cast(Literal["win", "loss"], direction)
        own_deltas = [
            value
            for result, before, after in personal
            if result == direction and (value := _valid_delta(result, before, after)) is not None
        ]
        all_deltas = [
            value
            for result, before, after in global_rows
            if result == direction and (value := _valid_delta(result, before, after)) is not None
        ]
        own_any_deltas = [
            value
            for result, before, after in personal
            if (value := _valid_delta(result, before, after)) is not None
        ]
        all_any_deltas = [
            value
            for result, before, after in global_rows
            if (value := _valid_delta(result, before, after)) is not None
        ]
        if len(own_deltas) >= MIN_PERSONAL_DELTAS:
            deltas, delta_source = own_deltas, "personal"
        elif len(own_any_deltas) >= MIN_PERSONAL_DELTAS:
            deltas, delta_source = own_any_deltas, "personal"
        elif len(all_deltas) >= MIN_GLOBAL_DELTAS:
            deltas, delta_source = all_deltas, "playlist"
        elif len(all_any_deltas) >= MIN_GLOBAL_DELTAS:
            deltas, delta_source = all_any_deltas, "playlist"
        else:
            deltas, delta_source = [], "playlist"
        opposite = "loss" if direction == "win" else "win"
        usual = _usual_results(boundaries, typed_direction) or _usual_results(
            boundaries, cast(Literal["win", "loss"], opposite)
        )
        targets = (("division", adjacent(current, offset)), ("rank", rank_target(current, offset)))
        for kind, target in targets:
            if target is None:
                continue
            boundary: float | None = None
            boundary_count = 0
            target_usual: tuple[float, int] | None = None
            stored = _stored_boundary(boundaries, current, target, typed_direction)
            crossing_values = _boundary_samples(transitions, current, target, typed_direction)
            if stored is None and not crossing_values:
                stored = _stored_boundary(
                    boundaries,
                    target,
                    current,
                    cast(Literal["win", "loss"], opposite),
                )
            if not crossing_values:
                crossing_values = _boundary_samples(
                    transitions,
                    target,
                    current,
                    cast(Literal["win", "loss"], opposite),
                )
            if stored is not None:
                boundary, boundary_count, mean_results, result_count = stored
                if mean_results > 0 and result_count > 0:
                    target_usual = mean_results, result_count
            elif crossing_values:
                boundary, boundary_count = _median(crossing_values), len(crossing_values)
            if boundary is None and kind == "rank":
                # Whole-rank edges are always learned from Division IV → I (or I → IV).
                edge = (target[0] - 1, 3) if direction == "win" else (target[0] + 1, 0)
                edge_stored = _stored_boundary(boundaries, edge, target, typed_direction)
                edge_values = _boundary_samples(transitions, edge, target, typed_direction)
                if edge_stored is None and not edge_values:
                    edge_stored = _stored_boundary(
                        boundaries,
                        target,
                        edge,
                        cast(Literal["win", "loss"], opposite),
                    )
                if not edge_values:
                    edge_values = _boundary_samples(
                        transitions,
                        target,
                        edge,
                        cast(Literal["win", "loss"], opposite),
                    )
                if edge_stored is not None:
                    boundary, boundary_count, mean_results, result_count = edge_stored
                    if edge == current and mean_results > 0 and result_count > 0:
                        target_usual = mean_results, result_count
                elif edge_values:
                    boundary, boundary_count = _median(edge_values), len(edge_values)
            if boundary is None:
                sampled = _sample_boundary(samples, target, typed_direction)
                if sampled is not None:
                    boundary, boundary_count = sampled
                else:
                    projected = _projected_boundary(
                        boundaries, transitions, samples, current, target
                    )
                    if projected is not None and has_personal_history:
                        boundary, boundary_count = projected
            if boundary is not None and deltas:
                distance = boundary - skill.mmr if direction == "win" else skill.mmr - boundary
                games = max(1, math.ceil(max(0.0, distance) / _median(deltas)))
                target_mmr: int | None = round(boundary)
                samples_used = boundary_count
                basis: Literal["boundary", "usual"] = "boundary"
            elif (usable_results := target_usual or usual) is not None:
                typical_results, usual_samples = usable_results
                state_distance = (
                    1
                    if target_usual is not None
                    else abs(_state_position(target) - _state_position(current))
                )
                games = max(1, round(typical_results * state_distance))
                target_mmr = None
                samples_used = usual_samples
                basis = "usual"
            else:
                continue
            steps.append(
                RankStep(
                    typed_direction,
                    cast(Literal["division", "rank"], kind),
                    state_name(target),
                    games,
                    target_mmr,
                    samples_used,
                    len(deltas),
                    cast(Literal["personal", "playlist"], delta_source),
                    basis,
                )
            )
    return RankStepEstimates(
        playlist=playlist,
        current_mmr=round(skill.mmr),
        current_rank=skill.rank,
        observed_at=observed_at,
        steps=tuple(steps),
        note=(
            "Based on rank crossings, single-game MMR changes, and complete rank-step "
            "histories Slopbot observed during the last 30 days. Estimates can change "
            "after each game. Counts assume consecutive wins or losses, not a predicted timeline."
        ),
    )


async def estimate_rank_steps(
    store: SubscriptionStore,
    *,
    skill: RocketLeagueSkill,
    playlist: int,
    player_id: str,
    observed_at: int | None = None,
    now: int | None = None,
) -> RankStepEstimates | None:
    since = (now if now is not None else int(time.time())) - EVIDENCE_DAYS * 86400

    def read(connection: Any) -> dict[str, Any]:
        transitions = connection.execute(
            "SELECT DISTINCT event_type,rank_before,rank_after,mmr_before,mmr_after "
            "FROM rocket_league_events WHERE playlist=? AND occurred_at>=? "
            "AND event_type IN ('rank_up','rank_down')",
            (playlist, since),
        ).fetchall()
        boundaries = connection.execute(
            "SELECT from_tier,from_division,to_tier,to_division,direction,mean_mmr,"
            "mean_results,result_sample_count,sample_count,last_observed "
            "FROM rocket_league_rank_boundaries "
            "WHERE playlist=? AND last_observed>=?",
            (playlist, since),
        ).fetchall()
        rank_samples = connection.execute(
            "SELECT tier,division,mmr FROM rocket_league_activity_samples "
            "WHERE playlist=? AND observed_at>=? AND tier IS NOT NULL AND division IS NOT NULL",
            (playlist, since),
        ).fetchall()
        history_rank_samples = connection.execute(
            "SELECT rank,mmr FROM rocket_league_mmr_history WHERE playlist=? AND observed_at>=?",
            (playlist, since),
        ).fetchall()
        personal_history = connection.execute(
            "SELECT 1 FROM rocket_league_mmr_history WHERE player_id=? AND playlist=? "
            "AND observed_at>=? LIMIT 1",
            (player_id, playlist, since),
        ).fetchone()
        # Each inferred game in a poll carries the whole poll's MMR delta.
        # Only polls containing exactly one game provide a per-game delta;
        # count uncertain rows too, then deduplicate copies across servers.
        global_deltas = connection.execute(
            "SELECT DISTINCT player_id,observed_at,MIN(result),MIN(mmr_before),MIN(mmr_after) "
            "FROM rocket_league_match_history WHERE playlist=? AND observed_at>=? "
            "GROUP BY guild_id,discord_user_id,player_id,playlist,observed_at "
            "HAVING SUM(match_count)=1 AND MIN(result) IN ('win','loss')",
            (playlist, since),
        ).fetchall()
        return {
            "transitions": [tuple(row) for row in transitions],
            "boundaries": [tuple(row) for row in boundaries],
            "rank_samples": [tuple(row) for row in rank_samples]
            + [
                (state[0], state[1], float(row[1]))
                for row in history_rank_samples
                if (state := rank_state(row[0])) is not None
            ],
            "has_personal_history": personal_history is not None,
            "personal_deltas": [
                (row[2], row[3], row[4]) for row in global_deltas if row[0] == player_id
            ],
            "global_deltas": [(row[2], row[3], row[4]) for row in global_deltas],
        }

    evidence = await store._run(read)
    return calculate_rank_steps(
        skill=skill,
        playlist=playlist,
        player_id=player_id,
        evidence=evidence,
        observed_at=observed_at,
    )


def format_rank_steps(estimates: RankStepEstimates | None) -> str | None:
    if estimates is None or not estimates.steps:
        return None
    labels = {
        ("win", "division"): "division up",
        ("win", "rank"): "rank up",
        ("loss", "division"): "division down",
        ("loss", "rank"): "rank down",
    }

    def result_word(step: RankStep) -> str:
        if step.games == 1:
            return "win" if step.direction == "win" else "loss"
        return "wins" if step.direction == "win" else "losses"

    parts = [
        f"~{step.games} {result_word(step)} to {labels[(step.direction, step.kind)]}"
        for step in estimates.steps
    ]
    return " · ".join(parts)
