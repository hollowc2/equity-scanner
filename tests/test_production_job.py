import pytest

from equity_scanner import production_job
from equity_scanner.production_job import notification_environment


def test_external_notifications_select_only_needed_settings(tmp_path):
    source = tmp_path / 'external.env'
    source.write_text(
        'SCHWAB_GATEWAY_API_KEY=excluded\n'
        'UNRELATED_SECRET=excluded\n'
        'EQUITY_DISCORD_WEBHOOK_URL="https://example.invalid/daily"\n'
        'SEC_USER_AGENT=Scanner ops@example.invalid\n'
        'ALPHA_VANTAGE_API_KEY=example # comment\n'
    )
    assert notification_environment(source) == {
        'EQUITY_SCANNER_DISCORD_WEBHOOK_URL': 'https://example.invalid/daily',
        'SEC_USER_AGENT': 'Scanner ops@example.invalid',
        'ALPHA_VANTAGE_API_KEY': 'example',
    }


class _FakeProcess:
    def __init__(self, returncode: int, output: str) -> None:
        self.returncode = returncode
        self.stdout = iter(output.splitlines(keepends=True))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _run_main(monkeypatch, tmp_path, *, service: str, returncode: int, output: str = ""):
    source = tmp_path / "external.env"
    source.write_text(
        "EQUITY_DISCORD_WEBHOOK_URL=https://example.invalid/hook\n"
        "SEC_USER_AGENT=Scanner ops@example.invalid\n"
    )
    commands: list[list[str]] = []
    alerts: list[tuple[str, str]] = []

    def fake_popen(command, **kwargs):
        commands.append(command)
        return _FakeProcess(returncode, output)

    monkeypatch.setattr(production_job.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        production_job, "post_failure_alert", lambda url, content: alerts.append((url, content))
    )
    monkeypatch.setattr(production_job.sys, "argv", ["production_job.py"])
    monkeypatch.setenv("EQUITY_SCANNER_ROOT", str(tmp_path))
    monkeypatch.setenv("EQUITY_SCANNER_NOTIFICATION_ENV", str(source))
    monkeypatch.setenv("EQUITY_SCANNER_SERVICE", service)
    monkeypatch.delenv("EQUITY_SCANNER_DISCORD_WEBHOOK_URL", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        production_job.main()
    return exit_info.value.code, commands, alerts


def test_failed_scan_posts_alert_with_last_output_line(monkeypatch, tmp_path):
    code, commands, alerts = _run_main(
        monkeypatch,
        tmp_path,
        service="scan",
        returncode=1,
        output="starting\nError response from daemon: No such image: sha256:abc\n\n",
    )

    assert code == 1
    assert commands[0][-1] == "scan"
    assert "EQUITY_SCANNER_DISCORD_WEBHOOK_URL" in commands[0]
    [(url, content)] = alerts
    assert url == "https://example.invalid/hook"
    assert "`scan` failed" in content
    assert "exit 1" in content
    assert "No such image: sha256:abc" in content
    assert "production-scan.log" in content


def test_successful_job_does_not_alert(monkeypatch, tmp_path):
    code, _, alerts = _run_main(monkeypatch, tmp_path, service="scan", returncode=0)

    assert code == 0
    assert alerts == []


def test_refresh_gets_no_notification_settings_but_alerts_on_failure(monkeypatch, tmp_path):
    code, commands, alerts = _run_main(
        monkeypatch, tmp_path, service="refresh-universes", returncode=3
    )

    assert code == 3
    assert commands[0][-1] == "refresh-universes"
    assert not any("DISCORD" in part or "SEC_USER_AGENT" in part for part in commands[0])
    [(_, content)] = alerts
    assert "`refresh-universes` failed" in content
    assert "production-refresh.log" in content
