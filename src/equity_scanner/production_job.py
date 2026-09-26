"""Host-side Compose launcher; expose only selected external notification settings.

Runs on the host with the system python3 (stdlib only). A failed job posts a short
alert to the scan's Discord webhook, since cron output otherwise lands only in a log.
"""
from __future__ import annotations

import collections
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

SERVICE_LOGS = {
    "scan": "reports/production-scan.log",
    "refresh-universes": "reports/production-refresh.log",
}


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


def post_failure_alert(webhook_url: str, content: str) -> None:
    """Best effort: an alert failure must not mask the job's own exit status."""
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps({"content": content[:1900]}).encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://github.com/hollowc2/equity-scanner, 0.2)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10):  # noqa: S310
            pass
    except Exception as exc:
        print(f"equity_scanner_failure_alert_failed error_type={type(exc).__name__}")
    else:
        print("equity_scanner_failure_alert_sent")


def _run_streaming(command: list[str], *, cwd: Path, env: dict[str, str]) -> tuple[int, str]:
    """Run the job, passing output through to the cron log, and keep its last line."""
    last_lines: collections.deque[str] = collections.deque(maxlen=1)
    with subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    ) as process:
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            if line.strip():
                last_lines.append(line.strip())
    return process.returncode, last_lines[0] if last_lines else ""


def main() -> None:
    root = Path(os.environ.get("EQUITY_SCANNER_ROOT", "/opt/equity-scanner"))
    service = os.environ.get("EQUITY_SCANNER_SERVICE", "scan")
    if service not in SERVICE_LOGS:
        raise SystemExit(f"Unknown equity-scanner service: {service}")
    env = dict(os.environ)
    notifications = notification_environment(Path(env.get(
        "EQUITY_SCANNER_NOTIFICATION_ENV", "/opt/butterflyguy/.env"
    )))
    command = ["docker", "compose", "-f", "compose.production.yml", "run", "--rm"]
    command += ["-e", "SCHWAB_GATEWAY_URL=" + env.get(
        "EQUITY_SCANNER_GATEWAY_URL", "http://127.0.0.1:8011"
    )]
    if service == "scan":
        # Only the scan container needs notification settings; the refresh job uses
        # the webhook solely from this host process to report its own failure.
        env.update(notifications)
        for name in ("EQUITY_SCANNER_DISCORD_WEBHOOK_URL", "SEC_USER_AGENT",
                     "ALPHA_VANTAGE_API_KEY"):
            if name in env:
                command += ["-e", name]
    command += [service, *sys.argv[1:]]
    returncode, last_line = _run_streaming(command, cwd=root, env=env)
    if returncode != 0:
        detail = f"\n`{last_line[:300]}`" if last_line else ""
        post_failure_alert(
            notifications["EQUITY_SCANNER_DISCORD_WEBHOOK_URL"],
            f"⚠️ equity-scanner `{service}` failed on {socket.gethostname()} "
            f"(exit {returncode}); see {root / SERVICE_LOGS[service]}{detail}",
        )
    raise SystemExit(returncode)


if __name__ == "__main__":
    main()
