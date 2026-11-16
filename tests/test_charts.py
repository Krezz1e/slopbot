from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import discord
from PIL import Image

from slopbot.charts import render_mmr_history_chart
from slopbot.models import RocketLeagueAccount
from slopbot.rocketleague import (
    ROCKET_LEAGUE_HISTORY_DAYS,
    PlayerDashboardView,
    RocketLeagueCog,
)


def test_mmr_chart_is_a_png() -> None:
    output = render_mmr_history_chart(
        player_name="Player",
        playlist_name="Ranked Doubles (2v2)",
        points=[
            (1_700_000_000, 1000.0, "Platinum I"),
            (1_700_003_600, 1035.0, "Platinum II"),
        ],
    )

    assert output.read(8) == b"\x89PNG\r\n\x1a\n"


def test_xkcd_chart_supports_light_and_dark_modes() -> None:
    points = [(1_700_000_000, 1000.0, "Platinum I"), (1_700_003_600, 1035.0, "Platinum II")]
    light = render_mmr_history_chart(
        player_name="Player",
        playlist_name="Ranked Doubles (2v2)",
        points=points,
        theme_preset="xkcd",
        theme_mode="light",
    )
    dark = render_mmr_history_chart(
        player_name="Player",
        playlist_name="Ranked Doubles (2v2)",
        points=points,
        theme_preset="xkcd",
        theme_mode="dark",
    )

    assert Image.open(light).getpixel((0, 0)) == (255, 253, 245)
    assert Image.open(dark).getpixel((0, 0)) == (17, 17, 15)


async def test_mmr_payload_uses_callers_linked_web_theme(monkeypatch: Any) -> None:
    output = BytesIO(b"chart")
    render = Mock(return_value=output)
    monkeypatch.setattr("slopbot.rocketleague.render_mmr_history_chart", render)
    identities = SimpleNamespace(
        theme_preferences_for_discord=AsyncMock(
            return_value={"theme_preset": "xkcd", "theme_mode": "dark"}
        )
    )
    store = SimpleNamespace(
        list_rocket_league_mmr_history=AsyncMock(
            return_value=[
                (1_700_000_000, 1000.0, "Platinum I"),
                (1_700_003_600, 1035.0, "Platinum II"),
            ]
        )
    )
    cog = RocketLeagueCog(SimpleNamespace(web=SimpleNamespace(identities=identities)), store, None)

    await cog._mmr_history_payload(
        guild_id=2,
        requester_id=99,
        account=RocketLeagueAccount(2, 1, "Epic|player|0", "Player"),
        playlist=11,
        days=7,
    )

    identities.theme_preferences_for_discord.assert_awaited_once_with("99")
    assert render.call_args.kwargs["theme_preset"] == "xkcd"
    assert render.call_args.kwargs["theme_mode"] == "dark"


def test_history_commands_use_thirty_day_limit() -> None:
    history = {parameter.name: parameter for parameter in RocketLeagueCog.mmr_history.parameters}
    movers = {parameter.name: parameter for parameter in RocketLeagueCog.movers.parameters}
    matches = {parameter.name: parameter for parameter in RocketLeagueCog.match_history.parameters}

    assert ROCKET_LEAGUE_HISTORY_DAYS == 30
    assert set(history) == {"account", "gamemode", "days", "visibility"}
    assert history["days"].max_value == 30
    assert history["account"].autocomplete is not None
    assert set(movers) == {"gamemode", "days", "size", "visibility"}
    assert movers["days"].max_value == 30
    assert movers["size"].max_value == 10
    assert matches["days"].max_value == 30


def test_player_dashboard_is_integrated_as_one_selector() -> None:
    view = PlayerDashboardView(
        owner_id=1,
        guild_id=2,
        cog=cast(Any, object()),
        account=RocketLeagueAccount(2, 1, "Epic|player|0", "Player"),
        overview=discord.Embed(title="Overview"),
        all_time_streaks=discord.Embed(title="All-time streaks"),
    )

    assert len(view.children) == 1
    selector = cast(discord.ui.Select[Any], view.children[0])
    assert [option.value for option in selector.options] == [
        "overview",
        "10",
        "11",
        "13",
        "streaks",
    ]
