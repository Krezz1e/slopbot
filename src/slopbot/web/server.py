from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import aiohttp
import discord
from aiohttp import web
from discord import app_commands

from slopbot.rocketleague import PLATFORMS, PLAYER_ACCOUNT_ID_PATTERN, PLAYLIST_NAMES
from slopbot.themes import THEME_MODES, THEME_PRESETS
from slopbot.tournaments import REGION_CHOICES
from slopbot.web.auth import SESSION_SECONDS, WebIdentityStore, digest
from slopbot.web.bridge import (
    WebFlow,
    WebInteraction,
    command_schema,
    invoke_command,
    invoke_component,
    require_command_permissions,
    visible_channels,
)
from slopbot.web.controls import Controls
from slopbot.web.dashboard import WIDGETS, Dashboard
from slopbot.web.push import WebPushService, load_or_create_vapid_key

LOGGER = logging.getLogger(__name__)
COOKIE = "__Host-slopbot"
STATE_COOKIE = "__Host-slopbot-oauth"
STATIC = Path(__file__).with_name("static")
ASSET_VERSION_TOKEN = "__SLOPBOT_ASSET_VERSION__"
PUBLIC_ASSETS = frozenset(
    {
        "app.js",
        "app.css",
        "dashboard.js",
        "dashboard.css",
        "controls.js",
        "theme.css",
        "refresh.css",
        "reports.js",
        "comic-neue-regular.woff2",
        "comic-neue-bold.woff2",
        "icon-192.png",
        "icon-512.png",
    }
)
MEMBERSHIP_READ_TIMEOUT_SECONDS = 2
PROVIDERS = {
    "google": (
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
        "https://openidconnect.googleapis.com/v1/userinfo",
        "openid email profile",
    ),
    "discord": (
        "https://discord.com/oauth2/authorize",
        "https://discord.com/api/oauth2/token",
        "https://discord.com/api/v10/users/@me",
        "identify",
    ),
}


@dataclass(frozen=True)
class WebSettings:
    origin: str
    port: int = 8080
    google_client_id: str = ""
    google_client_secret: str = ""
    discord_client_id: str = ""
    discord_client_secret: str = ""
    vapid_private_key: str = ""
    vapid_public_key: str = ""
    vapid_subject: str = ""

    @classmethod
    def from_environment(cls) -> WebSettings | None:
        origin = os.environ.get("SLOPBOT_WEB_ORIGIN", "").strip().rstrip("/")
        if not origin:
            return None
        url = urlsplit(origin)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.path
            or url.query
            or url.fragment
            or url.username
            or url.password
            or url.port not in (None, 443)
        ):
            raise ValueError("SLOPBOT_WEB_ORIGIN must be a bare HTTPS origin.")
        port = int(os.environ.get("SLOPBOT_WEB_PORT", "8080"))
        if not 1024 <= port <= 65535:
            raise ValueError("SLOPBOT_WEB_PORT must be between 1024 and 65535.")
        private_key = os.environ.get("SLOPBOT_VAPID_PRIVATE_KEY", "").strip()
        public_key = os.environ.get("SLOPBOT_VAPID_PUBLIC_KEY", "").strip()
        if bool(private_key) != bool(public_key):
            raise ValueError("Both SLOPBOT_VAPID_PRIVATE_KEY and PUBLIC_KEY are required.")
        return cls(
            origin=origin,
            port=port,
            google_client_id=os.environ.get("GOOGLE_CLIENT_ID", "").strip(),
            google_client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", "").strip(),
            discord_client_id=os.environ.get("DISCORD_CLIENT_ID", "").strip(),
            discord_client_secret=os.environ.get("DISCORD_CLIENT_SECRET", "").strip(),
            vapid_private_key=private_key,
            vapid_public_key=public_key,
            vapid_subject=os.environ.get("SLOPBOT_VAPID_SUBJECT", "").strip() or origin,
        )


class WebServer:
    def __init__(self, bot: Any, settings: WebSettings) -> None:
        self.bot = bot
        self.settings = settings
        self.asset_version = self._asset_version()
        self.identities = WebIdentityStore(bot.settings.database_path.with_suffix(".web.db"))
        private_key, public_key = settings.vapid_private_key, settings.vapid_public_key
        if not private_key:
            private_key, public_key = load_or_create_vapid_key(
                bot.settings.database_path.parent / "vapid-private.pem"
            )
        self.push = WebPushService(
            self.identities,
            private_key=private_key,
            public_key=public_key,
            subject=settings.vapid_subject,
        )
        self.dashboard = Dashboard(self)
        self.controls = Controls(self)
        from slopbot.web.reports import Reports

        self.reports = Reports(self)
        self.http: aiohttp.ClientSession | None = None
        self.runner: web.AppRunner | None = None
        self.flows: dict[str, WebFlow] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.member_reads: dict[tuple[int, int], asyncio.Task[Any]] = {}
        self.rates: dict[str, deque[float]] = defaultdict(deque)
        self.app = web.Application(client_max_size=32 * 1024, middlewares=[self.guard])
        self.app.add_routes(
            [
                web.get("/", self.index),
                web.get("/privacy", self.index),
                web.get("/terms", self.index),
                web.get("/offline", self.offline),
                web.get("/manifest.webmanifest", self.manifest),
                web.get("/service-worker.js", self.service_worker),
                web.get("/static/{name}", self.asset),
                web.get("/healthz", self.health),
                web.get("/auth/{provider}/start", self.auth_start),
                web.get("/auth/{provider}/callback", self.auth_callback),
                web.get("/api/me", self.me),
                web.get("/api/push", self.push_status),
                web.post("/api/push", self.push_subscription),
                web.get("/api/status", self.controls.status),
                web.get("/api/settings/{guild_id}", self.controls.read),
                web.post("/api/settings/{guild_id}/policy", self.controls.policy),
                web.post("/api/settings/{guild_id}/personal", self.controls.personal),
                web.post("/api/logout", self.logout),
                web.post("/api/discord/unlink", self.unlink),
                web.post("/api/account/delete", self.delete_account),
                web.get("/api/personal", self.personal),
                web.get("/api/dashboard", self.personal),
                web.post("/api/sessions/settings", self.session_settings),
                web.get("/api/reports", self.reports.read),
                web.get("/api/reports/snapshot", self.reports.snapshot),
                web.post("/api/reports/settings", self.reports.save),
                web.get("/api/leaderboards", self.leaderboards),
                web.get("/api/activity", self.activity),
                web.post("/api/preferences", self.preferences),
                web.post("/api/personal/link", self.link_player),
                web.post("/api/personal/unlink", self.unlink_player),
                web.get("/api/shop", self.shop),
                web.get("/api/tournaments", self.tournaments),
                web.get("/api/guilds", self.guilds),
                web.get("/api/guild/{guild_id}", self.guild),
                web.post("/api/command", self.command),
                web.post("/api/component", self.component),
            ]
        )

    def throttle(self, key: str, limit: int) -> None:
        now = time.monotonic()
        if len(self.rates) > 5000:
            self.rates = defaultdict(
                deque, {k: v for k, v in self.rates.items() if v and v[-1] > now - 60}
            )
            if len(self.rates) > 5000:
                raise web.HTTPTooManyRequests(text="The service is busy. Please try again.")
        bucket = self.rates[key]
        while bucket and bucket[0] < now - 60:
            bucket.popleft()
        if len(bucket) >= limit:
            raise web.HTTPTooManyRequests(text="Please wait a minute before trying again.")
        bucket.append(now)

    @web.middleware
    async def guard(self, request: web.Request, handler: Any) -> web.StreamResponse:
        started = time.monotonic()
        response: web.StreamResponse
        try:
            if request.method not in {"GET", "HEAD"}:
                if request.headers.get("Origin") != self.settings.origin:
                    raise web.HTTPForbidden(text="Invalid request origin.")
                token = request.cookies.get(COOKIE, "")
                if not token or not secrets.compare_digest(
                    request.headers.get("X-CSRF-Token", ""), digest(token + ":csrf")
                ):
                    raise web.HTTPForbidden(text="Your session has changed. Refresh the page.")
                if request.content_type != "application/json":
                    raise web.HTTPBadRequest(text="Expected JSON.")
            if request.path.startswith(("/api/", "/auth/")):
                # The listener is loopback-only; cloudflared supplies the trusted client IP.
                self.throttle(
                    "ip:" + request.headers.get("CF-Connecting-IP", request.remote or "local"), 120
                )
            response = await handler(request)
        except asyncio.CancelledError:
            LOGGER.warning(
                "Web request cancelled after %.3fs: %s %s",
                time.monotonic() - started,
                request.method,
                request.path,
            )
            raise
        except web.HTTPException as error:
            # aiohttp no longer accepts returning an HTTPException from middleware.
            # Preserve its status, body, and redirect headers as an ordinary response
            # so every response still receives the security headers below.
            response = web.Response(
                status=error.status,
                reason=error.reason,
                body=error.body,
                headers=error.headers,
            )
        except (ValueError, TypeError, KeyError) as error:
            response = web.json_response({"error": str(error)}, status=400)
        except PermissionError as error:
            response = web.json_response({"error": str(error)}, status=403)
        except app_commands.CommandOnCooldown as error:
            response = web.json_response(
                {"error": f"Try again in {error.retry_after:.0f} seconds."}, status=429
            )
        except (app_commands.CheckFailure, discord.Forbidden):
            response = web.json_response(
                {"error": "You do not have access to this feature."}, status=403
            )
        except discord.NotFound:
            response = web.json_response(
                {"error": "That server member or channel is no longer available."}, status=404
            )
        except Exception:
            LOGGER.exception("Web request failed: %s", request.path)
            response = web.json_response(
                {"error": "This service is temporarily unavailable. Please try again."}, status=503
            )
        if request.path.startswith("/static/") or request.path == "/manifest.webmanifest":
            if request.query.get("v") == self.asset_version:
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = (
                    "public, max-age=300, stale-while-revalidate=86400"
                )
        elif request.path == "/service-worker.js":
            response.headers["Cache-Control"] = "no-store, max-age=0, must-revalidate"
            # Prevent shared CDNs from extending a service worker beyond the origin policy.
            response.headers["CDN-Cache-Control"] = "no-store"
        else:
            response.headers["Cache-Control"] = "no-store"
        elapsed = time.monotonic() - started
        response.headers["Server-Timing"] = f"app;dur={elapsed * 1000:.1f}"
        if elapsed >= 1:
            LOGGER.warning(
                "Slow web request completed in %.3fs with %s: %s %s",
                elapsed,
                response.status,
                request.method,
                request.path,
            )
        response.headers.update(
            {
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; "
                "style-src 'self'; "
                "img-src 'self' data: https:; connect-src 'self'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                "Strict-Transport-Security": "max-age=31536000",
            }
        )
        return response

    async def start(self) -> None:
        await self.identities.initialize()
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.settings.port).start()
        LOGGER.info("Web interface listening on 127.0.0.1:%s", self.settings.port)

    async def close(self) -> None:
        await self.dashboard.close()
        await self.reports.fallback.service.close()
        if self.runner:
            await self.runner.cleanup()
        if self.http:
            await self.http.close()

    async def index(self, request: web.Request) -> web.Response:
        return web.Response(text=self._versioned_text("index.html"), content_type="text/html")

    async def offline(self, request: web.Request) -> web.Response:
        return web.Response(text=self._versioned_text("offline.html"), content_type="text/html")

    async def manifest(self, request: web.Request) -> web.Response:
        return web.Response(
            text=(STATIC / "manifest.webmanifest").read_text(encoding="utf-8"),
            content_type="application/manifest+json",
        )

    async def service_worker(self, request: web.Request) -> web.Response:
        response = web.Response(
            text=self._versioned_text("service-worker.js"),
            content_type="text/javascript",
        )
        response.headers["Service-Worker-Allowed"] = "/"
        return response

    async def asset(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        if name not in PUBLIC_ASSETS:
            raise web.HTTPNotFound()
        binary_content_types = {".png": "image/png", ".woff2": "font/woff2"}
        if content_type := binary_content_types.get(Path(name).suffix):
            return web.FileResponse(STATIC / name, headers={"Content-Type": content_type})
        return web.Response(
            text=(STATIC / name).read_text(encoding="utf-8"),
            content_type="text/javascript" if name.endswith(".js") else "text/css",
        )

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"web": "ok", "discord": self.bot.is_ready(), "asset_version": self.asset_version}
        )

    @staticmethod
    def _asset_version() -> str:
        fingerprint = hashlib.sha256()
        for name in sorted((*PUBLIC_ASSETS, "index.html", "offline.html", "service-worker.js")):
            fingerprint.update(name.encode())
            fingerprint.update((STATIC / name).read_bytes())
        return fingerprint.hexdigest()[:12]

    def _versioned_text(self, name: str) -> str:
        text = (STATIC / name).read_text(encoding="utf-8")
        if ASSET_VERSION_TOKEN not in text:
            raise RuntimeError(f"Missing asset version token in {name}.")
        return text.replace(ASSET_VERSION_TOKEN, self.asset_version)

    async def current_user(self, request: web.Request) -> dict[str, Any]:
        user = await self.identities.user(request.cookies.get(COOKIE, ""))
        if user is None:
            raise web.HTTPUnauthorized(text="Sign in to continue.")
        self.throttle(f"user:{user['id']}", 90)
        return user

    async def me(self, request: web.Request) -> web.Response:
        user = await self.identities.user(request.cookies.get(COOKIE, ""))
        return web.json_response(
            {
                "user": user,
                "preferences": await self.identities.preferences(user["id"]) if user else {},
                "csrf": digest(request.cookies.get(COOKIE, "") + ":csrf") if user else None,
                "google_ready": bool(
                    self.settings.google_client_id and self.settings.google_client_secret
                ),
                "discord_ready": bool(
                    self.settings.discord_client_id and self.settings.discord_client_secret
                ),
            }
        )

    async def push_status(self, request: web.Request) -> web.Response:
        user = await self.current_user(request)
        return web.json_response(
            {
                "configured": self.push.configured,
                "public_key": self.push.public_key if self.push.configured else None,
                "devices": await self.identities.push_subscription_count(user["id"]),
            }
        )

    async def push_subscription(self, request: web.Request) -> web.Response:
        user = await self.current_user(request)
        data = await request.json()
        if not isinstance(data, dict) or data.get("action") not in {"subscribe", "unsubscribe"}:
            raise ValueError("Invalid push subscription action.")
        endpoint = data.get("endpoint")
        if not isinstance(endpoint, str):
            raise ValueError("Invalid push subscription.")
        if data["action"] == "unsubscribe":
            await self.identities.delete_push_subscription(user["id"], endpoint)
        else:
            if not self.push.configured:
                raise ValueError("PWA push is not configured.")
            keys = data.get("keys")
            if (
                not isinstance(keys, dict)
                or not isinstance(keys.get("p256dh"), str)
                or not isinstance(keys.get("auth"), str)
            ):
                raise ValueError("Invalid push subscription.")
            await self.identities.save_push_subscription(
                user["id"], endpoint, keys["p256dh"], keys["auth"]
            )
        return web.json_response({"ok": True})

    def credentials(self, provider: str) -> tuple[str, str]:
        if provider not in PROVIDERS:
            raise web.HTTPNotFound()
        client_id = str(getattr(self.settings, provider + "_client_id"))
        secret = str(getattr(self.settings, provider + "_client_secret"))
        if not client_id or not secret:
            raise web.HTTPServiceUnavailable(
                text="Sign-in is still being configured. Please check back shortly."
            )
        return client_id, secret

    async def auth_start(self, request: web.Request) -> web.Response:
        provider = request.match_info["provider"]
        client_id, _ = self.credentials(provider)
        self.throttle("oauth:" + request.headers.get("CF-Connecting-IP", "local"), 10)
        session = request.cookies.get(COOKIE, "")
        if provider == "discord":
            await self.current_user(request)
        state, verifier = await self.identities.begin_oauth(provider, session)
        args = {
            "client_id": client_id,
            "redirect_uri": self.settings.origin + f"/auth/{provider}/callback",
            "response_type": "code",
            "scope": PROVIDERS[provider][3],
            "state": state,
        }
        if provider == "google":
            args.update(
                prompt="select_account",
                code_challenge_method="S256",
                code_challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("="),
            )
        response = web.Response(
            status=web.HTTPFound.status_code,
            headers={"Location": PROVIDERS[provider][0] + "?" + urlencode(args)},
        )
        response.set_cookie(
            STATE_COOKIE, state, secure=True, httponly=True, samesite="Lax", max_age=600, path="/"
        )
        return response

    async def auth_callback(self, request: web.Request) -> web.Response:
        provider = request.match_info["provider"]
        client_id, secret = self.credentials(provider)
        state = request.query.get("state", "")
        if not state or not secrets.compare_digest(state, request.cookies.get(STATE_COOKIE, "")):
            raise web.HTTPBadRequest(
                text="Sign-in expired or came from a different browser. Start again."
            )
        session = request.cookies.get(COOKIE, "")
        verifier = await self.identities.consume_oauth(state, provider, session)
        if verifier is None or not request.query.get("code"):
            raise web.HTTPBadRequest(text="Sign-in expired or was cancelled. Start again.")
        payload = {
            "client_id": client_id,
            "client_secret": secret,
            "grant_type": "authorization_code",
            "code": request.query["code"],
            "redirect_uri": self.settings.origin + f"/auth/{provider}/callback",
        }
        if provider == "google":
            payload["code_verifier"] = verifier
        assert self.http is not None
        async with self.http.post(PROVIDERS[provider][1], data=payload) as result:
            if result.status != 200:
                raise web.HTTPBadGateway(text="Sign-in could not be completed. Please start again.")
            tokens = await result.json()
        async with self.http.get(
            PROVIDERS[provider][2], headers={"Authorization": "Bearer " + tokens["access_token"]}
        ) as result:
            if result.status != 200:
                raise web.HTTPBadGateway(text="Your identity could not be verified.")
            identity = await result.json()
        response = web.Response(status=web.HTTPFound.status_code, headers={"Location": "/"})
        response.del_cookie(STATE_COOKIE, path="/", secure=True, httponly=True, samesite="Lax")
        if provider == "google":
            if not identity.get("sub") or identity.get("email_verified") is not True:
                raise web.HTTPForbidden(text="A verified Google account is required.")
            token = await self.identities.login(
                identity["sub"], identity.get("name", "Player"), identity.get("email", "")
            )
            await self.identities.logout(session)
            response.set_cookie(
                COOKIE,
                token,
                secure=True,
                httponly=True,
                samesite="Lax",
                max_age=SESSION_SECONDS,
                path="/",
            )
        else:
            user = await self.current_user(request)
            discord_id = str(identity.get("id", ""))
            if not discord_id.isdigit():
                raise web.HTTPBadGateway(text="Discord identity was invalid.")
            await self.identities.link_discord(user["id"], discord_id)
        return response

    async def logout(self, request: web.Request) -> web.Response:
        await self.identities.logout(request.cookies.get(COOKIE, ""))
        response = web.json_response({"ok": True})
        response.del_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="Lax")
        return response

    async def unlink(self, request: web.Request) -> web.Response:
        user = await self.current_user(request)
        await self.identities.unlink_discord(user["id"])
        return web.json_response({"ok": True})

    async def delete_account(self, request: web.Request) -> web.Response:
        user = await self.current_user(request)
        if (await request.json()).get("confirm") != "DELETE":
            raise ValueError("Type DELETE to confirm account deletion.")
        await self.bot.store.delete_guild(guild_id=-user["id"])
        await self.identities.delete_user(user["id"])
        return await self.logout(request)

    async def personal(self, request: web.Request) -> web.Response:
        user = await self.current_user(request)
        return web.json_response(await self.dashboard.data(user, request.query))

    async def session_settings(self, request: web.Request) -> web.Response:
        from slopbot.sessions import Sessions

        user = await self.current_user(request)
        data = await request.json()
        if not isinstance(data, dict) or set(data) != {
            "source",
            "cutoff",
            "enabled",
            "minimum",
            "include_observations",
        }:
            raise ValueError("Invalid session settings.")
        sources = await self.dashboard.sources(user)
        source = next(
            (
                s
                for s in sources
                if s.key == data["source"]
                and (
                    s.account.guild_id < 0
                    or s.account.discord_user_id == int(user.get("discord_id") or 0)
                )
            ),
            None,
        )
        if source is None:
            raise PermissionError("You can only edit your own session settings.")
        if source.account.guild_id < 0 and data["enabled"]:
            raise ValueError(
                "Link Discord and select its server history to receive private summaries."
            )
        await Sessions(self.bot.store).save(
            source.account.guild_id,
            source.account.discord_user_id,
            cutoff=data["cutoff"],
            enabled=data["enabled"],
            minimum=data["minimum"],
            include_observations=data["include_observations"],
        )
        return web.json_response({"ok": True})

    async def leaderboards(self, request: web.Request) -> web.Response:
        user = await self.current_user(request)
        return web.json_response(await self.dashboard.board(user, request.query))

    async def activity(self, request: web.Request) -> web.Response:
        user = await self.current_user(request)
        return web.json_response(await self.dashboard.activity(user, request.query))

    async def preferences(self, request: web.Request) -> web.Response:
        user = await self.current_user(request)
        data = await request.json()
        if not isinstance(data, dict) or set(data) - {
            "widgets",
            "player",
            "source",
            "playlist",
            "days",
            "compare",
            "server_widgets",
            "layout",
            "card_sizes",
            "theme_preset",
            "theme_mode",
            "theme_radius",
            "theme_density",
            "report_view",
            "report_presets",
            "history_sections",
            "timezone",
        }:
            raise ValueError("Invalid dashboard preferences.")
        if "timezone" in data:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            zone = data["timezone"]
            if not isinstance(zone, str) or not 1 <= len(zone) <= 100:
                raise ValueError("Choose an IANA timezone.")
            try:
                ZoneInfo(zone)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError("Choose a valid IANA timezone.") from exc
        if "report_presets" in data:
            from slopbot.summaries import SECTIONS

            presets = data["report_presets"]
            if not isinstance(presets, dict) or len(presets) > 10:
                raise ValueError("Save up to ten content presets.")
            for name, keys in presets.items():
                if (
                    not name.strip()
                    or len(name) > 40
                    or not isinstance(keys, list)
                    or not keys
                    or len(keys) > len(SECTIONS)
                    or any(not isinstance(k, str) or k not in SECTIONS for k in keys)
                    or len(set(keys)) != len(keys)
                ):
                    raise ValueError("Choose a name and supported sections for each preset.")
        if "report_view" in data:
            from slopbot.summaries import defaults, validate

            view = data["report_view"]
            if not isinstance(view, dict) or set(view) - {
                "guild_id",
                "cadence",
                "scope",
                "offset",
                "timezone",
                "weekday",
                "sections",
                "playlists",
                "minimum_streak",
                "minimum_peak_gain",
                "player_order",
            }:
                raise ValueError("Invalid report viewing preferences.")
            p = defaults()
            p.update({k: v for k, v in view.items() if k in p})
            validate(view.get("cadence", "daily"), view.get("scope", "self"), p, user["id"])
            if "guild_id" in view:
                await self.reports.context(user, view["guild_id"])
            if type(view.get("offset", 1)) is not int or not 0 <= view.get("offset", 1) <= (
                30 if view.get("cadence", "daily") == "daily" else 4
            ):
                raise ValueError("Invalid report period.")
        if "history_sections" in data:
            from slopbot.summaries import EVENT_TYPES

            values = data["history_sections"]
            if (
                not isinstance(values, list)
                or len(values) > 8
                or any(not isinstance(v, str) or v not in {"match", *EVENT_TYPES} for v in values)
            ):
                raise ValueError("Invalid history sections.")
        if "widgets" in data:
            widgets = data["widgets"]
            if (
                not isinstance(widgets, list)
                or not all(isinstance(w, str) for w in widgets)
                or len(widgets) != len(set(widgets))
                or set(widgets) - set(WIDGETS)
            ):
                raise ValueError("Choose supported dashboard widgets.")
        if "playlist" in data and (
            type(data["playlist"]) is not int or data["playlist"] not in PLAYLIST_NAMES
        ):
            raise ValueError("Invalid playlist.")
        if "days" in data and (type(data["days"]) is not int or data["days"] not in {1, 7, 14, 30}):
            raise ValueError("Invalid time range.")
        if "compare" in data and type(data["compare"]) is not bool:
            raise ValueError("Invalid chart preference.")
        if "theme_preset" in data and (
            not isinstance(data["theme_preset"], str) or data["theme_preset"] not in THEME_PRESETS
        ):
            raise ValueError("Invalid theme preset.")
        if "theme_mode" in data and (
            not isinstance(data["theme_mode"], str) or data["theme_mode"] not in THEME_MODES
        ):
            raise ValueError("Invalid theme mode.")
        if "theme_radius" in data and (
            not isinstance(data["theme_radius"], str)
            or data["theme_radius"] not in {"square", "balanced", "soft"}
        ):
            raise ValueError("Invalid corner style.")
        if "theme_density" in data and (
            not isinstance(data["theme_density"], str)
            or data["theme_density"] not in {"comfortable", "compact"}
        ):
            raise ValueError("Invalid spacing density.")
        layout_keys = set(WIDGETS) | {f"server-{n}" for n in range(8)}
        if "layout" in data:
            layout = data["layout"]
            if (
                not isinstance(layout, list)
                or not all(isinstance(k, str) for k in layout)
                or len(layout) != len(set(layout))
                or set(layout) - layout_keys
            ):
                raise ValueError("Invalid dashboard layout.")
        if "card_sizes" in data:
            sizes = data["card_sizes"]
            if (
                not isinstance(sizes, dict)
                or set(sizes) - layout_keys
                or any(type(v) is not int or v not in {1, 2} for v in sizes.values())
            ):
                raise ValueError("Invalid card sizes.")
        if "server_widgets" in data:
            cards = data["server_widgets"]
            if not isinstance(cards, list) or len(cards) > 8:
                raise ValueError("Choose up to eight server cards.")
            for card in cards:
                if (
                    not isinstance(card, dict)
                    or set(card) != {"kind", "guild_id", "playlist", "days"}
                    or not isinstance(card["kind"], str)
                    or card["kind"]
                    not in {"ranks", "activity", "server_activity", "movers", "streaks"}
                    or not isinstance(card["guild_id"], str)
                    or not card["guild_id"].isdigit()
                    or type(card["playlist"]) is not int
                    or card["playlist"] not in PLAYLIST_NAMES
                    or type(card["days"]) is not int
                    or card["days"] not in {1, 7, 14, 30}
                ):
                    raise ValueError("Invalid server card settings.")
            # Validate each distinct server once. Saved settings never grant access.
            for guild_id in {c["guild_id"] for c in cards}:
                await self.context(user, int(guild_id))
        for key in ("player", "source"):
            if key in data and (not isinstance(data[key], str) or len(data[key]) > 180):
                raise ValueError("Invalid profile preference.")
        await self.identities.save_preferences(user["id"], data)
        return web.json_response({"ok": True})

    async def link_player(self, request: web.Request) -> web.Response:
        user = await self.current_user(request)
        self.throttle(f"link:{user['id']}", 2)
        payload = await request.json()
        platform = payload.get("platform")
        account_id = payload.get("account_id", "")
        if (
            platform not in PLATFORMS
            or not isinstance(account_id, str)
            or not PLAYER_ACCOUNT_ID_PATTERN.fullmatch(account_id)
        ):
            raise ValueError("Select a platform and enter its raw account ID.")
        player_id = f"{PLATFORMS[platform][1]}|{account_id}|0"
        known = await self.dashboard.sources(user, desired=player_id)
        existing = next((s for s in known if s.account.player_id == player_id), None)
        if existing is not None:
            await self.identities.save_preferences(user["id"], {"player": player_id, "source": ""})
            return web.json_response(
                {"ok": True, "reused": True, "name": existing.account.player_name}
            )
        personal = await self.bot.store.get_rocket_league_account(
            guild_id=-user["id"], discord_user_id=user["id"]
        )
        if not self.bot.rocket_league:
            raise web.HTTPServiceUnavailable(text="Rocket League is unavailable.")
        if (
            personal is not None
            and personal.player_id != player_id
            and payload.get("replace") is not True
        ):
            raise ValueError(
                "You already track a personal profile. Confirm replacement in Account first."
            )
        profiles = await self.bot.rocket_league.profiles([player_id])
        profile = next(
            (p for p in profiles if p.player_id == player_id and p.player_name.strip()), None
        )
        if profile is None:
            raise ValueError("Account not found. Use the account ID, not its display name.")
        uid = user["id"]
        await self.bot.store.save_rocket_league_account(
            guild_id=-uid,
            discord_user_id=uid,
            player_id=player_id,
            player_name=profile.player_name[:100],
        )
        await self.identities.save_preferences(user["id"], {"player": player_id, "source": ""})
        return web.json_response({"ok": True, "reused": False, "name": profile.player_name})

    async def unlink_player(self, request: web.Request) -> web.Response:
        user = await self.current_user(request)
        await self.bot.store.unlink_rocket_league_account(
            guild_id=-user["id"], discord_user_id=user["id"]
        )
        return web.json_response({"ok": True})

    async def shop(self, request: web.Request) -> web.Response:
        await self.current_user(request)
        items = await self.bot.store.list_item_shop_snapshot()
        changes = await self.bot.store.list_item_shop_changes(since=int(time.time()) - 30 * 86400)
        return web.json_response(
            {"items": [asdict(i) for i in items], "changes": [asdict(c) for c in changes]}
        )

    async def tournaments(self, request: web.Request) -> web.Response:
        await self.current_user(request)
        region = request.query.get("region", "USE")
        if region not in {choice.value for choice in REGION_CHOICES}:
            raise ValueError("Choose a supported region.")
        if not self.bot.rocket_league:
            raise web.HTTPServiceUnavailable(text="Tournament schedule is unavailable.")
        cog = self.bot.get_cog("TournamentAlertsCog")
        if cog is None:
            raise web.HTTPServiceUnavailable(text="Tournament schedule is unavailable.")
        schedule = await cog._get_schedule(region)
        return web.json_response(
            {
                "tournaments": [asdict(t) for t in schedule.tournaments],
                "regions": [{"value": c.value, "label": c.name} for c in REGION_CHOICES],
            }
        )

    async def context(
        self,
        user: dict[str, Any],
        guild_id: int,
        channel_id: int | None = None,
        *,
        allow_cached: bool = False,
    ) -> tuple[Any, Any, Any]:
        if not self.bot.is_ready():
            raise web.HTTPServiceUnavailable(
                text="Discord is reconnecting. Please try again shortly."
            )
        if not user.get("discord_id"):
            raise PermissionError("Link Discord to access server features.")
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise PermissionError("Slopbot is not in that server.")
        key = (guild.id, int(user["discord_id"]))
        cached_member = guild.get_member(key[1]) if allow_cached else None
        pending = self.member_reads.get(key)
        if pending is None:
            pending = asyncio.create_task(guild.fetch_member(key[1]))
            self.member_reads[key] = pending

            def completed(task: asyncio.Task[Any]) -> None:
                self.member_reads.pop(key, None)
                if not task.cancelled():
                    task.exception()

            pending.add_done_callback(completed)
        try:
            if allow_cached:
                member = await asyncio.wait_for(
                    asyncio.shield(pending), timeout=MEMBERSHIP_READ_TIMEOUT_SECONDS
                )
            else:
                member = await asyncio.shield(pending)
        except TimeoutError:
            if cached_member is None:
                raise web.HTTPServiceUnavailable(
                    text="Discord membership verification is taking too long. Please try again."
                ) from None
            LOGGER.warning(
                "Discord membership verification exceeded %.1fs; "
                "using gateway cache for read: %s/%s",
                MEMBERSHIP_READ_TIMEOUT_SECONDS,
                guild.id,
                key[1],
            )
            member = cached_member
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel_id and (channel is None or not channel.permissions_for(member).view_channel):
            raise PermissionError("That channel is not accessible.")
        return guild, member, channel

    async def guilds(self, request: web.Request) -> web.Response:
        user = await self.current_user(request)
        if not user.get("discord_id"):
            return web.json_response({"guilds": []})

        async def membership(guild: Any) -> dict[str, Any] | None:
            try:
                _, member, _ = await self.context(user, guild.id, allow_cached=True)
            except (discord.NotFound, discord.Forbidden, PermissionError):
                return None
            return {
                "id": str(guild.id),
                "name": guild.name,
                "admin": member.guild_permissions.manage_guild,
            }

        rows = await asyncio.gather(*(membership(guild) for guild in self.bot.guilds))
        return web.json_response({"guilds": [row for row in rows if row is not None]})

    def commands(self) -> dict[str, Any]:
        return {
            c.qualified_name: c
            for c in self.bot.tree.walk_commands()
            if isinstance(c, app_commands.Command)
        }

    async def guild(self, request: web.Request) -> web.Response:
        user = await self.current_user(request)
        guild, member, _ = await self.context(
            user, int(request.match_info["guild_id"]), allow_cached=True
        )
        catalog = []
        for command in self.commands().values():
            try:
                require_command_permissions(command, member)
            except PermissionError:
                continue
            catalog.append(command_schema(command))
        accounts = await self.bot.store.list_all_rocket_league_accounts(guild_id=guild.id)
        return web.json_response(
            {
                "id": str(guild.id),
                "name": guild.name,
                "commands": catalog,
                "channels": [
                    {"id": str(c.id), "name": c.name, "type": c.type.value}
                    for c in visible_channels(guild, member)
                ],
                "members": [
                    {"id": str(m.id), "name": m.display_name} for m in guild.members if not m.bot
                ],
                "accounts": [
                    {
                        "label": a.player_name,
                        "value": f"epic:{-a.discord_user_id}"
                        if a.unlinked
                        else f"member:{a.discord_user_id}",
                    }
                    for a in accounts
                    if a.unlinked or guild.get_member(a.discord_user_id) is not None
                ],
            }
        )

    def prune_flows(self) -> None:
        self.flows = {k: v for k, v in self.flows.items() if v.expires > time.monotonic()}
        owners = {f.owner for f in self.flows.values()}
        self.locks = {k: v for k, v in self.locks.items() if k in owners or v.locked()}

    async def command(self, request: web.Request) -> web.Response:
        user = await self.current_user(request)
        payload = await request.json()
        command = self.commands().get(payload.get("command"))
        if command is None:
            raise ValueError("Unknown feature.")
        guild, member, channel = await self.context(
            user, int(payload["guild_id"]), int(payload["channel_id"])
        )
        if channel is None:
            raise ValueError("Select a channel context.")
        owner = digest(request.cookies[COOKIE])
        self.prune_flows()
        if len(self.flows) >= 1000 or sum(f.owner == owner for f in self.flows.values()) >= 15:
            raise ValueError("Too many open forms. Wait five minutes before opening another.")
        flow = WebFlow(command, guild.id, channel.id, owner)
        interaction = WebInteraction(self.bot, member, channel, flow)
        values = payload.get("values", {})
        if not isinstance(values, dict):
            raise ValueError("Invalid form.")
        async with self.locks.setdefault(owner, asyncio.Lock()):
            await invoke_command(interaction, values)
            self.flows[flow.id] = flow
            return web.json_response(flow.render(interaction))

    async def component(self, request: web.Request) -> web.Response:
        user = await self.current_user(request)
        payload = await request.json()
        self.prune_flows()
        flow = self.flows.get(payload.get("id"))
        owner = digest(request.cookies[COOKIE])
        if flow is None or flow.owner != owner:
            raise web.HTTPNotFound(text="This form expired. Open the feature again.")
        _, member, channel = await self.context(user, flow.guild_id, flow.channel_id)
        interaction = WebInteraction(self.bot, member, channel, flow)
        async with self.locks.setdefault(owner, asyncio.Lock()):
            await invoke_component(interaction, payload)
            return web.json_response(flow.render(interaction))
