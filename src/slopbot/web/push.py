from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from slopbot.web.auth import WebIdentityStore

LOGGER = logging.getLogger(__name__)


def load_or_create_vapid_key(path: Path) -> tuple[str, str]:
    """Return a stable private-key path and URL-safe uncompressed public key."""
    if path.exists():
        private_key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    else:
        private_key = ec.generate_private_key(ec.SECP256R1())
        pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as key_file:
            key_file.write(pem)
    if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(
        private_key.curve, ec.SECP256R1
    ):
        raise ValueError("The VAPID private key must use the P-256 curve.")
    path.chmod(0o600)
    public = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return str(path), base64.urlsafe_b64encode(public).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True)
class PushOutcome:
    delivered: bool
    retryable: bool
    detail: str


class WebPushService:
    def __init__(
        self,
        store: WebIdentityStore,
        *,
        private_key: str,
        public_key: str,
        subject: str,
    ) -> None:
        self.store = store
        self.private_key = private_key
        self.public_key = public_key
        self.subject = subject

    @property
    def configured(self) -> bool:
        return bool(self.private_key and self.public_key and self.subject)

    async def has_subscriptions(self, discord_user_id: int) -> bool:
        return bool(await self.store.push_subscriptions_for_discord(str(discord_user_id)))

    async def send(
        self, *, discord_user_id: int, title: str, body: str, url: str = "/"
    ) -> PushOutcome:
        if not self.configured:
            return PushOutcome(False, True, "PWA push is not configured")
        subscriptions = await self.store.push_subscriptions_for_discord(str(discord_user_id))
        if not subscriptions:
            return PushOutcome(False, False, "No subscribed PWA device")
        payload = json.dumps(
            {
                "title": title[:100],
                "body": body[:500],
                "url": url if url.startswith("/") else "/",
                "icon": "/static/icon-192.png",
                "badge": "/static/icon-192.png",
            }
        )
        delivered = False
        retryable = False
        for subscription in subscriptions:
            try:
                await asyncio.to_thread(self._send_sync, subscription, payload)
                delivered = True
            except Exception as error:
                status = getattr(getattr(error, "response", None), "status_code", None)
                if status in {404, 410}:
                    await self.store.delete_push_endpoint(subscription["endpoint"])
                    LOGGER.info("Removed expired PWA push endpoint")
                else:
                    retryable = True
                    LOGGER.exception("PWA push delivery failed")
        return PushOutcome(
            delivered,
            retryable and not delivered,
            "Delivered"
            if delivered
            else "PWA push delivery failed"
            if retryable
            else "No active PWA device",
        )

    def _send_sync(self, subscription: dict[str, str], payload: str) -> None:
        from pywebpush import webpush  # type: ignore[import-untyped]

        webpush(
            subscription_info={
                "endpoint": subscription["endpoint"],
                "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
            },
            data=payload,
            vapid_private_key=self.private_key,
            vapid_claims={"sub": self.subject},
            ttl=300,
        )
