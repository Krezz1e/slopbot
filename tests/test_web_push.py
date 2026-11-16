from __future__ import annotations

from types import SimpleNamespace

from slopbot.web.auth import WebIdentityStore
from slopbot.web.push import WebPushService, load_or_create_vapid_key


def test_vapid_key_is_generated_once_with_private_permissions(tmp_path) -> None:
    path = tmp_path / "vapid-private.pem"
    private_path, public_key = load_or_create_vapid_key(path)
    second_path, second_public_key = load_or_create_vapid_key(path)

    assert private_path == second_path == str(path)
    assert public_key == second_public_key
    assert len(public_key) == 87
    assert path.stat().st_mode & 0o777 == 0o600


async def linked_store(tmp_path):
    store = WebIdentityStore(tmp_path / "web.db")
    await store.initialize()
    user = await store.user(await store.login("subject", "Player", "player@example.com"))
    assert user is not None
    await store.link_discord(user["id"], "123")
    return store, user


async def test_push_subscription_lifecycle_and_delivery(tmp_path, monkeypatch) -> None:
    store, user = await linked_store(tmp_path)
    await store.save_push_subscription(
        user["id"], "https://push.example/subscription", "p" * 32, "a" * 16
    )
    service = WebPushService(
        store, private_key="private", public_key="public", subject="mailto:test@example.com"
    )
    sent = []
    monkeypatch.setattr(service, "_send_sync", lambda subscription, payload: sent.append(payload))

    outcome = await service.send(
        discord_user_id=123, title="Test title", body="Test body", url="/#notifications"
    )

    assert outcome.delivered is True
    assert sent and "Test title" in sent[0]
    assert await store.push_subscription_count(user["id"]) == 1
    assert await store.delete_push_subscription(user["id"], "https://push.example/subscription")
    assert await store.push_subscription_count(user["id"]) == 0


async def test_expired_push_subscription_is_removed(tmp_path, monkeypatch) -> None:
    store, user = await linked_store(tmp_path)
    endpoint = "https://push.example/expired"
    await store.save_push_subscription(user["id"], endpoint, "p" * 32, "a" * 16)
    service = WebPushService(
        store, private_key="private", public_key="public", subject="https://slopbot.test"
    )
    error = RuntimeError("expired")
    error.response = SimpleNamespace(status_code=410)
    monkeypatch.setattr(service, "_send_sync", lambda *_: (_ for _ in ()).throw(error))

    outcome = await service.send(discord_user_id=123, title="Test", body="Body")

    assert outcome.delivered is False
    assert outcome.retryable is False
    assert await store.push_subscription_count(user["id"]) == 0
