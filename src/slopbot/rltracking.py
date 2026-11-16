from __future__ import annotations

from dataclasses import dataclass

MAX_INFERRED_MATCH_DELTA = 20


@dataclass(frozen=True, slots=True)
class ResultInference:
    unknown_count: int
    known_tail: tuple[str, ...]


def infer_activity_results(
    *,
    previous_matches: int,
    previous_win_streak: int,
    previous_mmr: float,
    current_matches: int,
    current_win_streak: int,
    current_mmr: float,
) -> ResultInference:
    """Infer only results guaranteed by aggregate skill-counter changes.

    Rocket League exposes a match count and current streak, not arbitrary players' match
    histories. Any unprovable prefix is represented as an explicit gap so callers never
    manufacture a streak.
    """
    delta = current_matches - previous_matches
    if delta <= 0:
        return ResultInference(0, ())
    if delta > MAX_INFERRED_MATCH_DELTA:
        return ResultInference(delta, ())

    if current_win_streak != 0:
        result = "win" if current_win_streak > 0 else "loss"
        same_direction = (previous_win_streak > 0) == (current_win_streak > 0)
        if (
            previous_win_streak != 0
            and same_direction
            and abs(current_win_streak) == abs(previous_win_streak) + delta
        ):
            return ResultInference(0, (result,) * delta)
        known_tail_count = min(abs(current_win_streak), delta)
        return ResultInference(delta - known_tail_count, (result,) * known_tail_count)

    if delta == 1:
        if current_mmr > previous_mmr:
            return ResultInference(0, ("win",))
        if current_mmr < previous_mmr:
            return ResultInference(0, ("loss",))
    return ResultInference(delta, ())
