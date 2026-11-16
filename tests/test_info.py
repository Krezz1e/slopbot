from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands

from slopbot.bot import AlertsCog
from slopbot.engagement import EngagementCog
from slopbot.info import (
    CAPABILITY_TOPICS,
    INFO_TOPIC_CHOICES,
    InfoCog,
    capability_embed,
    validate_info_command_coverage,
)
from slopbot.itemshop import ItemShopCog
from slopbot.rocketleague import RocketLeagueCog, StatusCog
from slopbot.summary_alerts import SummaryCog
from slopbot.tournaments import TournamentAlertsCog


def _leaf_command_names(command: app_commands.Command | app_commands.Group) -> set[str]:
    if isinstance(command, app_commands.Group):
        return {name for child in command.commands for name in _leaf_command_names(child)}
    return {command.qualified_name}


def test_info_catalog_covers_every_slash_command() -> None:
    cog_types = (
        SummaryCog,
        AlertsCog,
        EngagementCog,
        RocketLeagueCog,
        ItemShopCog,
        TournamentAlertsCog,
        StatusCog,
        InfoCog,
    )
    registered = {
        name
        for cog_type in cog_types
        for command in cog_type.__cog_app_commands__
        for name in _leaf_command_names(command)
    }

    validate_info_command_coverage(registered)


def test_info_command_has_one_required_choice_parameter() -> None:
    parameters = list(InfoCog.info.parameters)

    assert len(parameters) == 1
    assert parameters[0].name == "topic"
    assert parameters[0].required
    assert [(choice.name, choice.value) for choice in parameters[0].choices] == [
        (choice.name, choice.value) for choice in INFO_TOPIC_CHOICES
    ]


def test_overview_embed_summarizes_each_detailed_topic() -> None:
    embed = capability_embed("overview")

    assert embed.title == "Slopbot info — Overview"
    assert [field.name for field in embed.fields] == [
        topic.label for key, topic in CAPABILITY_TOPICS.items() if key != "overview"
    ]


@pytest.mark.parametrize("topic_value", CAPABILITY_TOPICS)
def test_info_embeds_fit_discord_limits(topic_value: str) -> None:
    embed = capability_embed(topic_value)

    assert len(embed) <= 6000
    assert all(len(field.name) <= 256 for field in embed.fields)
    assert all(len(field.value) <= 1024 for field in embed.fields)


def test_detailed_topic_explains_behavior_limits_and_commands() -> None:
    embed = capability_embed("rocket_league")
    rendered = "\n".join([embed.description or "", *(field.value for field in embed.fields)])

    assert "cannot backfill" in rendered
    assert "Ambiguous missed matches" in rendered
    assert "/rank current" in rendered
    assert "/streakleaderboard" in rendered


@pytest.mark.asyncio
async def test_info_response_is_private() -> None:
    interaction = SimpleNamespace(response=SimpleNamespace(send_message=AsyncMock()))

    await InfoCog.info.callback(
        InfoCog(),
        interaction,
        app_commands.Choice(name="Voice-channel alerts", value="voice_alerts"),
    )

    interaction.response.send_message.assert_awaited_once()
    _, kwargs = interaction.response.send_message.await_args
    assert kwargs["ephemeral"] is True
    assert isinstance(kwargs["embed"], discord.Embed)
    assert "/alerts" in kwargs["embed"].fields[0].value


def test_coverage_validator_reports_new_and_removed_commands() -> None:
    with pytest.raises(RuntimeError, match="not documented in /info: futurecommand"):
        validate_info_command_coverage({"futurecommand"})
