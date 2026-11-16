from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

from slopbot.models import Subscription
from slopbot.ui import ThresholdPicker, VoiceChannelPicker, accessible_subscriptions


def test_threshold_picker_keeps_selected_value_as_default() -> None:
    editor = SimpleNamespace(threshold=3)
    picker = ThresholdPicker(cast(Any, editor))

    picker.set_selected_threshold(2)

    defaults = [int(option.value) for option in picker.options if option.default]
    assert editor.threshold == 2
    assert defaults == [2]


def test_voice_channel_picker_keeps_selected_channel_as_default() -> None:
    editor = SimpleNamespace(channel_id=None)
    picker = VoiceChannelPicker(cast(Any, editor))

    picker.set_selected_channel(123456)

    assert editor.channel_id == 123456
    assert len(picker.default_values) == 1
    assert picker.default_values[0].id == 123456


async def test_manage_alerts_removes_channels_member_cannot_view(monkeypatch: Any) -> None:
    class FakeVoiceChannel:
        def __init__(self, *, visible: bool) -> None:
            self.visible = visible

        def permissions_for(self, _member: Any) -> Any:
            return SimpleNamespace(view_channel=self.visible)

    monkeypatch.setattr("slopbot.ui.discord.VoiceChannel", FakeVoiceChannel)
    visible = Subscription(1, 10, 20, 30, 3, True, False)
    hidden = Subscription(2, 10, 20, 31, 3, True, False)
    channels = {30: FakeVoiceChannel(visible=True), 31: FakeVoiceChannel(visible=False)}
    guild = SimpleNamespace(id=10, get_channel=lambda channel_id: channels.get(channel_id))
    member = SimpleNamespace(id=20)
    store = SimpleNamespace(
        list_for_user=AsyncMock(return_value=[visible, hidden]),
        delete_for_user=AsyncMock(return_value=True),
    )

    result = await accessible_subscriptions(
        guild=cast(Any, guild), member=cast(Any, member), store=cast(Any, store)
    )

    assert result == [visible]
    store.delete_for_user.assert_awaited_once_with(
        subscription_id=2,
        guild_id=10,
        user_id=20,
    )
