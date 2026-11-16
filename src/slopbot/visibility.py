from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from slopbot.models import ResponseVisibility

if TYPE_CHECKING:
    from slopbot.database import SubscriptionStore

RESPONSE_VISIBILITY_CHOICES = [
    app_commands.Choice(name="Public — everyone in the channel", value="public"),
    app_commands.Choice(name="Only me", value="private"),
]
RESULT_DEFER_TIMEOUT_SECONDS = 1.5

SHAREABLE_COMMANDS: dict[str, str] = {
    "today": "/today",
    "rank": "/rank",
    "matchhistory": "/matchhistory",
    "mmrhistory": "/mmrhistory",
    "movers": "/movers",
    "leaderboard": "/leaderboard",
    "streakleaderboard": "/streakleaderboard",
    "peakleaderboard": "/peakleaderboard",
    "playeroverview": "/playeroverview",
    "tournaments": "/tournaments",
    "itemshop_current": "/itemshop current",
    "itemshop_changes": "/itemshop changes",
}

SHAREABLE_COMMAND_CHOICES = [
    app_commands.Choice(name=label, value=command_name)
    for command_name, label in SHAREABLE_COMMANDS.items()
]


async def response_is_ephemeral(
    *,
    store: SubscriptionStore,
    guild_id: int,
    command_name: str,
    visibility: app_commands.Choice[str] | None,
) -> bool:
    if visibility is not None:
        selected = ResponseVisibility(visibility.value)
    else:
        policy = await store.get_command_policy(
            guild_id=guild_id,
            command_name=command_name,
        )
        selected = policy.default_visibility
    return selected is ResponseVisibility.PRIVATE


async def command_channel_is_allowed(
    *,
    store: SubscriptionStore,
    interaction: discord.Interaction,
    command_name: str,
) -> bool:
    if getattr(interaction, "slopbot_web", False) is True:
        # Web results are private; membership and destination permissions are
        # checked by the web transport. Discord posting policies still apply there.
        return True
    channel_id = getattr(interaction, "channel_id", None)
    channel_ids = [channel_id] if isinstance(channel_id, int) else []
    parent_id = getattr(getattr(interaction, "channel", None), "parent_id", None)
    if isinstance(parent_id, int):
        channel_ids.append(parent_id)
    return await store.command_channel_is_allowed(
        guild_id=interaction.guild_id or 0,
        command_name=command_name,
        channel_ids=channel_ids,
    )


async def defer_result_response(
    *,
    store: SubscriptionStore,
    interaction: discord.Interaction,
    command_name: str,
    visibility: app_commands.Choice[str] | None,
) -> tuple[bool, bool]:
    """Acknowledge an interaction before a busy serialized store can expire it."""
    guild_id = interaction.guild_id or 0
    lookup = asyncio.gather(
        command_channel_is_allowed(
            store=store,
            interaction=interaction,
            command_name=command_name,
        ),
        response_is_ephemeral(
            store=store,
            guild_id=guild_id,
            command_name=command_name,
            visibility=visibility,
        ),
    )
    try:
        allowed, ephemeral = await asyncio.wait_for(
            asyncio.shield(lookup),
            timeout=RESULT_DEFER_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        # When the server default is still queued behind another database operation,
        # acknowledge privately. A private fallback cannot expose a response that an
        # administrator intended to keep private.
        ephemeral = (
            visibility is None or ResponseVisibility(visibility.value) is ResponseVisibility.PRIVATE
        )
        await interaction.response.defer(ephemeral=ephemeral)
        allowed, _resolved_ephemeral = await lookup
        return allowed, ephemeral
    # A quick policy denial should remain a private explanation, even on public commands.
    ephemeral = ephemeral or not allowed
    await interaction.response.defer(ephemeral=ephemeral)
    return allowed, ephemeral
