from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from slopbot.itemshop import ItemShopCog, filter_items, watch_description
from slopbot.models import ItemShopItem, ItemShopPrice, ItemShopWatch


def item(*, title: str, amount: int, attributes: tuple[tuple[str, str], ...]) -> ItemShopItem:
    return ItemShopItem(
        key=title,
        shop_id=1,
        shop_name="Featured",
        shop_type="Daily",
        shop_item_id=1,
        title=title,
        description="A Rocket League item",
        image_url="",
        starts_at=100,
        ends_at=200,
        product_ids=(42,),
        attributes=attributes,
        prices=(ItemShopPrice(1, amount),),
    )


def test_item_shop_filter_matches_attributes_and_price() -> None:
    crimson = item(title="Octane Decal", amount=700, attributes=(("Painted", "Crimson"),))
    blue = item(title="Fennec Decal", amount=1200, attributes=(("Painted", "Sky Blue"),))

    assert filter_items([blue, crimson], query="crimson", max_price=800) == [crimson]
    assert filter_items([blue, crimson], query="decal", max_price=800) == [crimson]
    assert filter_items([blue, crimson], query="blue", max_price=800) == []


def test_watch_description_explains_all_items_and_filters() -> None:
    assert watch_description(ItemShopWatch(1, 2, 3, "", None)) == "all items"
    description = watch_description(ItemShopWatch(1, 2, 3, "Crimson", 800))
    assert "Crimson" in description
    assert "800" in description


def test_item_shop_command_group_has_expected_commands() -> None:
    assert {command.name for command in ItemShopCog.itemshop.commands} == {
        "current",
        "watch",
        "watches",
        "unwatch",
        "changes",
    }


def test_shareable_item_shop_commands_offer_visibility() -> None:
    current = {parameter.name: parameter for parameter in ItemShopCog.current.parameters}
    changes = {parameter.name: parameter for parameter in ItemShopCog.changes.parameters}

    assert [choice.value for choice in current["visibility"].choices] == ["public", "private"]
    assert [choice.value for choice in changes["visibility"].choices] == ["public", "private"]


async def test_watch_rejects_when_shop_service_is_not_configured() -> None:
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=1),
        response=SimpleNamespace(send_message=AsyncMock()),
        user=SimpleNamespace(id=2),
    )
    store = SimpleNamespace(save_item_shop_watch=AsyncMock())
    cog = ItemShopCog(SimpleNamespace(), store, None, None)

    await cog.watch.callback(cog, interaction)

    interaction.response.send_message.assert_awaited_once_with(
        "Rocket League shop alerts are not configured on this bot.", ephemeral=True
    )
    store.save_item_shop_watch.assert_not_awaited()
