from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from discord import app_commands

from slopbot.models import CommandPolicy, ResponseVisibility
from slopbot.visibility import (
    command_channel_is_allowed,
    defer_result_response,
    response_is_ephemeral,
)


async def test_response_visibility_uses_command_default() -> None:
    store = SimpleNamespace(
        get_command_policy=AsyncMock(
            return_value=CommandPolicy("rank", ResponseVisibility.PRIVATE, False, ())
        )
    )

    assert await response_is_ephemeral(
        store=cast(Any, store),
        guild_id=1,
        command_name="rank",
        visibility=None,
    )


async def test_explicit_visibility_overrides_command_default_without_database_read() -> None:
    store = SimpleNamespace(get_command_policy=AsyncMock())

    assert not await response_is_ephemeral(
        store=cast(Any, store),
        guild_id=1,
        command_name="rank",
        visibility=app_commands.Choice(name="Public", value="public"),
    )
    store.get_command_policy.assert_not_awaited()


async def test_thread_inherits_parent_text_channel_allowance() -> None:
    store = SimpleNamespace(command_channel_is_allowed=AsyncMock(return_value=True))
    interaction = SimpleNamespace(
        guild_id=1,
        channel_id=30,
        channel=SimpleNamespace(parent_id=20),
    )

    assert await command_channel_is_allowed(
        store=cast(Any, store),
        interaction=cast(Any, interaction),
        command_name="rank",
    )
    store.command_channel_is_allowed.assert_awaited_once_with(
        guild_id=1,
        command_name="rank",
        channel_ids=[30, 20],
    )


async def test_slow_policy_lookup_defers_privately_before_finishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("slopbot.visibility.RESULT_DEFER_TIMEOUT_SECONDS", 0.001)

    async def slow_allowed(**_kwargs: object) -> bool:
        await asyncio.sleep(0.02)
        return True

    async def slow_policy(**_kwargs: object) -> CommandPolicy:
        await asyncio.sleep(0.02)
        return CommandPolicy("mmrhistory", ResponseVisibility.PUBLIC, False, ())

    store = SimpleNamespace(
        command_channel_is_allowed=AsyncMock(side_effect=slow_allowed),
        get_command_policy=AsyncMock(side_effect=slow_policy),
    )
    response = SimpleNamespace(defer=AsyncMock())
    interaction = SimpleNamespace(
        guild_id=1,
        channel_id=30,
        channel=SimpleNamespace(parent_id=None),
        response=response,
    )

    allowed, ephemeral = await defer_result_response(
        store=cast(Any, store),
        interaction=cast(Any, interaction),
        command_name="mmrhistory",
        visibility=None,
    )

    assert allowed
    assert ephemeral
    response.defer.assert_awaited_once_with(ephemeral=True)
