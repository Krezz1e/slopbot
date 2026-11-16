from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MIN_THRESHOLD = 2
MAX_THRESHOLD = 25
MAX_SUBSCRIPTIONS_PER_USER = 25
MAX_ITEM_SHOP_WATCHES_PER_USER = 25
DEFAULT_NOTIFICATION_DOWNTIME_START_MINUTE = 9 * 60
DEFAULT_NOTIFICATION_DOWNTIME_END_MINUTE = 17 * 60
DEFAULT_NOTIFICATION_DOWNTIME_TIMEZONE = "America/New_York"
MIN_TOURNAMENT_LEAD_MINUTES = 5
MAX_TOURNAMENT_LEAD_MINUTES = 120
TOURNAMENT_TEAM_SIZES = (1, 2, 3)
TOURNAMENT_GAME_MODES = (
    "soccar",
    "rumble",
    "hoops",
    "dropshot",
    "snow_day",
    "heatseeker",
    "pentathlon",
    "other",
)


class Transition(Enum):
    REACHED = "reached"
    DROPPED = "dropped"


class DeliveryMethod(str, Enum):
    DISCORD = "discord"
    TELEGRAM = "telegram"
    PWA = "pwa"
    BOTH = "both"
    DISCORD_PWA = "discord_pwa"
    TELEGRAM_PWA = "telegram_pwa"
    ALL = "all"


@dataclass(frozen=True, slots=True)
class NotificationDowntime:
    enabled: bool = False
    start_minute: int = DEFAULT_NOTIFICATION_DOWNTIME_START_MINUTE
    end_minute: int = DEFAULT_NOTIFICATION_DOWNTIME_END_MINUTE
    timezone: str = DEFAULT_NOTIFICATION_DOWNTIME_TIMEZONE

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("downtime enabled must be true or false")
        for label, value in (("start", self.start_minute), ("end", self.end_minute)):
            if type(value) is not int or not 0 <= value < 24 * 60:
                raise ValueError(f"downtime {label} must be a minute of the day")
        if self.start_minute == self.end_minute:
            raise ValueError("downtime start and end must be different")
        if not isinstance(self.timezone, str) or len(self.timezone) > 100:
            raise ValueError("choose an IANA timezone, such as America/New_York")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown timezone; use an IANA name such as America/New_York") from exc

    def active_at(self, timestamp: float | None = None) -> bool:
        if not self.enabled:
            return False
        local = (
            datetime.now(ZoneInfo(self.timezone))
            if timestamp is None
            else datetime.fromtimestamp(timestamp, ZoneInfo(self.timezone))
        )
        minute = local.hour * 60 + local.minute
        if self.start_minute < self.end_minute:
            return self.start_minute <= minute < self.end_minute
        return minute >= self.start_minute or minute < self.end_minute


def delivery_channels(delivery: DeliveryMethod) -> frozenset[str]:
    return {
        DeliveryMethod.DISCORD: frozenset({"discord"}),
        DeliveryMethod.TELEGRAM: frozenset({"telegram"}),
        DeliveryMethod.PWA: frozenset({"pwa"}),
        DeliveryMethod.BOTH: frozenset({"discord", "telegram"}),
        DeliveryMethod.DISCORD_PWA: frozenset({"discord", "pwa"}),
        DeliveryMethod.TELEGRAM_PWA: frozenset({"telegram", "pwa"}),
        DeliveryMethod.ALL: frozenset({"discord", "telegram", "pwa"}),
    }[delivery]


class KeywordNotificationMode(str, Enum):
    ALWAYS = "always"
    OFFLINE_ONLY = "offline_only"


class ResponseVisibility(str, Enum):
    PUBLIC = "public"
    PRIVATE = "private"


@dataclass(frozen=True, slots=True)
class CommandPolicy:
    command_name: str
    default_visibility: ResponseVisibility
    channel_restricted: bool
    channel_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Subscription:
    id: int
    guild_id: int
    user_id: int
    channel_id: int
    threshold: int
    notify_on_reach: bool
    notify_on_drop: bool

    def __post_init__(self) -> None:
        validate_preferences(
            threshold=self.threshold,
            notify_on_reach=self.notify_on_reach,
            notify_on_drop=self.notify_on_drop,
        )


@dataclass(frozen=True, slots=True)
class KeywordProfile:
    enabled: bool
    notification_mode: KeywordNotificationMode
    keywords: tuple[str, ...]
    channel_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class KeywordWatcher:
    user_id: int
    notification_mode: KeywordNotificationMode
    keywords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GetOnDelivery:
    request_id: int
    guild_id: int
    recipient_id: int
    sender_name: str
    message: str
    attempt_count: int = 0


@dataclass(frozen=True, slots=True)
class RocketLeagueAccount:
    guild_id: int
    discord_user_id: int
    player_id: str
    player_name: str
    unlinked: bool = False


@dataclass(frozen=True, slots=True)
class RocketLeagueTrackingConfig:
    guild_id: int
    streak_channel_id: int | None
    streak_threshold: int
    inactivity_seconds: int
    leaderboard_channel_id: int | None
    leaderboard_playlists: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class RocketLeagueMatchEvent:
    result: str
    playlist: int
    match_count: int
    observed_at: int
    mmr_before: float | None
    mmr_after: float | None


@dataclass(frozen=True, slots=True)
class RocketLeagueEvent:
    event_type: str
    discord_user_id: int
    player_id: str
    player_name: str
    playlist: int
    occurred_at: int
    result: str | None = None
    count: int = 1
    mmr_before: float | None = None
    mmr_after: float | None = None
    rank_before: str | None = None
    rank_after: str | None = None
    position_before: int | None = None
    position_after: int | None = None
    scope: str | None = None
    total_count: int | None = None
    id: int | None = None
    notification_text: str | None = None


@dataclass(frozen=True, slots=True)
class CompletedRocketLeagueStreak:
    guild_id: int
    discord_user_id: int
    player_name: str
    streak_class: str
    result: str
    count: int
    ended_by: str
    total_count: int | None = None
    continued: bool = False


@dataclass(frozen=True, slots=True)
class CurrentRocketLeagueStreak:
    guild_id: int
    discord_user_id: int
    streak_class: str
    result: str
    count: int


@dataclass(frozen=True, slots=True)
class RocketLeagueRecord:
    id: int
    guild_id: int
    discord_user_id: int
    player_id: str
    player_name: str
    streak_class: str
    result: str
    count: int
    observed_at: int


@dataclass(frozen=True, slots=True)
class RocketLeaguePeakMMR:
    guild_id: int
    discord_user_id: int
    player_id: str
    player_name: str
    playlist: int
    mmr: float
    rank: str
    observed_at: int


@dataclass(frozen=True, slots=True)
class ItemShopPrice:
    currency_id: int
    amount: int


@dataclass(frozen=True, slots=True)
class ItemShopItem:
    key: str
    shop_id: int
    shop_name: str
    shop_type: str
    shop_item_id: int
    title: str
    description: str
    image_url: str
    starts_at: int
    ends_at: int | None
    product_ids: tuple[int, ...]
    attributes: tuple[tuple[str, str], ...]
    prices: tuple[ItemShopPrice, ...]

    @property
    def searchable_text(self) -> str:
        parts = [self.title, self.description, self.shop_name, self.shop_type]
        parts.extend(f"{key} {value}" for key, value in self.attributes)
        parts.extend(str(product_id) for product_id in self.product_ids)
        return " ".join(parts).casefold()


@dataclass(frozen=True, slots=True)
class ItemShopWatch:
    id: int
    guild_id: int
    user_id: int
    query: str
    max_price: int | None


@dataclass(frozen=True, slots=True)
class ItemShopChange:
    id: int
    change_type: str
    item: ItemShopItem
    observed_at: int


@dataclass(frozen=True, slots=True)
class ItemShopDelivery:
    change: ItemShopChange
    guild_id: int
    user_id: int
    attempt_count: int


@dataclass(frozen=True, slots=True)
class TournamentUserAlert:
    guild_id: int
    user_id: int
    region: str
    lead_minutes: int
    team_sizes: tuple[int, ...]
    game_modes: tuple[str, ...]

    def __post_init__(self) -> None:
        validate_tournament_alert(
            region=self.region,
            lead_minutes=self.lead_minutes,
            team_sizes=self.team_sizes,
            game_modes=self.game_modes,
        )


@dataclass(frozen=True, slots=True)
class TournamentChannelAlert:
    guild_id: int
    channel_id: int
    configured_by_user_id: int
    region: str
    lead_minutes: int
    team_sizes: tuple[int, ...]
    game_modes: tuple[str, ...]
    opt_in_lead_minutes: int = 15

    def __post_init__(self) -> None:
        validate_tournament_alert(
            region=self.region,
            lead_minutes=self.lead_minutes,
            team_sizes=self.team_sizes,
            game_modes=self.game_modes,
        )
        if (
            not MIN_TOURNAMENT_LEAD_MINUTES
            <= self.opt_in_lead_minutes
            <= MAX_TOURNAMENT_LEAD_MINUTES
        ):
            raise ValueError(
                "opt-in lead time must be between "
                f"{MIN_TOURNAMENT_LEAD_MINUTES} and {MAX_TOURNAMENT_LEAD_MINUTES}"
            )


@dataclass(frozen=True, slots=True)
class TournamentOptInDelivery:
    token: str
    guild_id: int
    user_id: int
    tournament_id: int
    tournament_title: str
    start_time: int
    region: str
    lead_minutes: int
    attempt_count: int


def validate_threshold(threshold: int) -> None:
    if not MIN_THRESHOLD <= threshold <= MAX_THRESHOLD:
        raise ValueError(f"threshold must be between {MIN_THRESHOLD} and {MAX_THRESHOLD}")


def validate_preferences(*, threshold: int, notify_on_reach: bool, notify_on_drop: bool) -> None:
    validate_threshold(threshold)
    if not notify_on_reach and not notify_on_drop:
        raise ValueError("at least one notification type must be enabled")


def validate_tournament_alert(
    *,
    region: str,
    lead_minutes: int,
    team_sizes: tuple[int, ...],
    game_modes: tuple[str, ...],
) -> None:
    if region != "auto" and (not region.isascii() or not region.isalpha() or len(region) > 8):
        raise ValueError("region must be 'auto' or a short alphabetic Rocket League region code")
    if not MIN_TOURNAMENT_LEAD_MINUTES <= lead_minutes <= MAX_TOURNAMENT_LEAD_MINUTES:
        raise ValueError(
            "tournament lead time must be between "
            f"{MIN_TOURNAMENT_LEAD_MINUTES} and {MAX_TOURNAMENT_LEAD_MINUTES} minutes"
        )
    if not team_sizes or len(set(team_sizes)) != len(team_sizes):
        raise ValueError("at least one unique tournament team size must be selected")
    if any(team_size not in TOURNAMENT_TEAM_SIZES for team_size in team_sizes):
        raise ValueError("tournament team sizes must be 1v1, 2v2, or 3v3")
    if not game_modes or len(set(game_modes)) != len(game_modes):
        raise ValueError("at least one unique tournament game mode must be selected")
    if any(game_mode not in TOURNAMENT_GAME_MODES for game_mode in game_modes):
        raise ValueError("unsupported tournament game mode")


def detect_transition(
    *,
    previous_count: int,
    current_count: int,
    threshold: int,
    notify_on_reach: bool,
    notify_on_drop: bool,
) -> Transition | None:
    """Return a threshold transition, avoiding repeated alerts while on one side."""
    if previous_count < threshold <= current_count and notify_on_reach:
        return Transition.REACHED
    if previous_count >= threshold > current_count and notify_on_drop:
        return Transition.DROPPED
    return None
