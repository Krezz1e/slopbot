from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from slopbot.models import (
    NotificationDowntime,
    Subscription,
    Transition,
    detect_transition,
    validate_tournament_alert,
)


def local_timestamp(hour: int, minute: int, timezone: str = "America/New_York") -> float:
    return datetime(2026, 1, 15, hour, minute, tzinfo=ZoneInfo(timezone)).timestamp()


def test_notification_downtime_uses_local_start_inclusive_end_exclusive() -> None:
    downtime = NotificationDowntime(True, 9 * 60, 17 * 60, "America/New_York")

    assert not downtime.active_at(local_timestamp(8, 59))
    assert downtime.active_at(local_timestamp(9, 0))
    assert downtime.active_at(local_timestamp(16, 59))
    assert not downtime.active_at(local_timestamp(17, 0))


def test_notification_downtime_supports_overnight_windows() -> None:
    downtime = NotificationDowntime(True, 22 * 60, 7 * 60, "America/New_York")

    assert downtime.active_at(local_timestamp(23, 0))
    assert downtime.active_at(local_timestamp(6, 59))
    assert not downtime.active_at(local_timestamp(12, 0))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start_minute": 60, "end_minute": 60},
        {"timezone": "Missing/Timezone"},
        {"start_minute": -1},
        {"end_minute": 24 * 60},
    ],
)
def test_notification_downtime_rejects_invalid_settings(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        NotificationDowntime(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("previous", "current", "threshold", "reach", "drop", "expected"),
    [
        (1, 2, 2, True, False, Transition.REACHED),
        (2, 3, 2, True, False, None),
        (4, 2, 3, True, False, None),
        (3, 2, 3, False, True, Transition.DROPPED),
        (3, 1, 3, False, True, Transition.DROPPED),
        (2, 1, 3, False, True, None),
        (2, 2, 2, True, True, None),
    ],
)
def test_detect_transition(
    previous: int,
    current: int,
    threshold: int,
    reach: bool,
    drop: bool,
    expected: Transition | None,
) -> None:
    assert (
        detect_transition(
            previous_count=previous,
            current_count=current,
            threshold=threshold,
            notify_on_reach=reach,
            notify_on_drop=drop,
        )
        is expected
    )


def test_subscription_rejects_no_notification_types() -> None:
    with pytest.raises(ValueError, match="at least one"):
        Subscription(
            id=1,
            guild_id=2,
            user_id=3,
            channel_id=4,
            threshold=3,
            notify_on_reach=False,
            notify_on_drop=False,
        )


@pytest.mark.parametrize("threshold", [1, 26])
def test_subscription_rejects_threshold_outside_supported_range(threshold: int) -> None:
    with pytest.raises(ValueError, match="threshold"):
        Subscription(
            id=1,
            guild_id=2,
            user_id=3,
            channel_id=4,
            threshold=threshold,
            notify_on_reach=True,
            notify_on_drop=False,
        )


@pytest.mark.parametrize(
    ("region", "lead_minutes", "team_sizes", "game_modes"),
    [
        ("US-East", 15, (1,), ("soccar",)),
        ("USE", 4, (1,), ("soccar",)),
        ("USE", 121, (1,), ("soccar",)),
        ("USE", 15, (), ("soccar",)),
        ("USE", 15, (4,), ("soccar",)),
        ("USE", 15, (2,), ()),
        ("USE", 15, (2,), ("unknown",)),
    ],
)
def test_tournament_alert_rejects_invalid_settings(
    region: str,
    lead_minutes: int,
    team_sizes: tuple[int, ...],
    game_modes: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError):
        validate_tournament_alert(
            region=region,
            lead_minutes=lead_minutes,
            team_sizes=team_sizes,
            game_modes=game_modes,
        )
