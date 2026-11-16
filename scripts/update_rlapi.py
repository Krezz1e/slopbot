from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "rlservice"
PROTOCOL = SERVICE / "protocol-defaults.json"


def run(*args: str) -> str:
    return subprocess.check_output(args, cwd=SERVICE, text=True).strip()


def constant(source: str, name: str) -> str:
    match = re.search(rf'^\s*{re.escape(name)}\s*=\s*"([^"]+)"', source, re.MULTILINE)
    if match is None:
        raise RuntimeError(f"rlapi no longer exposes the expected {name} constant")
    return match.group(1)


def main() -> None:
    subprocess.run(["go", "get", "github.com/dank/rlapi@latest"], cwd=SERVICE, check=True)
    subprocess.run(["go", "mod", "tidy"], cwd=SERVICE, check=True)
    version, module_dir = run(
        "go", "list", "-m", "-f", "{{.Version}}|{{.Dir}}", "github.com/dank/rlapi"
    ).split("|", 1)
    source = (Path(module_dir) / "psynet.go").read_text(encoding="utf-8")
    payload = {
        "rlapi_version": version,
        "game_version": constant(source, "gameVersion"),
        "feature_set": constant(source, "featureSet"),
        "build_secret": constant(source, "buildSecret"),
    }
    PROTOCOL.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(version)


if __name__ == "__main__":
    main()
