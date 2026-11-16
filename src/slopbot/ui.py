from __future__ import annotations

from collections.abc import Sequence

import discord

from slopbot.database import SubscriptionLimitError, SubscriptionStore
from slopbot.models import (
    MAX_SUBSCRIPTIONS_PER_USER,
    MAX_THRESHOLD,
    MIN_THRESHOLD,
    Subscription,
)


class EmbedPagesView(discord.ui.View):
    """Browse a result in place instead of posting a burst of follow-up messages."""

    def __init__(self, *, owner_id: int, pages: list[discord.Embed]) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.pages = pages
        self.index = 0
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.previous.disabled = self.index == 0
        self.next.disabled = self.index == len(self.pages) - 1
        self.page.label = f"Page {self.index + 1} of {len(self.pages)}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "Only the member who opened this result can change its page. Run /today for your own.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(
        self, interaction: discord.Interaction, _button: discord.ui.Button[EmbedPagesView]
    ) -> None:
        self.index = max(0, self.index - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="Page 1", style=discord.ButtonStyle.secondary, disabled=True)
    async def page(
        self, interaction: discord.Interaction, _button: discord.ui.Button[EmbedPagesView]
    ) -> None:
        pass

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(
        self, interaction: discord.Interaction, _button: discord.ui.Button[EmbedPagesView]
    ) -> None:
        self.index = min(len(self.pages) - 1, self.index + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)


def home_embed() -> discord.Embed:
    return discord.Embed(
        title="Voice channel alerts",
        description=(
            "Get a private DM when a voice channel reaches your chosen number of people. "
            "Each alert belongs only to you."
        ),
        colour=discord.Colour.blurple(),
    )


def _channel_name(guild: discord.Guild, channel_id: int) -> str:
    channel = guild.get_channel(channel_id)
    return channel.name if channel is not None else "Deleted channel"


def member_can_view_channel(
    member: discord.Member, channel: discord.abc.GuildChannel | discord.Thread
) -> bool:
    return channel.permissions_for(member).view_channel


def member_can_view_voice_channel(member: discord.Member, channel: object) -> bool:
    return isinstance(channel, discord.VoiceChannel | discord.StageChannel) and (
        member_can_view_channel(member, channel)
    )


async def accessible_subscriptions(
    *, guild: discord.Guild, member: discord.Member, store: SubscriptionStore
) -> list[Subscription]:
    """Return visible alerts and remove stale ones without exposing private channel details."""
    subscriptions = await store.list_for_user(guild_id=guild.id, user_id=member.id)
    accessible: list[Subscription] = []
    for subscription in subscriptions:
        channel = guild.get_channel(subscription.channel_id)
        if member_can_view_voice_channel(member, channel):
            accessible.append(subscription)
            continue
        await store.delete_for_user(
            subscription_id=subscription.id,
            guild_id=guild.id,
            user_id=member.id,
        )
    return accessible


def _notification_summary(subscription: Subscription) -> str:
    if subscription.notify_on_reach and subscription.notify_on_drop:
        return "joins and departures"
    if subscription.notify_on_reach:
        return "threshold reached"
    return "drops below threshold"


class OwnedView(discord.ui.View):
    def __init__(self, *, owner_id: int, timeout: float = 300) -> None:
        super().__init__(timeout=timeout)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "This alert panel belongs to someone else. Run `/alerts` for your own panel.",
            ephemeral=True,
        )
        return False


class AlertsHomeView(OwnedView):
    def __init__(self, *, owner_id: int, guild: discord.Guild, store: SubscriptionStore) -> None:
        super().__init__(owner_id=owner_id)
        self.guild = guild
        self.store = store

    @discord.ui.button(label="Create alert", style=discord.ButtonStyle.primary, emoji="🔔")
    async def create_alert(
        self, interaction: discord.Interaction, _button: discord.ui.Button[OwnedView]
    ) -> None:
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this inside a server.", ephemeral=True)
            return
        subscriptions = await accessible_subscriptions(
            guild=self.guild, member=interaction.user, store=self.store
        )
        if len(subscriptions) >= MAX_SUBSCRIPTIONS_PER_USER:
            await interaction.response.send_message(
                f"You can have up to {MAX_SUBSCRIPTIONS_PER_USER} alerts in one server. "
                "Delete one before creating another.",
                ephemeral=True,
            )
            return
        view = AlertEditorView(owner_id=self.owner_id, guild=self.guild, store=self.store)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    @discord.ui.button(label="Manage alerts", style=discord.ButtonStyle.secondary, emoji="⚙️")
    async def manage_alerts(
        self, interaction: discord.Interaction, _button: discord.ui.Button[OwnedView]
    ) -> None:
        await show_manage_view(
            interaction=interaction,
            guild=self.guild,
            store=self.store,
            owner_id=self.owner_id,
        )


class VoiceChannelPicker(discord.ui.ChannelSelect[discord.ui.View]):
    def __init__(self, editor: AlertEditorView) -> None:
        super().__init__(
            placeholder="Choose a voice channel",
            channel_types=[discord.ChannelType.voice, discord.ChannelType.stage_voice],
            min_values=1,
            max_values=1,
            row=0,
        )
        self.editor = editor

    def set_selected_channel(self, channel_id: int) -> None:
        self.editor.channel_id = channel_id
        self.default_values = [discord.Object(id=channel_id)]

    async def callback(self, interaction: discord.Interaction) -> None:
        channel_id = self.values[0].id
        channel = self.editor.guild.get_channel(channel_id)
        if not isinstance(interaction.user, discord.Member) or not member_can_view_voice_channel(
            interaction.user, channel
        ):
            await interaction.response.send_message(
                "You cannot create an alert for that channel.", ephemeral=True
            )
            return
        bot_member = self.editor.guild.me
        if bot_member is None or not member_can_view_voice_channel(bot_member, channel):
            await interaction.response.send_message(
                "Slopbot cannot view that channel. Ask an admin to grant its role View Channel.",
                ephemeral=True,
            )
            return
        self.set_selected_channel(channel_id)
        await interaction.response.edit_message(embed=self.editor.embed(), view=self.editor)


class ThresholdPicker(discord.ui.Select[discord.ui.View]):
    def __init__(self, editor: AlertEditorView) -> None:
        options = [
            discord.SelectOption(
                label=f"{threshold} or more people",
                value=str(threshold),
                default=threshold == editor.threshold,
            )
            for threshold in range(MIN_THRESHOLD, MAX_THRESHOLD + 1)
        ]
        super().__init__(
            placeholder="Choose the minimum number of people",
            options=options,
            min_values=1,
            max_values=1,
            row=1,
        )
        self.editor = editor

    def set_selected_threshold(self, threshold: int) -> None:
        self.editor.threshold = threshold
        for option in self.options:
            option.default = option.value == str(threshold)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.set_selected_threshold(int(self.values[0]))
        await interaction.response.edit_message(embed=self.editor.embed(), view=self.editor)


class ToggleButton(discord.ui.Button[discord.ui.View]):
    def __init__(self, editor: AlertEditorView, *, notification_type: str, row: int) -> None:
        self.editor = editor
        self.notification_type = notification_type
        super().__init__(row=row)
        self.refresh()

    def refresh(self) -> None:
        if self.notification_type == "reach":
            enabled = self.editor.notify_on_reach
            prefix = "Threshold reached"
        else:
            enabled = self.editor.notify_on_drop
            prefix = "People leave"
        self.label = f"{prefix}: {'On' if enabled else 'Off'}"
        self.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.notification_type == "reach":
            self.editor.notify_on_reach = not self.editor.notify_on_reach
        else:
            self.editor.notify_on_drop = not self.editor.notify_on_drop
        self.editor.refresh_toggles()
        await interaction.response.edit_message(embed=self.editor.embed(), view=self.editor)


class SaveAlertButton(discord.ui.Button[discord.ui.View]):
    def __init__(self, editor: AlertEditorView) -> None:
        super().__init__(label="Save alert", style=discord.ButtonStyle.primary, row=3)
        self.editor = editor

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.editor.channel_id is None:
            await interaction.response.send_message("Choose a voice channel first.", ephemeral=True)
            return
        if not self.editor.notify_on_reach and not self.editor.notify_on_drop:
            await interaction.response.send_message(
                "Turn on at least one notification type before saving.",
                ephemeral=True,
            )
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use this inside a server.", ephemeral=True)
            return
        channel = self.editor.guild.get_channel(self.editor.channel_id)
        if not member_can_view_voice_channel(interaction.user, channel):
            await interaction.response.send_message(
                "You cannot create an alert for that channel. Choose a voice channel you can view.",
                ephemeral=True,
            )
            return
        bot_member = self.editor.guild.me
        if bot_member is None or not member_can_view_voice_channel(bot_member, channel):
            await interaction.response.send_message(
                "Slopbot cannot view that channel. Ask an admin to grant its role View Channel.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        try:
            subscription = await self.editor.store.save(
                guild_id=self.editor.guild.id,
                user_id=self.editor.owner_id,
                channel_id=self.editor.channel_id,
                threshold=self.editor.threshold,
                notify_on_reach=self.editor.notify_on_reach,
                notify_on_drop=self.editor.notify_on_drop,
            )
        except SubscriptionLimitError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        channel_mention = channel.mention if channel is not None else "the selected channel"
        embed = discord.Embed(
            title="Alert saved",
            description=(
                f"I'll DM you when {channel_mention} reaches **{subscription.threshold} people**. "
                f"Notifications: **{_notification_summary(subscription)}**."
            ),
            colour=discord.Colour.green(),
        )
        view = AlertsHomeView(
            owner_id=self.editor.owner_id,
            guild=self.editor.guild,
            store=self.editor.store,
        )
        await interaction.edit_original_response(embed=embed, view=view)


class EditorBackButton(discord.ui.Button[discord.ui.View]):
    def __init__(self, editor: AlertEditorView) -> None:
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary, row=3)
        self.editor = editor

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.editor.existing is None:
            view = AlertsHomeView(
                owner_id=self.editor.owner_id,
                guild=self.editor.guild,
                store=self.editor.store,
            )
            await interaction.response.edit_message(embed=home_embed(), view=view)
            return
        await show_manage_view(
            interaction=interaction,
            guild=self.editor.guild,
            store=self.editor.store,
            owner_id=self.editor.owner_id,
        )


class AlertEditorView(OwnedView):
    def __init__(
        self,
        *,
        owner_id: int,
        guild: discord.Guild,
        store: SubscriptionStore,
        existing: Subscription | None = None,
    ) -> None:
        super().__init__(owner_id=owner_id)
        self.guild = guild
        self.store = store
        self.existing = existing
        self.channel_id = existing.channel_id if existing is not None else None
        self.threshold = existing.threshold if existing is not None else 3
        self.notify_on_reach = existing.notify_on_reach if existing is not None else True
        self.notify_on_drop = existing.notify_on_drop if existing is not None else False

        if existing is None:
            self.add_item(VoiceChannelPicker(self))
        self.add_item(ThresholdPicker(self))
        self.reach_toggle = ToggleButton(self, notification_type="reach", row=2)
        self.drop_toggle = ToggleButton(self, notification_type="drop", row=2)
        self.add_item(self.reach_toggle)
        self.add_item(self.drop_toggle)
        self.add_item(SaveAlertButton(self))
        self.add_item(EditorBackButton(self))

    def refresh_toggles(self) -> None:
        self.reach_toggle.refresh()
        self.drop_toggle.refresh()

    def embed(self) -> discord.Embed:
        channel_text = (
            f"**{_channel_name(self.guild, self.channel_id)}**"
            if self.channel_id is not None
            else "*Choose a channel below*"
        )
        reach_text = "On" if self.notify_on_reach else "Off"
        drop_text = "On" if self.notify_on_drop else "Off"
        title = "Edit voice alert" if self.existing is not None else "Create a voice alert"
        embed = discord.Embed(
            title=title,
            description=(
                f"**Channel:** {channel_text}\n"
                f"**Minimum:** {self.threshold} people\n"
                f"**When threshold is reached:** {reach_text}\n"
                f"**When activity drops below it:** {drop_text}"
            ),
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(text="Alerts fire only when the count crosses your threshold.")
        return embed


class SubscriptionPicker(discord.ui.Select[discord.ui.View]):
    def __init__(
        self,
        manage_view: ManageAlertsView,
        subscriptions: Sequence[Subscription],
    ) -> None:
        options = []
        for subscription in subscriptions:
            options.append(
                discord.SelectOption(
                    label=_channel_name(manage_view.guild, subscription.channel_id)[:100],
                    description=(
                        f"{subscription.threshold}+ people • {_notification_summary(subscription)}"
                    )[:100],
                    value=str(subscription.id),
                    default=subscription.id == manage_view.selected_id,
                )
            )
        super().__init__(
            placeholder="Choose an alert",
            options=options,
            min_values=1,
            max_values=1,
            row=0,
        )
        self.manage_view = manage_view

    async def callback(self, interaction: discord.Interaction) -> None:
        self.manage_view.selected_id = int(self.values[0])
        self.manage_view.replace_picker()
        await interaction.response.edit_message(
            embed=self.manage_view.embed(), view=self.manage_view
        )


class ManageAlertsView(OwnedView):
    def __init__(
        self,
        *,
        owner_id: int,
        guild: discord.Guild,
        store: SubscriptionStore,
        subscriptions: Sequence[Subscription],
    ) -> None:
        super().__init__(owner_id=owner_id)
        if not subscriptions:
            raise ValueError("ManageAlertsView requires at least one subscription")
        self.guild = guild
        self.store = store
        self.subscriptions = list(subscriptions)
        self.selected_id = self.subscriptions[0].id
        self.add_item(SubscriptionPicker(self, self.subscriptions))

    @property
    def selected(self) -> Subscription:
        return next(item for item in self.subscriptions if item.id == self.selected_id)

    def replace_picker(self) -> None:
        for child in list(self.children):
            if isinstance(child, SubscriptionPicker):
                self.remove_item(child)
                break
        self.add_item(SubscriptionPicker(self, self.subscriptions))

    def embed(self) -> discord.Embed:
        subscription = self.selected
        channel = self.guild.get_channel(subscription.channel_id)
        channel_text = channel.mention if channel is not None else "Deleted channel"
        return discord.Embed(
            title="Manage voice alerts",
            description=(
                f"**Channel:** {channel_text}\n"
                f"**Minimum:** {subscription.threshold} people\n"
                f"**Notifications:** {_notification_summary(subscription)}\n\n"
                f"You have **{len(self.subscriptions)}** alert"
                f"{'s' if len(self.subscriptions) != 1 else ''} in this server."
            ),
            colour=discord.Colour.blurple(),
        )

    @discord.ui.button(label="Edit selected", style=discord.ButtonStyle.primary, row=1)
    async def edit_selected(
        self, interaction: discord.Interaction, _button: discord.ui.Button[OwnedView]
    ) -> None:
        editor = AlertEditorView(
            owner_id=self.owner_id,
            guild=self.guild,
            store=self.store,
            existing=self.selected,
        )
        await interaction.response.edit_message(embed=editor.embed(), view=editor)

    @discord.ui.button(label="Delete selected", style=discord.ButtonStyle.danger, row=1)
    async def delete_selected(
        self, interaction: discord.Interaction, _button: discord.ui.Button[OwnedView]
    ) -> None:
        confirmation = DeleteConfirmationView(
            owner_id=self.owner_id,
            guild=self.guild,
            store=self.store,
            subscription=self.selected,
        )
        await interaction.response.edit_message(embed=confirmation.embed(), view=confirmation)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(
        self, interaction: discord.Interaction, _button: discord.ui.Button[OwnedView]
    ) -> None:
        view = AlertsHomeView(owner_id=self.owner_id, guild=self.guild, store=self.store)
        await interaction.response.edit_message(embed=home_embed(), view=view)


class DeleteConfirmationView(OwnedView):
    def __init__(
        self,
        *,
        owner_id: int,
        guild: discord.Guild,
        store: SubscriptionStore,
        subscription: Subscription,
    ) -> None:
        super().__init__(owner_id=owner_id)
        self.guild = guild
        self.store = store
        self.subscription = subscription

    def embed(self) -> discord.Embed:
        return discord.Embed(
            title="Delete this alert?",
            description=(
                f"You'll stop receiving alerts for "
                f"**{_channel_name(self.guild, self.subscription.channel_id)}**."
            ),
            colour=discord.Colour.red(),
        )

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def confirm(
        self, interaction: discord.Interaction, _button: discord.ui.Button[OwnedView]
    ) -> None:
        await interaction.response.defer()
        await self.store.delete_for_user(
            subscription_id=self.subscription.id,
            guild_id=self.guild.id,
            user_id=self.owner_id,
        )
        await show_manage_view(
            interaction=interaction,
            guild=self.guild,
            store=self.store,
            owner_id=self.owner_id,
            deleted=True,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, _button: discord.ui.Button[OwnedView]
    ) -> None:
        await show_manage_view(
            interaction=interaction,
            guild=self.guild,
            store=self.store,
            owner_id=self.owner_id,
        )


async def show_manage_view(
    *,
    interaction: discord.Interaction,
    guild: discord.Guild,
    store: SubscriptionStore,
    owner_id: int,
    deleted: bool = False,
) -> None:
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Use this inside a server.", ephemeral=True)
        return
    if not interaction.response.is_done():
        await interaction.response.defer()
    subscriptions = await accessible_subscriptions(
        guild=guild,
        member=interaction.user,
        store=store,
    )
    if not subscriptions:
        embed = discord.Embed(
            title="No voice alerts",
            description=(
                "Your alert was deleted. Create another whenever you like."
                if deleted
                else "You haven't created an alert in this server yet."
            ),
            colour=discord.Colour.blurple(),
        )
        view: discord.ui.View = AlertsHomeView(owner_id=owner_id, guild=guild, store=store)
    else:
        manage_view = ManageAlertsView(
            owner_id=owner_id,
            guild=guild,
            store=store,
            subscriptions=subscriptions,
        )
        embed = manage_view.embed()
        view = manage_view
    await interaction.edit_original_response(embed=embed, view=view)
