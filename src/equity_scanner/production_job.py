"""Host-side Compose launcher; expose only selected external notification settings."""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path


def notification_environment(path: Path) -> dict[str, str]:
    names = {
        "EQUITY_DISCORD_WEBHOOK_URL": "EQUITY_SCANNER_DISCORD_WEBHOOK_URL",
        "SEC_USER_AGENT": "SEC_USER_AGENT",
        "ALPHA_VANTAGE_API_KEY": "ALPHA_VANTAGE_API_KEY",
    }
    result = {}
    for line in path.read_text().splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.*)$", line)
        if not match or match[1] not in names:
            continue
        value = match[2].strip()
        if value.startswith(("'", '"')):
            try:
                parts = shlex.split(value, comments=True)
            except ValueError:
                raise ValueError("Invalid quoted notification setting") from None
            if len(parts) != 1:
                raise ValueError("Invalid notification setting")
            value = parts[0]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        result[names[match[1]]] = value
    if not result.get("EQUITY_SCANNER_DISCORD_WEBHOOK_URL"):
        raise ValueError("External equity Discord destination is missing")
    return result


def main() -> None:
    root = Path(os.environ.get("EQUITY_SCANNER_ROOT", "/opt/equity-scanner"))
    env = dict(os.environ)
    env.update(notification_environment(Path(env.get(
        "EQUITY_SCANNER_NOTIFICATION_ENV", "/opt/butterflyguy/.env"
    ))))
    command = ["docker", "compose", "-f", "compose.production.yml", "run", "--rm"]
    command += ["-e", "SCHWAB_GATEWAY_URL=" + env.get(
        "EQUITY_SCANNER_GATEWAY_URL", "http://127.0.0.1:8011"
    )]
    for name in ("EQUITY_SCANNER_DISCORD_WEBHOOK_URL", "SEC_USER_AGENT", "ALPHA_VANTAGE_API_KEY"):
        if name in env:
            command += ["-e", name]
    command += ["scan", *sys.argv[1:]]
    raise SystemExit(subprocess.call(command, cwd=root, env=env))


if __name__ == "__main__":
    main()
