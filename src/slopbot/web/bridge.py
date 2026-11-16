"""Translate the shared Discord command/UI contract into browser forms.

discord.py's interaction transport is the only duck-typed boundary. Feature handlers,
checks, models, and storage remain the same for both interfaces. Pin/contract-test the
few private component attributes used to provide browser input.
"""

from __future__ import annotations

import base64
import secrets
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import discord
from discord import app_commands

from slopbot.info import CAPABILITY_TOPICS

SUPPORTED_PARAMETERS = {"string", "integer", "number", "boolean", "user", "channel"}
SUPPORTED_COMPONENTS = (
    discord.ui.Button,
    discord.ui.Select,
    discord.ui.ChannelSelect,
    discord.ui.UserSelect,
    discord.ui.TextInput,
)


def command_schema(command: Any) -> dict[str, Any]:
    parameters = []
    for p in command.parameters:
        if p.type.name not in SUPPORTED_PARAMETERS:
            raise ValueError(f"Web support missing: {command.qualified_name}.{p.name}")
        # Browser responses are private. Discord posting defaults are managed in Setup.
        if p.name == "visibility":
            continue
        parameters.append(
            {
                "name": p.name,
                "description": p.description,
                "type": p.type.name,
                "required": p.required,
                "min": p.min_value,
                "max": p.max_value,
                "choices": [{"label": c.name, "value": c.value} for c in p.choices],
                "channel_types": [t.value for t in p.channel_types],
                "autocomplete": p.autocomplete,
            }
        )
    category = "Other"
    for topic in CAPABILITY_TOPICS.values():
        if any(c.name == command.qualified_name for s in topic.sections for c in s.commands):
            category = topic.label
            break
    permissions = command.default_permissions
    return {
        "name": command.qualified_name,
        "description": command.description,
        "category": category,
        "admin": bool(permissions and permissions.value),
        "parameters": parameters,
    }


def require_command_permissions(command: Any, member: discord.Member) -> None:
    current = command
    while current is not None:
        permissions = current.default_permissions
        if permissions and not member.guild_permissions.is_superset(permissions):
            raise PermissionError("You no longer have the required server permissions.")
        current = current.parent


def visible_channels(guild: Any, member: Any) -> list[Any]:
    return [c for c in guild.channels if c.permissions_for(member).view_channel]


async def command_values(command: Any, values: dict[str, Any], interaction: Any) -> dict[str, Any]:
    if set(values) - {p.name for p in command.parameters}:
        raise ValueError("Unknown form field.")
    result = {}
    for p in command.parameters:
        if p.name == "visibility":
            result[p.name] = app_commands.Choice(name="Only me", value="private")
            continue
        if p.name not in values:
            if p.required:
                raise ValueError(f"{p.name.replace('_', ' ')} is required.")
            result[p.name] = p.default
            continue
        value = values[p.name]
        kind = p.type.name
        if kind == "string":
            if not isinstance(value, str):
                raise ValueError("Expected text.")
            measure: float = len(value)
        elif kind in {"integer", "number"}:
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError("Expected a number.")
            if kind == "integer" and not isinstance(value, int):
                raise ValueError("Expected a whole number.")
            measure = value
        elif kind == "boolean":
            if not isinstance(value, bool):
                raise ValueError("Expected yes or no.")
            measure = 0
        elif kind == "channel":
            value = interaction.guild.get_channel(int(value))
            if value is None or not value.permissions_for(interaction.user).view_channel:
                raise PermissionError("That channel is not accessible.")
            if p.channel_types and value.type not in p.channel_types:
                raise ValueError("Choose a channel of the required type.")
            expected = getattr(command._params[p.name]._annotation, "_types", ())
            if expected and not isinstance(value, expected):
                raise ValueError("That channel type is not supported by this feature.")
            result[p.name] = value
            continue
        elif kind == "user":
            value = await interaction.guild.fetch_member(int(value))
            result[p.name] = value
            continue
        else:
            raise ValueError("Unsupported field type.")
        if p.min_value is not None and measure < p.min_value:
            raise ValueError(f"{p.name} must be at least {p.min_value}.")
        if p.max_value is not None and measure > p.max_value:
            raise ValueError(f"{p.name} must be at most {p.max_value}.")
        if p.choices and value not in [c.value for c in p.choices]:
            raise ValueError("Choose one of the listed options.")
        result[p.name] = await command._params[p.name].transform(interaction, value)
    return result


class WebFlow:
    def __init__(self, command: Any, guild_id: int, channel_id: int, owner: str) -> None:
        self.id = secrets.token_urlsafe(24)
        self.command = command
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.owner = owner
        self.expires = time.monotonic() + 300
        self.messages: list[dict[str, Any]] = []
        self.view: Any = None
        self.modal: Any = None
        self.revision = 0

    def update(self, content: Any = None, *, append: bool = False, **kwargs: Any) -> None:
        if append or not self.messages:
            self.messages.append({"content": "", "embeds": [], "images": []})
        message = self.messages[-1]
        if content is not None:
            message["content"] = str(content)
        if "embed" in kwargs:
            message["embeds"] = [kwargs["embed"].to_dict()] if kwargs["embed"] else []
        if "embeds" in kwargs:
            message["embeds"] = [e.to_dict() for e in kwargs["embeds"]]
        files = kwargs.get("attachments", kwargs.get("files", []))
        if kwargs.get("file") is not None:
            files = [kwargs["file"]]
        if "attachments" in kwargs or files:
            message["images"] = []
            for file in files:
                if not isinstance(file, discord.File):
                    continue
                file.fp.seek(0)
                data = file.fp.read(4 * 1024 * 1024)
                if data.startswith(b"\x89PNG\r\n\x1a\n"):
                    message["images"].append(
                        "data:image/png;base64," + base64.b64encode(data).decode()
                    )
        if "view" in kwargs:
            self.view = kwargs["view"]
        self.revision += 1

    def render(self, interaction: Any) -> dict[str, Any]:
        components = []
        container = self.modal or self.view
        if container is not None:
            for index, item in enumerate(container.children):
                if not isinstance(item, SUPPORTED_COMPONENTS):
                    raise ValueError(f"Web support missing for {type(item).__name__}.")
                data: dict[str, Any] = {
                    "index": index,
                    "disabled": getattr(item, "disabled", False),
                }
                if isinstance(item, discord.ui.Button):
                    data.update(
                        kind="button",
                        label=item.label or "Continue",
                        url=item.url,
                        style=item.style.name,
                    )
                elif isinstance(item, discord.ui.TextInput):
                    data.update(
                        kind="text",
                        label=item.label,
                        value=item.value or item.default or "",
                        min=item.min_length,
                        max=item.max_length,
                        required=item.required,
                        placeholder=item.placeholder or "",
                    )
                else:
                    options = []
                    if isinstance(item, discord.ui.Select):
                        options = [
                            {"label": o.label, "value": o.value, "default": o.default}
                            for o in item.options
                        ]
                    elif isinstance(item, discord.ui.ChannelSelect):
                        defaults = {o.id for o in item.default_values}
                        options = [
                            {"label": c.name, "value": str(c.id), "default": c.id in defaults}
                            for c in visible_channels(interaction.guild, interaction.user)
                            if not item.channel_types or c.type in item.channel_types
                        ]
                    elif isinstance(item, discord.ui.UserSelect):
                        defaults = {o.id for o in item.default_values}
                        options = [
                            {
                                "label": m.display_name,
                                "value": str(m.id),
                                "default": m.id in defaults,
                            }
                            for m in interaction.guild.members
                            if not m.bot
                        ]
                    data.update(
                        kind="select",
                        label=item.placeholder or "Choose an option",
                        options=options,
                        min=item.min_values,
                        max=item.max_values,
                    )
                components.append(data)
        return {
            "id": self.id,
            "revision": self.revision,
            "messages": self.messages,
            "components": components,
            "modal": self.modal.title if self.modal else None,
        }


class WebResponse:
    def __init__(self, interaction: WebInteraction) -> None:
        self.interaction = interaction
        self.done = False

    def is_done(self) -> bool:
        return self.done

    async def defer(self, **kwargs: Any) -> None:
        self.done = True

    async def send_message(self, content: Any = None, **kwargs: Any) -> None:
        self.done = True
        self.interaction.flow.update(content, append=True, **kwargs)

    async def edit_message(self, content: Any = None, **kwargs: Any) -> None:
        self.done = True
        self.interaction.flow.update(content, **kwargs)

    async def send_modal(self, modal: Any) -> None:
        self.done = True
        self.interaction.flow.modal = modal
        self.interaction.flow.revision += 1


class WebInteraction:
    slopbot_web = True

    def __init__(self, bot: Any, member: Any, channel: Any, flow: WebFlow) -> None:
        self.client = bot
        self.user = member
        self.guild = member.guild
        self.guild_id = member.guild.id
        self.channel = channel
        self.channel_id = channel.id
        self.permissions = channel.permissions_for(member)
        self.app_permissions = channel.permissions_for(self.guild.me)
        self.command = flow.command
        self.created_at = datetime.now(UTC)
        self.id = discord.utils.time_snowflake(self.created_at)
        self.type = discord.InteractionType.application_command
        self.data: dict[str, Any] = {}
        self.namespace = SimpleNamespace()
        self.extras: dict[str, Any] = {}
        self.flow = flow
        self.response = WebResponse(self)
        self.followup = SimpleNamespace(send=self.response.send_message)

    async def edit_original_response(self, content: Any = None, **kwargs: Any) -> None:
        self.flow.update(content, **kwargs)

    async def delete_original_response(self) -> None:
        self.flow.messages.clear()
        self.flow.view = None


async def invoke_command(interaction: Any, values: dict[str, Any]) -> None:
    command = interaction.command
    require_command_permissions(command, interaction.user)
    if not await command._check_can_run(interaction):
        raise PermissionError("This feature is not available in this context.")
    arguments = await command_values(command, values, interaction)
    await command.callback(command.binding, interaction, **arguments)


async def invoke_component(interaction: Any, payload: dict[str, Any]) -> None:
    flow = interaction.flow
    require_command_permissions(flow.command, interaction.user)
    if payload.get("revision") != flow.revision:
        raise ValueError("This form has changed. Open the feature again.")
    if flow.modal is not None:
        modal = flow.modal
        values = payload.get("values", {})
        if not isinstance(values, dict):
            raise ValueError("Invalid form.")
        for index, item in enumerate(modal.children):
            value = values.get(str(index), "")
            if not isinstance(value, str) or len(value) > (item.max_length or 4000):
                raise ValueError("Invalid text length.")
            if (item.required and not value) or (value and len(value) < (item.min_length or 0)):
                raise ValueError(f"Enter {item.label.lower()}.")
            item._value = value
        if not await modal.interaction_check(interaction):
            raise PermissionError("This form belongs to another user.")
        flow.modal = None
        flow.revision += 1
        await modal.on_submit(interaction)
        return
    view = flow.view
    if view is None or view.is_finished():
        raise ValueError("This form has expired. Open the feature again.")
    selected_index = payload.get("index")
    if type(selected_index) is not int or not 0 <= selected_index < len(view.children):
        raise ValueError("Invalid control.")
    item = view.children[selected_index]
    if item.disabled:
        raise ValueError("This control is disabled.")
    if not await view.interaction_check(interaction):
        raise PermissionError("This form belongs to another user.")
    if isinstance(item, discord.ui.Button):
        if item.url:
            raise ValueError("Open this link in your browser.")
    else:
        values = payload.get("values", [])
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ValueError("Invalid selection.")
        if len(set(values)) != len(values) or not item.min_values <= len(values) <= item.max_values:
            raise ValueError("Choose the requested number of options.")
        if isinstance(item, discord.ui.Select):
            if set(values) - {o.value for o in item.options}:
                raise ValueError("Choose one of the listed options.")
            item._values = values
        elif isinstance(item, discord.ui.ChannelSelect):
            channels = {
                str(c.id): c
                for c in visible_channels(interaction.guild, interaction.user)
                if not item.channel_types or c.type in item.channel_types
            }
            if set(values) - channels.keys():
                raise PermissionError("A selected channel is not accessible.")
            item._values = [channels[v] for v in values]
        elif isinstance(item, discord.ui.UserSelect):
            item._values = [await interaction.guild.fetch_member(int(v)) for v in values]
        else:
            raise ValueError("Unsupported control.")
    flow.revision += 1
    await item.callback(interaction)
