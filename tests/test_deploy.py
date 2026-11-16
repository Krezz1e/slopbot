from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_services_survive_clean_exits_and_restart_bursts() -> None:
    for name in ("slopbot.service", "slopbot-rl.service"):
        unit = (ROOT / "deploy" / name).read_text(encoding="utf-8")
        assert "Restart=always" in unit
        assert "RestartSec=10" in unit
        assert "StartLimitIntervalSec=0" in unit
        assert "TimeoutStopSec=30" in unit
        assert "UMask=0077" in unit
        assert "network-online.target" in unit
        assert "time-sync.target" in unit


def test_rocket_league_updater_verifies_and_rolls_back_release() -> None:
    updater = (ROOT / "deploy" / "slopbot-rl-update").read_text(encoding="utf-8")
    assert 'git -C "$checkout" fetch' in updater
    assert 'git -C "$checkout" archive origin/main rlservice' in updater
    assert 'go -C "$stage/rlservice" build' in updater
    assert 'go -C "$stage/rlservice" test' not in updater
    assert "sha256sum" in updater
    assert "rolling back" in updater
    assert 'status" == "200" || "$status" == "428"' in updater
    cron = (ROOT / "deploy" / "slopbot-rl-update.cron").read_text(encoding="utf-8")
    assert "/opt/slopbot/deploy/slopbot-rl-update" in cron
    installer = (ROOT / "deploy" / "install-slopbot-rl-update").read_text(encoding="utf-8")
    assert "crontab -l" in installer
    assert "grep -Fqx" in installer
