# Deploy Slopbot 1.8.1

This update restores compact leaderboard announcements, improves response handling and charts,
and puts long activity results in a single paginated message. Existing settings, links, history,
session streaks, and permanent streaks are retained. No new environment variables, Discord
permissions, or setup commands are required.

These steps assume the README installation paths and an existing 1.8.0 deployment. If your
`DATABASE_PATH` differs, change the backup script's source path first. The Go service has no code
changes in this release and can keep running. For an older installation, use the full update
procedure in README, including rebuilding the Go service.

## Update

Run on the Raspberry Pi. `set -e` stops the block if any step fails.

```bash
set -e
sudo -u slopbot git -C /opt/slopbot switch main
sudo -u slopbot git -C /opt/slopbot pull --ff-only origin main
sudo -u slopbot git -C /opt/slopbot rev-parse --short HEAD
sudo systemctl stop slopbot

sudo -u slopbot /opt/slopbot/.venv/bin/python - <<'PY'
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

source = Path('/var/lib/slopbot/slopbot.db')
stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
backup = source.with_name(f'slopbot-before-1.8.1-{stamp}.db')
with sqlite3.connect(f'{source.as_uri()}?mode=ro', uri=True) as src:
    with sqlite3.connect(backup) as dst:
        src.backup(dst)
backup.chmod(0o600)
print(f'Database backup: {backup}')
PY

sudo -u slopbot /opt/slopbot/.venv/bin/pip install /opt/slopbot
sudo systemctl start slopbot
sudo systemctl --no-pager status slopbot slopbot-rl
sudo journalctl -u slopbot -n 60 --no-pager
```

The database adds one nullable event-text column automatically at startup. No manual migration or
database replacement is needed. If installation fails after the stop, the bot remains stopped;
resolve the error or use the rollback below before restarting.

## Check in Discord

1. Run `/slopbotstatus`; check the database, recurring workers, and Rocket League connection.
2. Run `/leaderboard` and `/playeroverview`. Switch between the dashboard and its 2v2 chart.
3. Run `/today events:100`. If enough events exist, use Previous/Next; all pages stay in one message.
4. On the next actual MMR ordering change, expect one standings card for that playlist, including
   unchanged players. Ordinary MMR changes with the same order should remain quiet.

Existing Discord posts are not rewritten. Old pending movement notifications created before the
upgrade retain their original format; newly observed updates use compact cards. Existing
notification routes remain active. Inspect them with `/setup events notifications`.

## Roll back the application

The pre-audit main revision was `891fe12`. The additive database column is compatible with that
version; retain the live database so new activity and preferences are not lost.

```bash
set -e
sudo systemctl stop slopbot
sudo -u slopbot git -C /opt/slopbot switch --detach 891fe12
sudo -u slopbot /opt/slopbot/.venv/bin/pip install /opt/slopbot
sudo systemctl start slopbot
sudo systemctl --no-pager status slopbot
```

This leaves the checkout on the old revision. To return to the update, run the update block again;
it switches back to main. The older `/today` formatter may show a generic line for standings-summary
events recorded by 1.8.1. Restoring the database backup is unnecessary for an application rollback
and would discard data recorded since that backup.
