from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from slopbot.database import (
    GetOnRateLimitError,
    RocketLeagueAccountConflictError,
    SubscriptionLimitError,
    SubscriptionStore,
)
from slopbot.models import (
    MAX_SUBSCRIPTIONS_PER_USER,
    TOURNAMENT_GAME_MODES,
    CommandPolicy,
    DeliveryMethod,
    ItemShopItem,
    ItemShopPrice,
    KeywordNotificationMode,
    ResponseVisibility,
    RocketLeagueEvent,
)


@pytest.fixture
async def store(tmp_path: Path) -> SubscriptionStore:
    result = SubscriptionStore(tmp_path / "nested" / "subscriptions.db")
    await result.initialize()
    return result


async def test_save_and_list_subscription(store: SubscriptionStore) -> None:
    saved = await store.save(
        guild_id=10,
        user_id=20,
        channel_id=30,
        threshold=3,
        notify_on_reach=True,
        notify_on_drop=False,
    )

    assert saved.id > 0
    assert await store.list_for_user(guild_id=10, user_id=20) == [saved]
    assert await store.list_for_channel(guild_id=10, channel_id=30) == [saved]


async def test_save_upserts_same_user_channel(store: SubscriptionStore) -> None:
    first = await store.save(
        guild_id=10,
        user_id=20,
        channel_id=30,
        threshold=3,
        notify_on_reach=True,
        notify_on_drop=False,
    )
    updated = await store.save(
        guild_id=10,
        user_id=20,
        channel_id=30,
        threshold=5,
        notify_on_reach=False,
        notify_on_drop=True,
    )

    assert updated.id == first.id
    assert updated.threshold == 5
    assert updated.notify_on_reach is False
    assert updated.notify_on_drop is True
    assert len(await store.list_for_user(guild_id=10, user_id=20)) == 1


async def test_subscription_limit_is_enforced_atomically(store: SubscriptionStore) -> None:
    for channel_id in range(MAX_SUBSCRIPTIONS_PER_USER):
        await store.save(
            guild_id=10,
            user_id=20,
            channel_id=channel_id,
            threshold=3,
            notify_on_reach=True,
            notify_on_drop=False,
        )

    with pytest.raises(SubscriptionLimitError, match="up to 25"):
        await store.save(
            guild_id=10,
            user_id=20,
            channel_id=999,
            threshold=3,
            notify_on_reach=True,
            notify_on_drop=False,
        )


async def test_users_and_guilds_are_isolated(store: SubscriptionStore) -> None:
    for guild_id, user_id in [(1, 10), (1, 11), (2, 10)]:
        await store.save(
            guild_id=guild_id,
            user_id=user_id,
            channel_id=99,
            threshold=3,
            notify_on_reach=True,
            notify_on_drop=False,
        )

    assert len(await store.list_for_user(guild_id=1, user_id=10)) == 1
    assert len(await store.list_for_channel(guild_id=1, channel_id=99)) == 2


async def test_delete_requires_matching_owner(store: SubscriptionStore) -> None:
    saved = await store.save(
        guild_id=1,
        user_id=2,
        channel_id=3,
        threshold=3,
        notify_on_reach=True,
        notify_on_drop=False,
    )

    assert not await store.delete_for_user(
        subscription_id=saved.id,
        guild_id=1,
        user_id=999,
    )
    assert await store.get_for_user(subscription_id=saved.id, guild_id=1, user_id=2) == saved
    assert await store.delete_for_user(subscription_id=saved.id, guild_id=1, user_id=2)
    assert await store.get_for_user(subscription_id=saved.id, guild_id=1, user_id=2) is None


async def test_delete_channel_and_guild_cleanup(store: SubscriptionStore) -> None:
    for channel_id in [10, 11]:
        await store.save(
            guild_id=1,
            user_id=2,
            channel_id=channel_id,
            threshold=3,
            notify_on_reach=True,
            notify_on_drop=False,
        )

    assert await store.delete_channel(guild_id=1, channel_id=10) == 1
    assert await store.delete_guild(guild_id=1) == 1
    assert await store.list_for_user(guild_id=1, user_id=2) == []


async def test_deleted_channel_disables_pending_server_session_summaries(
    store: SubscriptionStore,
) -> None:
    from slopbot.sessions import Sessions

    sessions = Sessions(store)
    await sessions.save(1, 0, cutoff=300, enabled=True, channel_id=10)
    await store.save_rocket_league_account(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|session-player|0",
        player_name="Session Player",
    )
    for observed_at, matches in ((100, 10), (200, 11)):
        await store.record_rocket_league_activity_batch(
            guild_id=1,
            discord_user_id=2,
            player_id="Epic|session-player|0",
            player_name="Session Player",
            activities=[(11, matches, 1, 1000 + matches, "Diamond I")],
            threshold=3,
            observed_at=observed_at,
        )

    await store.delete_channel(guild_id=1, channel_id=11)
    assert (await sessions.settings(1, 0))["enabled"] == 1
    assert (await sessions.history(1, "Epic|session-player|0", 0))[0]["notified"] == 0

    await store.delete_channel(guild_id=1, channel_id=10)

    assert await sessions.settings(1, 0) == {
        "cutoff": 300,
        "enabled": 0,
        "channel_id": None,
        "minimum": 1,
        "include_observations": 1,
    }
    assert await sessions.finish(501) == []


async def test_delete_guild_removes_rank_transition_state(store: SubscriptionStore) -> None:
    await store.save_rocket_league_account(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|rank-state|0",
        player_name="Rank State",
    )
    await store.record_rocket_league_activity_batch(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|rank-state|0",
        player_name="Rank State",
        activities=[(11, 10, 0, 1000, "Diamond I")],
        rank_positions={11: (13, 0)},
        threshold=3,
        observed_at=100,
    )
    assert (
        await store._run(
            lambda c: c.execute(
                "SELECT COUNT(*) FROM rocket_league_rank_state_visits WHERE guild_id=1"
            ).fetchone()[0]
        )
        == 1
    )

    await store.delete_guild(guild_id=1)

    assert (
        await store._run(
            lambda c: c.execute(
                "SELECT COUNT(*) FROM rocket_league_rank_state_visits WHERE guild_id=1"
            ).fetchone()[0]
        )
        == 0
    )


async def test_invalid_preferences_do_not_reach_database(store: SubscriptionStore) -> None:
    with pytest.raises(ValueError, match="at least one"):
        await store.save(
            guild_id=1,
            user_id=2,
            channel_id=3,
            threshold=3,
            notify_on_reach=False,
            notify_on_drop=False,
        )


async def test_initialize_retains_obsolete_fomo_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE fomo_settings (guild_id INTEGER)")
        connection.execute("CREATE TABLE fomo_subscribers (guild_id INTEGER)")
    legacy_store = SubscriptionStore(database_path)

    await legacy_store.initialize()

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        names = {row[0] for row in rows}
    assert "fomo_settings" in names
    assert "fomo_subscribers" in names


async def test_notification_delivery_defaults_to_discord(store: SubscriptionStore) -> None:
    assert await store.get_delivery_method(guild_id=1, user_id=2) is DeliveryMethod.DISCORD

    await store.set_delivery_method(guild_id=1, user_id=2, delivery_method=DeliveryMethod.BOTH)

    assert await store.get_delivery_method(guild_id=1, user_id=2) is DeliveryMethod.BOTH

    await store.set_delivery_method(guild_id=1, user_id=2, delivery_method=DeliveryMethod.ALL)
    assert await store.get_delivery_method(guild_id=1, user_id=2) is DeliveryMethod.ALL


async def test_notification_delivery_migration_preserves_legacy_preferences(tmp_path) -> None:
    database_path = tmp_path / "legacy.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE notification_preferences (guild_id INTEGER NOT NULL, "
            "user_id INTEGER NOT NULL, delivery_method TEXT NOT NULL DEFAULT 'discord' "
            "CHECK (delivery_method IN ('discord', 'telegram', 'both')), "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY (guild_id, user_id))"
        )
        connection.execute(
            "INSERT INTO notification_preferences (guild_id,user_id,delivery_method) "
            "VALUES (1,2,'both')"
        )
    migrated = SubscriptionStore(database_path)
    await migrated.initialize()

    assert await migrated.get_delivery_method(guild_id=1, user_id=2) is DeliveryMethod.BOTH
    await migrated.set_delivery_method(guild_id=1, user_id=2, delivery_method=DeliveryMethod.PWA)
    assert await migrated.get_delivery_method(guild_id=1, user_id=2) is DeliveryMethod.PWA


async def test_command_policies_default_public_and_unrestricted(
    store: SubscriptionStore,
) -> None:
    policy = await store.get_command_policy(guild_id=1, command_name="rank")

    assert policy.default_visibility is ResponseVisibility.PUBLIC
    assert policy.channel_restricted is False
    assert policy.channel_ids == ()
    assert await store.command_channel_is_allowed(
        guild_id=1,
        command_name="rank",
        channel_ids=[10],
    )


async def test_command_policy_visibility_and_channel_allowlist(
    store: SubscriptionStore,
) -> None:
    await store.set_command_visibility(
        guild_id=1,
        command_name="rank",
        visibility=ResponseVisibility.PRIVATE,
    )
    await store.allow_command_channel(guild_id=1, command_name="rank", channel_id=10)
    policy = await store.allow_command_channel(guild_id=1, command_name="rank", channel_id=11)

    assert policy.default_visibility is ResponseVisibility.PRIVATE
    assert policy.channel_restricted is True
    assert policy.channel_ids == (10, 11)
    assert await store.command_channel_is_allowed(
        guild_id=1,
        command_name="rank",
        channel_ids=[11],
    )
    assert not await store.command_channel_is_allowed(
        guild_id=1,
        command_name="rank",
        channel_ids=[12],
    )

    policy = await store.disallow_command_channel(
        guild_id=1,
        command_name="rank",
        channel_id=10,
    )
    assert policy.channel_ids == (11,)
    policy = await store.clear_command_channel_restrictions(
        guild_id=1,
        command_name="rank",
    )
    assert policy.default_visibility is ResponseVisibility.PRIVATE
    assert policy.channel_restricted is False
    assert policy.channel_ids == ()


async def test_command_policy_cleanup_follows_channel_and_guild_lifecycle(
    store: SubscriptionStore,
) -> None:
    await store.allow_command_channel(guild_id=1, command_name="rank", channel_id=10)
    await store.allow_command_channel(guild_id=1, command_name="rank", channel_id=11)

    await store.delete_channel(guild_id=1, channel_id=10)
    assert (await store.get_command_policy(guild_id=1, command_name="rank")).channel_ids == (11,)
    await store.delete_guild(guild_id=1)
    assert await store.get_command_policy(guild_id=1, command_name="rank") == CommandPolicy(
        command_name="rank",
        default_visibility=ResponseVisibility.PUBLIC,
        channel_restricted=False,
        channel_ids=(),
    )


async def test_server_summary_queries_are_scoped_to_one_guild(store: SubscriptionStore) -> None:
    await store.set_command_visibility(
        guild_id=1,
        command_name="rank",
        visibility=ResponseVisibility.PRIVATE,
    )
    await store.set_command_visibility(
        guild_id=2,
        command_name="rank",
        visibility=ResponseVisibility.PRIVATE,
    )
    for guild_id, channel_id in [(1, 10), (2, 20)]:
        await store.save_tournament_channel_alert(
            guild_id=guild_id,
            channel_id=channel_id,
            configured_by_user_id=3,
            region="USE",
            lead_minutes=15,
            team_sizes=(2,),
            game_modes=("soccar",),
        )

    assert [policy.command_name for policy in await store.list_command_policies(guild_id=1)] == [
        "rank"
    ]
    assert [
        alert.channel_id
        for alert in await store.list_tournament_channel_alerts_for_guild(guild_id=1)
    ] == [10]


def shop_item(
    *,
    key: str = "1:2",
    title: str = "Crimson Octane Decal",
    amount: int = 700,
) -> ItemShopItem:
    return ItemShopItem(
        key=key,
        shop_id=1,
        shop_name="Featured Shop",
        shop_type="Featured",
        shop_item_id=int(key.split(":")[1]),
        title=title,
        description="Animated decal",
        image_url="https://example.com/item.png",
        starts_at=100,
        ends_at=200,
        product_ids=(42,),
        attributes=(("Painted", "Crimson"),),
        prices=(ItemShopPrice(1, amount),),
    )


async def test_item_shop_baseline_is_quiet_and_changes_are_deduplicated(
    store: SubscriptionStore,
) -> None:
    first = await store.save_item_shop_watch(guild_id=1, user_id=2, query="crimson", max_price=800)
    duplicate = await store.save_item_shop_watch(
        guild_id=1, user_id=2, query="  CRIMSON  ", max_price=800
    )
    await store.save_item_shop_watch(guild_id=1, user_id=2, query="", max_price=None)
    await store.save_item_shop_watch(guild_id=1, user_id=3, query="blue", max_price=None)
    await store.save_item_shop_watch(guild_id=1, user_id=4, query="crimson", max_price=800)

    assert duplicate.id == first.id
    assert await store.replace_item_shop_snapshot(items=[shop_item()], observed_at=100) == 0
    assert await store.list_due_item_shop_deliveries(now=100) == []

    updated = shop_item(amount=900)
    assert await store.replace_item_shop_snapshot(items=[updated], observed_at=200) == 1
    deliveries = await store.list_due_item_shop_deliveries(now=200)

    assert {delivery.user_id for delivery in deliveries} == {2, 4}
    assert all(delivery.change.change_type == "updated" for delivery in deliveries)
    assert all(delivery.change.item == updated for delivery in deliveries)


async def test_item_shop_tracks_add_update_remove_and_delivery_state(
    store: SubscriptionStore,
) -> None:
    await store.save_item_shop_watch(guild_id=1, user_id=2, query="", max_price=None)
    initial = shop_item()
    await store.replace_item_shop_snapshot(items=[initial], observed_at=100)

    added = shop_item(key="1:3", title="Blue Wheels", amount=500)
    assert await store.replace_item_shop_snapshot(items=[initial, added], observed_at=200) == 1
    assert (
        await store.replace_item_shop_snapshot(items=[shop_item(amount=750)], observed_at=300) == 2
    )

    changes = await store.list_item_shop_changes(since=0)
    assert [change.change_type for change in changes] == ["removed", "updated", "added"]
    assert (await store.list_item_shop_changes(since=0, query="blue"))[0].item == added

    delivery = (await store.list_due_item_shop_deliveries(now=300))[0]
    await store.retry_item_shop_delivery(
        change_id=delivery.change.id,
        guild_id=delivery.guild_id,
        user_id=delivery.user_id,
        next_attempt_at=400,
        detail="temporary",
    )
    assert all(
        item.change.id != delivery.change.id
        for item in await store.list_due_item_shop_deliveries(now=399)
    )
    retried = next(
        item
        for item in await store.list_due_item_shop_deliveries(now=400)
        if item.change.id == delivery.change.id
    )
    assert retried.attempt_count == 1
    await store.finish_item_shop_delivery(
        change_id=retried.change.id,
        guild_id=retried.guild_id,
        user_id=retried.user_id,
    )
    assert all(
        item.change.id != retried.change.id
        for item in await store.list_due_item_shop_deliveries(now=400)
    )


async def test_telegram_link_codes_are_single_use(store: SubscriptionStore) -> None:
    await store.save_telegram_link_code(
        discord_user_id=2,
        code_hash="secret-hash",
        expires_at=int(time.time()) + 60,
    )

    assert (
        await store.consume_telegram_link_code(code_hash="secret-hash", telegram_chat_id=900) == 2
    )
    assert await store.get_telegram_chat_id(discord_user_id=2) == 900
    assert (
        await store.consume_telegram_link_code(code_hash="secret-hash", telegram_chat_id=901)
        is None
    )


async def test_unlink_telegram_resets_delivery_preferences(store: SubscriptionStore) -> None:
    await store.save_telegram_link_code(
        discord_user_id=2,
        code_hash="secret-hash",
        expires_at=int(time.time()) + 60,
    )
    await store.consume_telegram_link_code(code_hash="secret-hash", telegram_chat_id=900)
    await store.set_delivery_method(guild_id=1, user_id=2, delivery_method=DeliveryMethod.TELEGRAM)

    assert await store.unlink_telegram(discord_user_id=2)
    assert await store.get_telegram_chat_id(discord_user_id=2) is None
    assert await store.get_delivery_method(guild_id=1, user_id=2) is DeliveryMethod.DISCORD


async def test_unlink_telegram_preserves_pwa_delivery(store: SubscriptionStore) -> None:
    await store.save_telegram_link_code(
        discord_user_id=2,
        code_hash="secret-hash",
        expires_at=int(time.time()) + 60,
    )
    await store.consume_telegram_link_code(code_hash="secret-hash", telegram_chat_id=900)
    await store.set_delivery_method(guild_id=1, user_id=2, delivery_method=DeliveryMethod.ALL)
    await store.set_delivery_method(
        guild_id=2, user_id=2, delivery_method=DeliveryMethod.TELEGRAM_PWA
    )

    assert await store.unlink_telegram(discord_user_id=2)
    assert await store.get_delivery_method(guild_id=1, user_id=2) is DeliveryMethod.DISCORD_PWA
    assert await store.get_delivery_method(guild_id=2, user_id=2) is DeliveryMethod.PWA


async def test_rocket_league_accounts_are_saved_listed_and_unlinked(
    store: SubscriptionStore,
) -> None:
    saved = await store.save_rocket_league_account(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
    )

    assert await store.get_rocket_league_account(guild_id=1, discord_user_id=2) == saved
    assert await store.list_rocket_league_accounts(guild_id=1, discord_user_ids=[2, 3]) == [saved]
    assert await store.list_rocket_league_accounts(guild_id=2, discord_user_ids=[2]) == []
    other_server = await store.save_rocket_league_account(
        guild_id=2,
        discord_user_id=3,
        player_id="Epic|account-id|0",
        player_name="Player One",
    )
    assert other_server.guild_id == 2
    assert await store.unlink_rocket_league_account(guild_id=1, discord_user_id=2)
    assert await store.get_rocket_league_account(guild_id=1, discord_user_id=2) is None


async def test_unlink_then_new_link_hides_old_account_leaderboard_history(
    store: SubscriptionStore,
) -> None:
    await store.save_rocket_league_account(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|old-account|0",
        player_name="Old Account",
    )
    await store.record_rocket_league_activity_batch(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|old-account|0",
        player_name="Old Account",
        activities=[(11, 10, 0, 1500, "Champion I")],
        rank_positions={11: (16, 0)},
        threshold=3,
        observed_at=100,
    )

    def old_record(c: sqlite3.Connection) -> int:
        cursor = c.execute(
            "INSERT INTO rocket_league_all_time_streak_records "
            "(guild_id,discord_user_id,player_id,player_name,scope,result,streak_count,"
            "observed_at) VALUES (1,2,'Epic|old-account|0','Old Account','main','win',10,100)"
        )
        c.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    old_record_id = await store._run(old_record)
    await store.unlink_rocket_league_account(guild_id=1, discord_user_id=2)
    assert (
        await store._run(
            lambda c: c.execute(
                "SELECT COUNT(*) FROM rocket_league_rank_state_visits "
                "WHERE guild_id=1 AND discord_user_id=2"
            ).fetchone()[0]
        )
        == 0
    )
    await store.save_rocket_league_account(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|new-account|0",
        player_name="New Account",
    )
    await store.record_rocket_league_activity(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|new-account|0",
        player_name="New Account",
        playlist=11,
        matches_played=10,
        win_streak=0,
        mmr=1000,
        threshold=3,
        observed_at=200,
        rank="Diamond I",
    )

    def new_record(c: sqlite3.Connection) -> int:
        cursor = c.execute(
            "INSERT INTO rocket_league_all_time_streak_records "
            "(guild_id,discord_user_id,player_id,player_name,scope,result,streak_count,"
            "observed_at) VALUES (1,2,'Epic|new-account|0','New Account','main','win',3,200)"
        )
        c.commit()
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    new_record_id = await store._run(new_record)

    peaks = await store.list_rocket_league_peak_mmr(guild_id=1, playlist=11, limit=10)
    records = await store.list_rocket_league_records(
        guild_id=1, streak_class="main", result="win", limit=10
    )
    assert [peak.player_id for peak in peaks] == ["Epic|new-account|0"]
    assert [record.player_id for record in records] == ["Epic|new-account|0"]
    assert (
        await store.get_rocket_league_peak_mmr_position(
            guild_id=1, player_id="Epic|new-account|0", playlist=11
        )
        == 1
    )
    assert await store.get_rocket_league_record_position(guild_id=1, record_id=new_record_id) == 1
    assert (
        await store.get_rocket_league_record_position(guild_id=1, record_id=old_record_id) is None
    )

    # Historical rows remain available if the old account is deliberately relinked.
    assert (
        await store.get_rocket_league_peak_mmr(
            guild_id=1, player_id="Epic|old-account|0", playlist=11
        )
        is not None
    )


async def test_rocket_league_account_cannot_be_claimed_by_two_users(
    store: SubscriptionStore,
) -> None:
    await store.save_rocket_league_account(
        guild_id=1,
        discord_user_id=2,
        player_id="Steam|76561198000000000|0",
        player_name="Player One",
    )

    with pytest.raises(RocketLeagueAccountConflictError, match="already linked"):
        await store.save_rocket_league_account(
            guild_id=1,
            discord_user_id=3,
            player_id="Steam|76561198000000000|0",
            player_name="Imposter",
        )


async def test_unlinked_rocket_league_account_is_tracked_without_a_discord_user(
    store: SubscriptionStore,
) -> None:
    saved = await store.save_unlinked_rocket_league_account(
        guild_id=1,
        player_id="Epic|unlinked-account|0",
        player_name="Epic Player",
    )

    assert saved.unlinked
    assert saved.discord_user_id < 0
    assert (
        await store.get_unlinked_rocket_league_account(
            guild_id=1, player_id="Epic|unlinked-account|0"
        )
        == saved
    )
    assert saved in await store.list_all_rocket_league_accounts(guild_id=1)
    assert saved in await store.list_linked_rocket_league_accounts()
    assert await store.unlink_unlinked_rocket_league_account(
        guild_id=1, player_id="Epic|unlinked-account|0"
    )


async def test_linking_an_unlinked_account_claims_it_without_duplicate_tracking(
    store: SubscriptionStore,
) -> None:
    unlinked = await store.save_unlinked_rocket_league_account(
        guild_id=1,
        player_id="Epic|claim-me|0",
        player_name="Epic Player",
    )
    await store.record_rocket_league_activity(
        guild_id=1,
        discord_user_id=unlinked.discord_user_id,
        player_id=unlinked.player_id,
        player_name=unlinked.player_name,
        playlist=11,
        matches_played=10,
        win_streak=1,
        mmr=1000,
        threshold=3,
        observed_at=1000,
    )
    await store.record_rocket_league_activity(
        guild_id=1,
        discord_user_id=unlinked.discord_user_id,
        player_id=unlinked.player_id,
        player_name=unlinked.player_name,
        playlist=11,
        matches_played=11,
        win_streak=2,
        mmr=1010,
        threshold=3,
        observed_at=1010,
    )

    linked = await store.save_rocket_league_account(
        guild_id=1,
        discord_user_id=42,
        player_id=unlinked.player_id,
        player_name=unlinked.player_name,
    )

    accounts = await store.list_all_rocket_league_accounts(guild_id=1)
    history = await store.list_rocket_league_match_history(guild_id=1, discord_user_id=42, since=0)
    assert accounts == [linked]
    assert [event.result for event in history] == ["win"]


async def test_tournament_alert_settings_are_persistent_and_isolated(
    store: SubscriptionStore,
) -> None:
    personal = await store.save_tournament_user_alert(
        guild_id=1,
        user_id=2,
        region="USE",
        lead_minutes=15,
        team_sizes=(1, 2),
        game_modes=("soccar", "hoops"),
    )
    channel = await store.save_tournament_channel_alert(
        guild_id=1,
        channel_id=3,
        configured_by_user_id=4,
        region="auto",
        lead_minutes=30,
        team_sizes=(1, 2, 3),
        game_modes=("soccar", "rumble", "pentathlon"),
    )

    assert await store.get_tournament_user_alert(guild_id=1, user_id=2) == personal
    assert await store.get_tournament_user_alert(guild_id=2, user_id=2) is None
    assert await store.get_tournament_channel_alert(guild_id=1, channel_id=3) == channel
    assert await store.list_tournament_user_alerts() == [personal]
    assert await store.list_tournament_channel_alerts() == [channel]


async def test_existing_single_size_tournament_alert_is_migrated(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy-tournaments.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        CREATE TABLE tournament_user_alerts (
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            region TEXT NOT NULL DEFAULT 'auto',
            lead_minutes INTEGER NOT NULL DEFAULT 15,
            team_size INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (guild_id, user_id)
        )
        """
    )
    connection.execute(
        "INSERT INTO tournament_user_alerts "
        "(guild_id, user_id, region, lead_minutes, team_size) VALUES (1, 2, 'USE', 30, 2)"
    )
    connection.commit()
    connection.close()

    legacy_store = SubscriptionStore(database_path)
    await legacy_store.initialize()

    migrated = await legacy_store.get_tournament_user_alert(guild_id=1, user_id=2)
    assert migrated is not None
    assert migrated.team_sizes == (2,)
    assert migrated.game_modes == TOURNAMENT_GAME_MODES


async def test_tournament_delivery_claims_are_deduplicated_and_releasable(
    store: SubscriptionStore,
) -> None:
    claim = {
        "target_type": "user",
        "guild_id": 1,
        "target_id": 2,
        "tournament_id": 42,
        "start_time": 1_800_000_000,
        "lead_minutes": 15,
        "now": 1_799_999_000,
    }

    assert await store.claim_tournament_delivery(**claim)
    assert not await store.claim_tournament_delivery(**claim)
    await store.release_tournament_delivery(
        target_type="user",
        guild_id=1,
        target_id=2,
        tournament_id=42,
        start_time=1_800_000_000,
        lead_minutes=15,
    )
    assert await store.claim_tournament_delivery(**claim)
    assert await store.prune_tournament_deliveries(before=1_800_000_001) == 1


async def test_tournament_delivery_claim_is_recovered_after_crash_but_not_after_finish(
    store: SubscriptionStore,
) -> None:
    claim = {
        "target_type": "channel",
        "guild_id": 1,
        "target_id": 3,
        "tournament_id": 42,
        "start_time": 2_000,
        "lead_minutes": 15,
    }

    assert await store.claim_tournament_delivery(**claim, now=100)
    assert not await store.claim_tournament_delivery(**claim, now=399)
    assert await store.claim_tournament_delivery(**claim, now=400)
    await store.finish_tournament_delivery(**claim)
    assert not await store.claim_tournament_delivery(**claim, now=1_000)


async def test_tournament_opt_in_is_one_shot_and_due_at_configured_lead(
    store: SubscriptionStore,
) -> None:
    await store.save_tournament_opt_in_offer(
        token="abc123",
        guild_id=1,
        channel_id=3,
        tournament_id=42,
        tournament_title="2v2 Soccar Cup",
        start_time=2_000,
        region="USE",
        lead_minutes=15,
        now=100,
    )

    assert (
        await store.subscribe_tournament_opt_in(token="abc123", guild_id=1, user_id=2, now=100)
        == "created"
    )
    assert (
        await store.subscribe_tournament_opt_in(token="abc123", guild_id=1, user_id=2, now=100)
        == "existing"
    )
    assert await store.list_due_tournament_opt_ins(now=1_099) == []
    due = await store.list_due_tournament_opt_ins(now=1_100)
    assert len(due) == 1
    assert due[0].tournament_title == "2v2 Soccar Cup"

    await store.finish_tournament_opt_in(token="abc123", user_id=2)
    assert await store.list_due_tournament_opt_ins(now=1_100) == []


async def test_tournament_opt_in_rejects_wrong_server_and_expired_offer(
    store: SubscriptionStore,
) -> None:
    await store.save_tournament_opt_in_offer(
        token="abc123",
        guild_id=1,
        channel_id=3,
        tournament_id=42,
        tournament_title="Cup",
        start_time=200,
        region="USE",
        lead_minutes=15,
        now=100,
    )

    assert (
        await store.subscribe_tournament_opt_in(token="abc123", guild_id=2, user_id=2, now=100)
        == "expired"
    )
    assert (
        await store.subscribe_tournament_opt_in(token="abc123", guild_id=1, user_id=2, now=200)
        == "expired"
    )


async def test_channel_cleanup_preserves_already_requested_one_time_reminder(
    store: SubscriptionStore,
) -> None:
    await store.save_tournament_channel_alert(
        guild_id=1,
        channel_id=3,
        configured_by_user_id=4,
        region="USE",
        lead_minutes=15,
        team_sizes=(2,),
        game_modes=("soccar",),
    )
    await store.save_tournament_opt_in_offer(
        token="abc123",
        guild_id=1,
        channel_id=3,
        tournament_id=42,
        tournament_title="Cup",
        start_time=2_000,
        region="USE",
        lead_minutes=15,
        now=100,
    )
    await store.subscribe_tournament_opt_in(token="abc123", guild_id=1, user_id=2, now=100)

    await store.delete_channel(guild_id=1, channel_id=3)

    due = await store.list_due_tournament_opt_ins(now=1_100)
    assert [(item.token, item.user_id) for item in due] == [("abc123", 2)]


async def test_tournament_alert_cleanup_follows_channel_and_guild_lifecycle(
    store: SubscriptionStore,
) -> None:
    await store.save_tournament_user_alert(
        guild_id=1,
        user_id=2,
        region="auto",
        lead_minutes=15,
        team_sizes=(1, 2, 3),
        game_modes=("soccar",),
    )
    await store.save_tournament_channel_alert(
        guild_id=1,
        channel_id=3,
        configured_by_user_id=4,
        region="auto",
        lead_minutes=15,
        team_sizes=(1, 2, 3),
        game_modes=("soccar",),
    )

    await store.delete_channel(guild_id=1, channel_id=3)
    assert await store.get_tournament_channel_alert(guild_id=1, channel_id=3) is None
    await store.delete_guild(guild_id=1)
    assert await store.get_tournament_user_alert(guild_id=1, user_id=2) is None


async def test_database_healthcheck(store: SubscriptionStore) -> None:
    assert await store.healthcheck()


async def test_slopbot_status_alert_configs_and_state_are_persistent(
    store: SubscriptionStore,
) -> None:
    await store.set_slopbot_status_user_alert(guild_id=1, user_id=2, enabled=True)
    await store.set_slopbot_status_channel_alert(
        guild_id=1, channel_id=3, configured_by_user_id=2, enabled=True
    )

    assert await store.has_slopbot_status_user_alert(guild_id=1, user_id=2)
    assert await store.list_slopbot_status_user_alerts() == [(1, 2)]
    assert await store.list_slopbot_status_channel_alerts() == [(1, 3)]
    assert await store.replace_slopbot_status_state(fingerprint="healthy") is None
    assert await store.replace_slopbot_status_state(fingerprint="unhealthy") == "healthy"

    await store.set_slopbot_status_user_alert(guild_id=1, user_id=2, enabled=False)
    await store.delete_channel(guild_id=1, channel_id=3)
    assert await store.list_slopbot_status_user_alerts() == []
    assert await store.list_slopbot_status_channel_alerts() == []


async def test_delete_guild_removes_rocket_league_links(store: SubscriptionStore) -> None:
    await store.save_rocket_league_account(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
    )

    await store.delete_guild(guild_id=1)

    assert await store.get_rocket_league_account(guild_id=1, discord_user_id=2) is None


async def test_rocket_league_tracking_config_is_server_scoped(
    store: SubscriptionStore,
) -> None:
    configured = await store.configure_rocket_league_streaks(
        guild_id=1,
        channel_id=99,
        threshold=4,
        inactivity_seconds=1200,
    )
    configured = await store.configure_rocket_league_leaderboard(
        guild_id=1,
        channel_id=100,
        playlists=[11, 13, 99],
    )

    assert configured.streak_channel_id == 99
    assert configured.streak_threshold == 4
    assert configured.inactivity_seconds == 1200
    assert configured.leaderboard_channel_id == 100
    assert configured.leaderboard_playlists == (11, 13, 99)
    assert (await store.get_rocket_league_tracking_config(guild_id=2)).streak_channel_id is None


async def test_existing_single_leaderboard_playlist_is_migrated(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        CREATE TABLE rocket_league_tracking_config (
            guild_id INTEGER PRIMARY KEY,
            streak_channel_id INTEGER,
            streak_threshold INTEGER NOT NULL DEFAULT 3,
            inactivity_seconds INTEGER NOT NULL DEFAULT 900,
            leaderboard_channel_id INTEGER,
            leaderboard_playlist INTEGER NOT NULL DEFAULT 11,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.execute(
        "INSERT INTO rocket_league_tracking_config "
        "(guild_id, leaderboard_channel_id, leaderboard_playlist) VALUES (1, 100, 13)"
    )
    connection.commit()
    connection.close()
    legacy_store = SubscriptionStore(database_path)

    await legacy_store.initialize()

    config = await legacy_store.get_rocket_league_tracking_config(guild_id=1)
    assert config.leaderboard_channel_id == 100
    assert config.leaderboard_playlists == (13,)


async def test_grace_period_state_migration_drops_already_notified_streaks(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "grace-period.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        CREATE TABLE rocket_league_streak_state (
            guild_id INTEGER NOT NULL,
            discord_user_id INTEGER NOT NULL,
            playlist INTEGER NOT NULL,
            result TEXT,
            streak_count INTEGER NOT NULL DEFAULT 0,
            last_match_at INTEGER,
            notification_sent INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, discord_user_id, playlist)
        )
        """
    )
    connection.executemany(
        "INSERT INTO rocket_league_streak_state "
        "(guild_id, discord_user_id, playlist, result, streak_count, last_match_at, "
        "notification_sent) VALUES (1, ?, 11, 'win', 3, 1000, ?)",
        [(2, 1), (3, 0)],
    )
    connection.commit()
    connection.close()

    grace_store = SubscriptionStore(database_path)
    await grace_store.initialize()

    connection = sqlite3.connect(database_path)
    rows = connection.execute(
        "SELECT discord_user_id FROM rocket_league_streak_state ORDER BY discord_user_id"
    ).fetchall()
    connection.close()
    assert rows == [(3,)]


async def test_activity_history_streak_reversal_and_inactivity(
    store: SubscriptionStore,
) -> None:
    await store.save_rocket_league_account(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
    )
    await store.configure_rocket_league_streaks(
        guild_id=1,
        channel_id=99,
        threshold=3,
        inactivity_seconds=300,
    )

    async def observe(matches: int, streak: int, mmr: float, observed_at: int):  # type: ignore[no-untyped-def]
        return await store.record_rocket_league_activity(
            guild_id=1,
            discord_user_id=2,
            player_id="Epic|account-id|0",
            player_name="Player One",
            playlist=11,
            matches_played=matches,
            win_streak=streak,
            mmr=mmr,
            threshold=3,
            observed_at=observed_at,
        )

    assert await observe(10, 0, 1000, 1000) == []
    assert await observe(11, 1, 1010, 1010) == []
    assert await observe(12, 2, 1020, 1020) == []
    assert await observe(13, 3, 1030, 1030) == []
    completed = await observe(14, -1, 1020, 1040)

    assert len(completed) == 1
    assert completed[0].result == "win"
    assert completed[0].count == 3
    assert completed[0].ended_by == "opposite_result"
    history = await store.list_rocket_league_match_history(guild_id=1, discord_user_id=2, since=0)
    assert [event.result for event in history] == ["win", "win", "win", "loss"]

    assert await observe(15, -2, 1010, 1050) == []
    assert await observe(16, -3, 1000, 1060) == []
    inactive = await store.finish_inactive_rocket_league_streaks(now=1361)
    assert len(inactive) == 1
    assert inactive[0].result == "loss"
    assert inactive[0].count == 3
    assert await store.finish_inactive_rocket_league_streaks(now=2000) == []


async def test_streak_records_rank_each_streak_instead_of_each_player(
    store: SubscriptionStore,
) -> None:
    await store.save_rocket_league_account(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
    )

    async def observe(matches: int, streak: int, observed_at: int) -> None:
        await store.record_rocket_league_activity(
            guild_id=1,
            discord_user_id=2,
            player_id="Epic|account-id|0",
            player_name="Player One",
            playlist=11,
            matches_played=matches,
            win_streak=streak,
            mmr=1000 + matches,
            threshold=3,
            observed_at=observed_at,
        )

    await observe(10, 0, 1000)
    await observe(11, 1, 1010)
    await observe(12, 2, 1020)
    await observe(13, -1, 1030)
    await observe(14, 1, 1040)
    await observe(15, 2, 1050)
    await observe(16, 3, 1060)
    await observe(17, -1, 1070)

    wins = await store.list_rocket_league_records(
        guild_id=1, streak_class="main", result="win", limit=3
    )
    losses = await store.list_rocket_league_records(
        guild_id=1, streak_class="main", result="loss", limit=3
    )

    assert [(record.discord_user_id, record.count) for record in wins] == [(2, 3), (2, 2)]
    assert [record.count for record in losses] == [1]
    assert await store.get_rocket_league_record_position(guild_id=1, record_id=wins[0].id) == 1
    assert await store.get_rocket_league_record_position(guild_id=1, record_id=wins[1].id) == 2
    assert await store.get_rocket_league_record_position(guild_id=1, record_id=999999) is None


async def test_peak_mmr_keeps_one_high_water_mark_per_player(
    store: SubscriptionStore,
) -> None:
    await store.save_rocket_league_account(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
    )
    for matches, mmr in [(10, 1000.0), (11, 1050.0), (12, 1020.0)]:
        await store.record_rocket_league_activity(
            guild_id=1,
            discord_user_id=2,
            player_id="Epic|account-id|0",
            player_name="Player One",
            playlist=11,
            matches_played=matches,
            win_streak=matches - 9,
            mmr=mmr,
            threshold=3,
            observed_at=matches * 10,
            rank="Diamond II, Division III",
        )

    peaks = await store.list_rocket_league_peak_mmr(guild_id=1, playlist=11, limit=3)

    assert len(peaks) == 1
    assert peaks[0].player_id == "Epic|account-id|0"
    assert peaks[0].mmr == 1050.0
    assert peaks[0].rank == "Diamond II, Division III"
    assert peaks[0].observed_at == 110
    assert (
        await store.get_rocket_league_peak_mmr(
            guild_id=1, player_id="Epic|account-id|0", playlist=11
        )
        == peaks[0]
    )
    assert (
        await store.get_rocket_league_peak_mmr_position(
            guild_id=1, player_id="Epic|account-id|0", playlist=11
        )
        == 1
    )
    await store.save_rocket_league_account(
        guild_id=1,
        discord_user_id=3,
        player_id="Epic|higher|0",
        player_name="Higher Player",
    )
    await store.record_rocket_league_activity(
        guild_id=1,
        discord_user_id=3,
        player_id="Epic|higher|0",
        player_name="Higher Player",
        playlist=11,
        matches_played=10,
        win_streak=0,
        mmr=1400,
        threshold=3,
        observed_at=200,
        rank="Champion I, Division I",
    )
    assert (
        await store.get_rocket_league_peak_mmr_position(
            guild_id=1, player_id="Epic|account-id|0", playlist=11
        )
        == 2
    )
    assert (
        await store.get_rocket_league_peak_mmr_position(
            guild_id=1, player_id="Epic|higher|0", playlist=11
        )
        == 1
    )
    assert (
        await store.get_rocket_league_peak_mmr_position(
            guild_id=1, player_id="missing", playlist=11
        )
        is None
    )


async def test_inactive_streak_is_recorded_without_notifications(
    store: SubscriptionStore,
) -> None:
    await store.save_rocket_league_account(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
    )
    await store.record_rocket_league_activity(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
        playlist=11,
        matches_played=10,
        win_streak=0,
        mmr=1000,
        threshold=3,
        observed_at=1000,
    )
    await store.record_rocket_league_activity(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
        playlist=11,
        matches_played=11,
        win_streak=1,
        mmr=1010,
        threshold=3,
        observed_at=1010,
    )

    assert await store.finish_inactive_rocket_league_streaks(now=2811) == []
    records = await store.list_rocket_league_records(
        guild_id=1, streak_class="main", result="win", limit=3
    )
    assert records == []
    session_events = await store.list_rocket_league_events(
        guild_id=1, since=0, event_types=("session_streak_completed",)
    )
    assert [(event.count, event.total_count) for event in session_events] == [(1, 1)]


async def test_all_time_streak_records_are_bounded_to_the_top_fifteen(
    store: SubscriptionStore,
) -> None:
    await store.save_rocket_league_account(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
    )

    async def observe(matches: int, streak: int) -> None:
        await store.record_rocket_league_activity(
            guild_id=1,
            discord_user_id=2,
            player_id="Epic|account-id|0",
            player_name="Player One",
            playlist=11,
            matches_played=matches,
            win_streak=streak,
            mmr=1000 + streak,
            threshold=3,
            observed_at=1000 + matches,
        )

    await observe(0, 0)
    for index in range(51):
        await observe(index * 2 + 1, 1)
        await observe(index * 2 + 2, -1)

    records = await store.list_rocket_league_records(
        guild_id=1, streak_class="main", result="win", limit=51
    )
    assert len(records) == 15
    assert [record.count for record in records] == [1] * 15

    movement_events = await store.list_rocket_league_events(
        guild_id=1, since=0, event_types=("streak_leaderboard_movement",)
    )
    assert len(movement_events) == 60
    assert all(
        event.position_after is not None and event.position_after <= 15 for event in movement_events
    )


async def test_legacy_session_records_do_not_seed_fair_all_time_boards(
    store: SubscriptionStore,
) -> None:
    def seed_legacy_record(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT INTO rocket_league_combined_streak_records "
            "(guild_id, discord_user_id, player_id, player_name, streak_class, result, "
            "streak_count, observed_at) VALUES (1, 2, 'Epic|old|0', 'Old', 'main', "
            "'win', 99, 1000)"
        )
        connection.commit()

    await store._run(seed_legacy_record)

    assert (
        await store.list_rocket_league_records(
            guild_id=1, streak_class="main", result="win", limit=10
        )
        == []
    )


async def test_main_streak_combines_1v1_2v2_and_3v3(store: SubscriptionStore) -> None:
    await store.save_rocket_league_account(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
    )

    async def observe(playlist: int, matches: int, streak: int, observed_at: int) -> None:
        await store.record_rocket_league_activity(
            guild_id=1,
            discord_user_id=2,
            player_id="Epic|account-id|0",
            player_name="Player One",
            playlist=playlist,
            matches_played=matches,
            win_streak=streak,
            mmr=1000 + matches,
            threshold=3,
            observed_at=observed_at,
        )

    for playlist in (10, 11, 13):
        await observe(playlist, 10, 0, 1000)
    await observe(10, 11, 1, 1010)
    await observe(11, 11, 1, 1020)
    await observe(13, 11, 1, 1030)
    completed = await store.record_rocket_league_activity(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
        playlist=10,
        matches_played=12,
        win_streak=-1,
        mmr=1000,
        threshold=3,
        observed_at=1040,
    )

    assert len(completed) == 1
    assert completed[0].streak_class == "main"
    assert completed[0].result == "win"
    assert completed[0].count == 3


async def test_all_ranked_streak_combines_special_modes_without_changing_competitive(
    store: SubscriptionStore,
) -> None:
    await store.save_rocket_league_account(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
    )
    for playlist in (11, 27):
        await store.record_rocket_league_activity(
            guild_id=1,
            discord_user_id=2,
            player_id="Epic|account-id|0",
            player_name="Player One",
            playlist=playlist,
            matches_played=10,
            win_streak=0,
            mmr=1000,
            threshold=3,
            observed_at=1000,
        )
    await store.record_rocket_league_activity(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
        playlist=11,
        matches_played=11,
        win_streak=1,
        mmr=1010,
        threshold=3,
        observed_at=1010,
    )
    await store.record_rocket_league_activity(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
        playlist=27,
        matches_played=11,
        win_streak=1,
        mmr=1010,
        threshold=3,
        observed_at=1020,
    )
    await store.record_rocket_league_activity(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
        playlist=11,
        matches_played=12,
        win_streak=-1,
        mmr=1000,
        threshold=3,
        observed_at=1030,
    )

    main = await store.list_rocket_league_records(
        guild_id=1, streak_class="main", result="win", limit=3
    )
    all_ranked = await store.list_rocket_league_records(
        guild_id=1, streak_class="all", result="win", limit=3
    )
    assert [record.count for record in main] == [1]
    assert [record.count for record in all_ranked] == [2]


async def test_mixed_simultaneous_main_results_do_not_manufacture_a_streak(
    store: SubscriptionStore,
) -> None:
    await store.save_rocket_league_account(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
    )
    await store.record_rocket_league_activity_batch(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
        activities=[(10, 10, 0, 1000, "Rank"), (11, 10, 0, 1000, "Rank")],
        threshold=3,
        observed_at=1000,
    )
    await store.record_rocket_league_activity_batch(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
        activities=[(10, 11, 1, 1010, "Rank"), (11, 11, -1, 990, "Rank")],
        threshold=3,
        observed_at=1010,
    )
    await store.record_rocket_league_activity(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
        playlist=10,
        matches_played=12,
        win_streak=2,
        mmr=1020,
        threshold=3,
        observed_at=1020,
    )
    await store.finish_inactive_rocket_league_streaks(now=1920)

    records = await store.list_rocket_league_records(
        guild_id=1, streak_class="main", result="win", limit=3
    )
    assert records == []


async def test_inactivity_breaks_subthreshold_streak_before_next_result(
    store: SubscriptionStore,
) -> None:
    await store.save_rocket_league_account(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
    )
    await store.configure_rocket_league_streaks(
        guild_id=1,
        channel_id=99,
        threshold=3,
        inactivity_seconds=300,
    )

    async def observe(matches: int, streak: int, observed_at: int):  # type: ignore[no-untyped-def]
        return await store.record_rocket_league_activity(
            guild_id=1,
            discord_user_id=2,
            player_id="Epic|account-id|0",
            player_name="Player One",
            playlist=11,
            matches_played=matches,
            win_streak=streak,
            mmr=1000 + matches,
            threshold=3,
            observed_at=observed_at,
            inactivity_seconds=300,
        )

    assert await observe(10, 0, 1000) == []
    assert await observe(11, 1, 1010) == []
    assert await observe(12, 2, 1020) == []

    # Rocket League's aggregate win counter reaches three, but the local streak must restart
    # because this match was observed after the configured five-minute inactivity timeout.
    assert await observe(13, 3, 1500) == []
    assert await observe(14, 4, 1510) == []
    assert await observe(15, 5, 1520) == []

    completed = await store.finish_inactive_rocket_league_streaks(now=1821)
    assert len(completed) == 1
    assert completed[0].result == "win"
    assert completed[0].count == 3


async def test_session_cutoff_does_not_discard_streak_state_early(
    store: SubscriptionStore,
) -> None:
    await store.save_rocket_league_account(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
    )

    async def observe(matches: int, streak: int, observed_at: int) -> None:
        await store.record_rocket_league_activity(
            guild_id=1,
            discord_user_id=2,
            player_id="Epic|account-id|0",
            player_name="Player One",
            playlist=11,
            matches_played=matches,
            win_streak=streak,
            mmr=1000 + matches,
            threshold=3,
            observed_at=observed_at,
        )

    await observe(10, 0, 1000)
    await observe(13, 3, 1010)

    # The default session cutoff is 30 minutes. The old cleanup query used a
    # stale 15-minute fallback and silently removed this state at this point.
    assert await store.finish_inactive_rocket_league_streaks(now=2000) == []
    state = await store._run(
        lambda connection: connection.execute(
            "SELECT result,streak_count FROM rocket_league_combined_streak_state "
            "WHERE guild_id=1 AND discord_user_id=2 AND streak_class='main'"
        ).fetchone()
    )
    assert state == ("win", 3)

    assert await store.finish_inactive_rocket_league_streaks(now=2811) == []
    state = await store._run(
        lambda connection: connection.execute(
            "SELECT result FROM rocket_league_combined_streak_state "
            "WHERE guild_id=1 AND discord_user_id=2 AND streak_class='main'"
        ).fetchone()
    )
    assert state is None
    events = await store.list_rocket_league_events(
        guild_id=1, since=0, event_types=("session_streak_completed",)
    )
    assert [(event.result, event.count) for event in events] == [("win", 3)]


async def test_session_continuations_use_cumulative_all_time_total(
    store: SubscriptionStore,
) -> None:
    await store.save_rocket_league_account(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Krezzle",
    )
    await store.configure_rocket_league_streaks(
        guild_id=1,
        channel_id=90,
        threshold=3,
        inactivity_seconds=300,
    )

    async def observe(matches: int, streak: int, observed_at: int) -> None:
        await store.record_rocket_league_activity(
            guild_id=1,
            discord_user_id=2,
            player_id="Epic|account-id|0",
            player_name="Krezzle",
            playlist=11,
            matches_played=matches,
            win_streak=streak,
            mmr=1000 + matches,
            threshold=3,
            observed_at=observed_at,
            inactivity_seconds=300,
        )

    await observe(10, 0, 1000)
    await observe(13, 3, 1010)
    first = await store.finish_inactive_rocket_league_streaks(now=1311)
    assert [(item.count, item.total_count, item.continued) for item in first] == [(3, 3, False)]

    await observe(14, 4, 1400)
    second = await store.finish_inactive_rocket_league_streaks(now=1701)
    assert [(item.count, item.total_count, item.continued) for item in second] == [(1, 4, True)]

    await observe(18, 8, 1800)
    third = await store.finish_inactive_rocket_league_streaks(now=2101)
    assert [(item.count, item.total_count, item.continued) for item in third] == [(4, 8, True)]

    # Inactivity only closes session segments. The permanent record appears on the loss.
    assert (
        await store.list_rocket_league_records(
            guild_id=1, streak_class="main", result="win", limit=10
        )
        == []
    )
    await observe(19, -1, 2200)
    competitive = await store.list_rocket_league_records(
        guild_id=1, streak_class="main", result="win", limit=10
    )
    all_ranked = await store.list_rocket_league_records(
        guild_id=1, streak_class="all", result="win", limit=10
    )
    assert [record.count for record in competitive] == [8]
    assert [record.count for record in all_ranked] == [8]

    session_events = await store.list_rocket_league_events(
        guild_id=1, since=0, event_types=("session_streak_completed",)
    )
    assert [(event.count, event.total_count) for event in reversed(session_events)] == [
        (3, 3),
        (1, 4),
        (4, 8),
    ]
    movement_events = await store.list_rocket_league_events(
        guild_id=1, since=0, event_types=("streak_leaderboard_movement",)
    )
    assert {(event.scope, event.count, event.position_after) for event in movement_events} == {
        ("main", 8, 1),
        ("all", 8, 1),
    }


async def test_uncertain_continuation_is_history_only_and_valid_continuation_delivers_once(
    store: SubscriptionStore,
) -> None:
    await store.save_rocket_league_account(
        guild_id=1, discord_user_id=2, player_id="p", player_name="Player"
    )
    await store.configure_rocket_league_streaks(
        guild_id=1, channel_id=90, threshold=3, inactivity_seconds=300
    )
    await store.configure_rocket_league_event_notification(
        guild_id=1,
        event_key="session_streak",
        channel_id=90,
        minimum_count=3,
        configured_at=0,
    )

    async def observe(matches, streak, at):
        return await store.record_rocket_league_activity(
            guild_id=1,
            discord_user_id=2,
            player_id="p",
            player_name="Player",
            playlist=11,
            matches_played=matches,
            win_streak=streak,
            mmr=1000 + matches,
            threshold=3,
            observed_at=at,
            inactivity_seconds=300,
        )

    async def deliver_pending(at):
        pending = await store.list_pending_rocket_league_event_notifications()
        for event, channel, key in pending:
            await store.mark_rocket_league_event_notification_delivered(
                event_id=event.id,
                event_key=key,
                channel_id=channel,
                delivered_at=at,
            )
        return [(e.count, e.total_count) for e, _, _ in pending]

    await observe(10, 0, 1000)
    await observe(13, 3, 1010)
    assert await store.list_pending_rocket_league_event_notifications() == []
    await store.finish_inactive_rocket_league_streaks(now=1311)
    assert await deliver_pending(1311) == [(3, 3)]
    await observe(14, 4, 1400)
    await store.finish_inactive_rocket_league_streaks(now=1700)  # exact cutoff stays active
    assert await store.list_pending_rocket_league_event_notifications() == []
    await store.finish_inactive_rocket_league_streaks(now=1701)
    assert await deliver_pending(1701) == [(1, 4)]
    await store.finish_inactive_rocket_league_streaks(now=1702)
    assert await store.list_pending_rocket_league_event_notifications() == []

    await observe(15, 5, 1800)
    assert await observe(18, 1, 1810) == []  # two uncertain games followed by one win
    history = await store.list_rocket_league_events(
        guild_id=1, since=0, event_types=("session_streak_completed",)
    )
    assert (history[0].count, history[0].total_count) == (1, 5)
    assert await store.list_pending_rocket_league_event_notifications() == []
    await store.initialize()  # suppression survives restarts and migration reruns
    await observe(18, 1, 1811)  # unchanged polling cannot retrigger it
    await store.finish_inactive_rocket_league_streaks(now=2111)
    assert await store.list_pending_rocket_league_event_notifications() == []


async def test_event_notification_routes_are_independent_and_delivered_once(
    store: SubscriptionStore,
) -> None:
    await store.configure_rocket_league_event_notification(
        guild_id=1,
        event_key="session_streak",
        channel_id=90,
        minimum_count=3,
        configured_at=0,
    )
    await store.configure_rocket_league_event_notification(
        guild_id=1,
        event_key="rank_change",
        channel_id=90,
        configured_at=0,
    )
    await store.record_rocket_league_events(
        guild_id=1,
        events=[
            RocketLeagueEvent(
                "session_streak_completed",
                2,
                "Epic|account-id|0",
                "Krezzle",
                0,
                100,
                result="win",
                count=1,
                scope="main",
                total_count=4,
            ),
            RocketLeagueEvent(
                "rank_up",
                2,
                "Epic|account-id|0",
                "Krezzle",
                11,
                100,
                rank_before="Gold III",
                rank_after="Platinum I",
            ),
        ],
    )

    pending = await store.list_pending_rocket_league_event_notifications()
    assert [(event.event_type, channel, key) for event, channel, key in pending] == [
        ("session_streak_completed", 90, "session_streak"),
        ("rank_up", 90, "rank_change"),
    ]
    first_event = pending[0][0]
    assert first_event.id is not None
    await store.mark_rocket_league_event_notification_delivered(
        event_id=first_event.id,
        event_key="session_streak",
        channel_id=90,
        delivered_at=200,
    )
    remaining = await store.list_pending_rocket_league_event_notifications()
    assert [(event.event_type, key) for event, _channel, key in remaining] == [
        ("rank_up", "rank_change")
    ]


async def test_rank_and_streak_leaderboard_notifications_are_separate(
    store: SubscriptionStore,
) -> None:
    await store.configure_rocket_league_event_notification(
        guild_id=1,
        event_key="leaderboard_movement",
        channel_id=91,
        configured_at=0,
    )
    await store.configure_rocket_league_event_notification(
        guild_id=1,
        event_key="streak_leaderboard_competitive",
        channel_id=92,
        configured_at=0,
    )
    await store.configure_rocket_league_event_notification(
        guild_id=1,
        event_key="streak_leaderboard_all_ranked",
        channel_id=93,
        configured_at=0,
    )
    await store.record_rocket_league_events(
        guild_id=1,
        events=[
            RocketLeagueEvent(
                "leaderboard_update",
                2,
                "Epic|account-id|0",
                "Krezzle",
                11,
                100,
                notification_text="Rank standings changed",
            ),
            RocketLeagueEvent(
                "streak_leaderboard_movement",
                2,
                "Epic|account-id|0",
                "Krezzle",
                0,
                100,
                result="loss",
                count=1,
                scope="main",
                total_count=1,
                position_after=14,
            ),
            RocketLeagueEvent(
                "streak_leaderboard_movement",
                2,
                "Epic|account-id|0",
                "Krezzle",
                0,
                100,
                result="loss",
                count=1,
                scope="all",
                total_count=1,
                position_after=14,
            ),
        ],
    )

    pending = await store.list_pending_rocket_league_event_notifications()
    assert [(event.event_type, channel, key) for event, channel, key in pending] == [
        ("leaderboard_update", 91, "leaderboard_movement"),
        ("streak_leaderboard_movement", 92, "streak_leaderboard_competitive"),
        ("streak_leaderboard_movement", 93, "streak_leaderboard_all_ranked"),
    ]


async def test_admin_session_changes_do_not_reset_all_time_state_or_records(
    store: SubscriptionStore,
) -> None:
    await store.save_rocket_league_account(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Krezzle",
    )

    async def observe(matches: int, streak: int, observed_at: int) -> None:
        await store.record_rocket_league_activity(
            guild_id=1,
            discord_user_id=2,
            player_id="Epic|account-id|0",
            player_name="Krezzle",
            playlist=11,
            matches_played=matches,
            win_streak=streak,
            mmr=1000 + matches,
            threshold=5,
            observed_at=observed_at,
            inactivity_seconds=600,
        )

    await observe(10, 0, 1000)
    await observe(12, 2, 1010)
    await store.configure_rocket_league_streaks(
        guild_id=1,
        channel_id=None,
        threshold=5,
        inactivity_seconds=600,
    )
    await observe(13, 3, 1020)
    await observe(14, -1, 1030)
    records_before = await store.list_rocket_league_records(
        guild_id=1, streak_class="main", result="win", limit=10
    )
    assert [record.count for record in records_before] == [3]

    await store.configure_rocket_league_streaks(
        guild_id=1,
        channel_id=91,
        threshold=10,
        inactivity_seconds=1200,
    )
    records_after = await store.list_rocket_league_records(
        guild_id=1, streak_class="main", result="win", limit=10
    )
    assert records_after == records_before


async def test_activity_gap_is_saved_and_resets_streak(store: SubscriptionStore) -> None:
    await store.save_rocket_league_account(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
    )
    await store.record_rocket_league_activity(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
        playlist=11,
        matches_played=10,
        win_streak=2,
        mmr=1000,
        threshold=3,
        observed_at=1000,
    )
    await store.record_rocket_league_activity(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
        playlist=11,
        matches_played=15,
        win_streak=2,
        mmr=1010,
        threshold=3,
        observed_at=1100,
    )

    history = await store.list_rocket_league_match_history(guild_id=1, discord_user_id=2, since=0)
    assert [(event.result, event.match_count) for event in history] == [
        ("gap", 3),
        ("win", 1),
        ("win", 1),
    ]


async def test_match_history_pruning_and_leaderboard_snapshots(
    store: SubscriptionStore,
) -> None:
    await store.save_rocket_league_account(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
    )
    await store.record_rocket_league_activity(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
        playlist=11,
        matches_played=10,
        win_streak=1,
        mmr=1000,
        threshold=3,
        observed_at=1000,
    )
    await store.record_rocket_league_activity(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
        playlist=11,
        matches_played=11,
        win_streak=2,
        mmr=1010,
        threshold=3,
        observed_at=1100,
    )
    assert await store.prune_rocket_league_match_history(before=1200) == 1
    assert (
        await store.list_rocket_league_match_history(guild_id=1, discord_user_id=2, since=0) == []
    )

    first = await store.replace_rocket_league_leaderboard_snapshot(
        guild_id=1, playlist=11, entries=[(2, 1, 1010.0), (3, 2, 1000.0)]
    )
    second = await store.replace_rocket_league_leaderboard_snapshot(
        guild_id=1, playlist=11, entries=[(3, 1, 1020.0), (2, 2, 1010.0)]
    )
    assert first is None
    assert second == [(2, 1, 1010.0), (3, 2, 1000.0)]


async def test_general_rocket_league_event_ledger_supports_reusable_filters(
    store: SubscriptionStore,
) -> None:
    account = await store.save_unlinked_rocket_league_account(
        guild_id=1,
        player_id="Epic|unlinked|0",
        player_name="Epic Player",
    )
    await store.record_rocket_league_activity_batch(
        guild_id=1,
        discord_user_id=account.discord_user_id,
        player_id=account.player_id,
        player_name=account.player_name,
        activities=[(11, 10, 1, 900, "Gold III, Division IV")],
        rank_positions={11: (8, 3)},
        threshold=3,
        observed_at=1000,
    )
    await store.record_rocket_league_activity_batch(
        guild_id=1,
        discord_user_id=account.discord_user_id,
        player_id=account.player_id,
        player_name=account.player_name,
        activities=[(11, 11, 2, 920, "Platinum I, Division I")],
        rank_positions={11: (9, 0)},
        threshold=3,
        observed_at=1100,
    )

    all_events = await store.list_rocket_league_events(guild_id=1, since=0)
    rank_events = await store.list_rocket_league_events(
        guild_id=1,
        since=0,
        discord_user_id=account.discord_user_id,
        event_types=("rank_up",),
        playlist=11,
    )

    assert {event.event_type for event in all_events} == {"match", "peak_mmr", "rank_up"}
    assert len(rank_events) == 1
    assert rank_events[0].player_name == "Epic Player"
    assert rank_events[0].rank_after == "Platinum I, Division I"


async def test_event_ledger_migration_backfills_match_history_once(tmp_path: Path) -> None:
    database_path = tmp_path / "pre-event-ledger.db"
    store = SubscriptionStore(database_path)
    await store.initialize()
    await store.save_rocket_league_account(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
    )
    await store.record_rocket_league_activity(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
        playlist=11,
        matches_played=10,
        win_streak=1,
        mmr=1000,
        threshold=3,
        observed_at=1000,
    )
    await store.record_rocket_league_activity(
        guild_id=1,
        discord_user_id=2,
        player_id="Epic|account-id|0",
        player_name="Player One",
        playlist=11,
        matches_played=11,
        win_streak=2,
        mmr=1010,
        threshold=3,
        observed_at=1100,
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE rocket_league_events")

    migrated = SubscriptionStore(database_path)
    await migrated.initialize()
    await migrated.initialize()

    events = await migrated.list_rocket_league_events(guild_id=1, since=0)
    assert [(event.event_type, event.result, event.player_name) for event in events] == [
        ("match", "win", "Player One")
    ]


async def test_deleted_notification_channel_disables_rocket_league_announcements(
    store: SubscriptionStore,
) -> None:
    await store.configure_rocket_league_streaks(
        guild_id=1, channel_id=99, threshold=3, inactivity_seconds=900
    )
    await store.configure_rocket_league_leaderboard(guild_id=1, channel_id=99, playlists=[11, 13])

    await store.delete_channel(guild_id=1, channel_id=99)

    config = await store.get_rocket_league_tracking_config(guild_id=1)
    assert config.streak_channel_id is None
    assert config.leaderboard_channel_id is None


async def test_keyword_profiles_are_private_and_channel_scoped(
    store: SubscriptionStore,
) -> None:
    profile = await store.get_keyword_profile(guild_id=1, user_id=2)
    assert profile.enabled is True
    assert profile.notification_mode is KeywordNotificationMode.ALWAYS

    await store.add_keyword(guild_id=1, user_id=2, keyword="rocket league?")
    await store.add_keyword(guild_id=1, user_id=2, keyword="rl?")
    await store.set_keyword_channels(guild_id=1, user_id=2, channel_ids=[10, 11])
    await store.set_keyword_notification_mode(
        guild_id=1,
        user_id=2,
        notification_mode=KeywordNotificationMode.OFFLINE_ONLY,
    )

    watchers = await store.list_keyword_watchers(guild_id=1, channel_id=10)
    assert len(watchers) == 1
    assert watchers[0].user_id == 2
    assert watchers[0].notification_mode is KeywordNotificationMode.OFFLINE_ONLY
    assert set(watchers[0].keywords) == {"rocket league?", "rl?"}
    assert await store.list_keyword_watchers(guild_id=1, channel_id=99) == []


async def test_disabled_keyword_profile_is_not_returned(store: SubscriptionStore) -> None:
    await store.add_keyword(guild_id=1, user_id=2, keyword="league?")
    await store.set_keyword_channels(guild_id=1, user_id=2, channel_ids=[10])
    await store.set_keyword_enabled(guild_id=1, user_id=2, enabled=False)

    assert await store.list_keyword_watchers(guild_id=1, channel_id=10) == []


async def test_geton_defaults_on_and_filters_opted_out_users(
    store: SubscriptionStore,
) -> None:
    assert await store.get_geton_enabled(guild_id=1, user_id=2) is True
    await store.set_geton_enabled(guild_id=1, user_id=3, enabled=False)

    accepted = await store.schedule_geton(
        guild_id=1,
        sender_id=9,
        sender_name="Krezzle",
        message="Get on Discord.",
        deliver_at=100,
        recipient_ids=[2, 3],
    )

    assert accepted == (2,)
    due = await store.list_due_geton_deliveries(now=100)
    assert len(due) == 1
    assert due[0].recipient_id == 2
    assert due[0].attempt_count == 0
    await store.mark_geton_delivered(request_id=due[0].request_id, user_id=2)
    assert await store.list_due_geton_deliveries(now=100) == []


async def test_failed_geton_delivery_is_deferred_for_retry(store: SubscriptionStore) -> None:
    await store.schedule_geton(
        guild_id=1,
        sender_id=9,
        sender_name="Krezzle",
        message="Get on Discord.",
        deliver_at=100,
        recipient_ids=[2],
    )
    delivery = (await store.list_due_geton_deliveries(now=100))[0]

    await store.retry_geton_delivery(
        request_id=delivery.request_id,
        user_id=delivery.recipient_id,
        next_attempt_at=200,
        error="temporary failure",
    )

    assert await store.list_due_geton_deliveries(now=199) == []
    retried = (await store.list_due_geton_deliveries(now=200))[0]
    assert retried.attempt_count == 1


async def test_geton_sender_cooldown_blocks_immediate_repeat(store: SubscriptionStore) -> None:
    request = {
        "guild_id": 1,
        "sender_id": 9,
        "sender_name": "Krezzle",
        "message": "Get on Discord.",
        "deliver_at": 100,
        "recipient_ids": [2],
    }
    await store.schedule_geton(**request)

    with pytest.raises(GetOnRateLimitError, match="Wait 30 seconds"):
        await store.schedule_geton(**request)


async def test_initialize_migrates_geton_retry_columns(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy-geton.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE geton_recipients (
                request_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (request_id, user_id)
            )
            """
        )
    legacy_store = SubscriptionStore(database_path)

    await legacy_store.initialize()

    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(geton_recipients)")}
    assert {"attempt_count", "next_attempt_at", "last_error"} <= columns


async def test_mmr_history_records_changes_and_supports_movers(
    store: SubscriptionStore,
) -> None:
    for user_id, player_id, player_name, start, end in (
        (10, "Epic|one|0", "Climber", 1000.0, 1040.0),
        (20, "Epic|two|0", "Faller", 1100.0, 1080.0),
    ):
        await store.record_rocket_league_activity_batch(
            guild_id=1,
            discord_user_id=user_id,
            player_id=player_id,
            player_name=player_name,
            activities=[(11, 1, 1, start, "Platinum I")],
            threshold=3,
            observed_at=1_000,
        )
        await store.record_rocket_league_activity_batch(
            guild_id=1,
            discord_user_id=user_id,
            player_id=player_id,
            player_name=player_name,
            activities=[(11, 2, 2, end, "Platinum II")],
            threshold=3,
            observed_at=2_000,
        )

    assert await store.list_rocket_league_mmr_history(
        guild_id=1,
        player_id="Epic|one|0",
        playlist=11,
        since=0,
    ) == [
        (1_000, 1000.0, "Platinum I"),
        (2_000, 1040.0, "Platinum II"),
    ]
    assert await store.list_rocket_league_movers(
        guild_id=1,
        playlist=11,
        since=0,
        limit=10,
    ) == [
        ("Epic|one|0", "Climber", 10, 1000.0, 1040.0),
        ("Epic|two|0", "Faller", 20, 1100.0, 1080.0),
    ]

    assert await store.prune_rocket_league_mmr_history(before=1_500) == 2
