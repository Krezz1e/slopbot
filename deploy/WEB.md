# Slopbot website deployment

## Configuration

Keep `/etc/slopbot.env` and its existing values. Create `/etc/slopbot-web.env`, owned by root with
mode 0600, containing:

```text
SLOPBOT_WEB_ORIGIN=https://bot.example.com
SLOPBOT_WEB_PORT=8080
GOOGLE_CLIENT_ID=your-google-web-client-id
GOOGLE_CLIENT_SECRET=your-google-client-secret
DISCORD_CLIENT_ID=your-existing-discord-application-id
DISCORD_CLIENT_SECRET=your-discord-oauth-client-secret
```

On first web-enabled startup, Slopbot automatically creates a stable P-256 VAPID private key beside
`DATABASE_PATH` with mode 0600. It derives the browser-safe public key at startup. To reuse an
existing VAPID identity instead, set `SLOPBOT_VAPID_PRIVATE_KEY`, `SLOPBOT_VAPID_PUBLIC_KEY`, and
optionally `SLOPBOT_VAPID_SUBJECT` in the web environment. Keep the pair stable or browsers will
need to subscribe again.

Google uses an External OAuth web application, with `openid email profile` scopes, JavaScript
origin `https://bot.example.com`, and redirect
`https://bot.example.com/auth/google/callback`. Change Google Auth Platform > Audience to
**In production** to allow friends outside the test-user list. Branding links:

- Home: `https://bot.example.com`
- Privacy: `https://bot.example.com/privacy`
- Terms: `https://bot.example.com/terms`

The existing Discord application needs the OAuth redirect
`https://bot.example.com/auth/discord/callback`. Website linking requests only `identify`;
server membership and permissions are checked through the existing bot. Do not replace the bot token.

Create `/etc/systemd/system/slopbot.service.d/web.conf`:

```ini
[Service]
EnvironmentFile=-/etc/slopbot-web.env
```

Run `sudo systemctl daemon-reload`. Configure the existing Cloudflare tunnel's published application
route with hostname `bot.example.com`, type HTTP, URL `127.0.0.1:8080`. No public inbound port
or origin TLS certificate is required. Do not route to the Rocket League service on port 8765.

## Deploy and validate

Fetch origin and check clean status before integration. Back up `/var/lib/slopbot/slopbot.db`
using SQLite's backup API while the bot is running, and back up `slopbot.web.db` if it exists.
Record the prior Git commit. Install the tested checkout using the service user's Python:

```bash
sudo -u slopbot /opt/slopbot/.venv/bin/pip install /opt/slopbot
sudo systemctl restart slopbot.service
curl --fail http://127.0.0.1:8080/healthz
curl --fail https://bot.example.com/healthz
```

Check `systemctl is-active slopbot slopbot-rl cloudflared`, the cloudflared metrics `/ready` endpoint,
the loopback health endpoint, the public health endpoint, and service logs. Confirm Google sign-in,
Discord linking, a read-only server result, a permitted settings update, a forbidden admin request,
and personal tracking. Do not create test notifications in real servers.

Run `ruff check .`, `ruff format --check .`, `mypy src`, `pytest`,
`go -C rlservice test ./...`, and `go -C rlservice vet ./...` before deployment. Also run
`node --check src/slopbot/web/static/app.js`,
`node --check src/slopbot/web/static/controls.js`, and
`node --check src/slopbot/web/static/service-worker.js` when changing browser code. Verify overview,
history, native leaderboards, server-card customization, arrangement persistence, and account linking
in either order. Dashboard preferences use an additive table in the existing web database; no
tracking-history migration or account duplication is required.

## Rollback

Record the previous commit before updating. To roll back the code, preserve any new work, check out
that recorded commit, reinstall it with the command above, then restart Slopbot. The web tables and
personal negative-guild rows are additive and do not require restoring the bot database. Avoid
restoring a database backup unless required: restoration would overwrite activity since that backup.
Disable the optional listener by removing `SLOPBOT_WEB_ORIGIN` from the web environment file and
restarting. Keep Discord and Rocket League environment values unchanged.

Credentials must never be committed or printed. Remove temporary deployment sudo permissions after
deployment if they are no longer needed. The website does not store Google or Discord OAuth access
tokens and never uses an email address to merge identities.
