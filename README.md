> [!IMPORTANT]
> **AI use disclosure:** Slopbot was built with extensive generative-AI assistance, including its
> code, tests, and documentation. Review the code and security model yourself before deploying it.

# Slopbot

Slopbot is a self-hosted Discord bot with an optional web dashboard. It sends configurable voice
and keyword alerts, schedules reminders, and tracks Rocket League activity for a private community.

## Features

- Voice-channel threshold alerts and keyword notifications
- Scheduled get-on requests with Discord and optional Telegram delivery
- Per-server notification downtime with timezone-aware daily schedules
- Rocket League ranks, peaks, leaderboards, streaks, match observations, sessions, and MMR charts
- Tournament reminders and item-shop watches
- Daily and weekly personal or server reports
- Google sign-in, Discord account linking, native dashboards, and server administration on the web
- Per-server permissions, private responses, delivery preferences, and configurable visibility

Rocket League data comes from an optional bundled Go service using the unofficial
[`dank/rlapi`](https://github.com/dank/rlapi) client. Its availability can change when Rocket
League changes its private APIs.

## Requirements

- Python 3.11 or newer
- A Discord application and bot token
- Go 1.24.5 or newer for the optional Rocket League service
- Google and Discord OAuth credentials for the optional website
- A Telegram bot token only if Telegram delivery is enabled

## Quick start

```bash
git clone <repository-url> slopbot
cd slopbot
python -m venv .venv
# macOS/Linux: source .venv/bin/activate
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Copy `.env.example` to `.env` and replace the placeholders. Export those settings into the process
environment, then start the bot:

```bash
slopbot
```

Slopbot does not need the Rocket League service or website for its core Discord notification
features. Leave their optional settings empty when they are not in use.

## Discord setup

Create an application in the [Discord Developer Portal](https://discord.com/developers/applications),
add a bot, and install it with the `bot` and `applications.commands` scopes. Grant only the channel
permissions it needs.

Enable these privileged intents on the bot page:

- **Presence Intent** for detected games and offline-only keyword alerts
- **Server Members Intent** for membership-aware results
- **Message Content Intent** for keyword matching

Use `DISCORD_GUILD_ID` during development for immediate command registration in one test server.
Leave it unset in production so commands register globally.

## Optional Rocket League service

Build the service and keep it on loopback. Generate a random bearer token of at least 32 characters
and give the Python and Go processes the same value.

```bash
mkdir -p bin
go -C rlservice build -o ../bin/slopbot-rl ./cmd/slopbot-rl
```

Configure:

```text
ROCKET_LEAGUE_SERVICE_URL=http://127.0.0.1:8765
ROCKET_LEAGUE_SERVICE_TOKEN=<random-secret>
RLAPI_LISTEN=127.0.0.1:8765
RLAPI_REFRESH_TOKEN_FILE=<private-token-file>
```

Authorize a dedicated, low-value Epic account with `slopbot-rl auth`. Do not expose port `8765`
or use another person's credentials.

## Optional website

Set `SLOPBOT_WEB_ORIGIN` to enable the web listener. It binds to `127.0.0.1` and is intended to sit
behind a trusted HTTPS reverse proxy or tunnel. Google handles sign-in; Discord linking unlocks
existing server data and permissions. OAuth credentials, session data, and the web database must
remain private.

The complete OAuth and service setup is in [deploy/WEB.md](deploy/WEB.md).

## Production deployment

Example hardened systemd units are available in `deploy/`. Keep configuration outside the checkout,
run the services as an unprivileged account, restrict secret files to that account, and store the
SQLite database outside the repository. Back up the database before upgrades.

## Development checks

Run the Python, web, browser, and Go checks before shipping:

```bash
ruff check .
ruff format --check .
mypy src
pytest
node tests/web_ui_smoke.cjs
node tests/reports_ui_smoke.cjs
go -C rlservice test ./...
go -C rlservice vet ./...
```

The browser smoke tests require Playwright and a compatible browser. Every Discord feature must
have equivalent authorized web support and regression coverage.

## Security and privacy

- Never commit `.env`, bot tokens, OAuth secrets, Epic refresh tokens, databases, or backups.
- Keep the Rocket League service and web origin listener on loopback.
- Use a dedicated Epic account and a unique random service token.
- Treat copied report links and exported data as private community data.
- Review access controls and dependencies before exposing an installation to the internet.

## License

Slopbot is available under the [MIT License](LICENSE). Bundled font licensing and dependency notices
are listed separately in the repository.
