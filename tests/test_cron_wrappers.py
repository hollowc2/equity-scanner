from __future__ import annotations

import fcntl
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parents[1]


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _run_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wrapper: str,
    *,
    weekday: str,
    local_time: str,
    docker_status: int = 0,
    hold_lock: bool = False,
) -> tuple[subprocess.CompletedProcess[str], str]:
    scanner_root = tmp_path / "scanner"
    scanner_root.mkdir()
    (scanner_root / ".candidate-image").write_text("candidate@sha256:abc\n")
    (scanner_root / "secrets").mkdir()
    (scanner_root / "secrets" / "gateway.env").write_text("placeholder=true\n")
    (scanner_root / "compose.candidate.yml").write_text("services: {}\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "docker.log"
    _write_executable(
        bin_dir / "date",
        "#!/bin/sh\n"
        f"case \"$*\" in *+%u*) echo {weekday};; *) echo {local_time};; esac\n",
    )
    _write_executable(
        bin_dir / "docker",
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" > \"$WRAPPER_DOCKER_LOG\"\n"
        "printf 'image=%s secret=%s\\n' \"$EQUITY_SCANNER_IMAGE\" "
        "\"$EQUITY_SCANNER_SECRET_ENV\" >> \"$WRAPPER_DOCKER_LOG\"\n"
        f"exit {docker_status}\n",
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "EQUITY_SCANNER_ROOT": str(scanner_root),
        "EQUITY_SCANNER_LOCK_FILE": str(tmp_path / "morning.lock"),
        "EQUITY_SCANNER_REFRESH_LOCK_FILE": str(tmp_path / "refresh.lock"),
        "WRAPPER_DOCKER_LOG": str(log_path),
    }
    monkeypatch.delenv("SCHWAB_GATEWAY_API_KEY", raising=False)
    lock_name = "refresh.lock" if "universe" in wrapper else "morning.lock"
    lock_handle = (tmp_path / lock_name).open("w") if hold_lock else None
    if lock_handle is not None:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = subprocess.run(
            [str(REPO / "tools" / wrapper)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        if lock_handle is not None:
            lock_handle.close()
    return result, log_path.read_text() if log_path.exists() else ""


def test_morning_wrapper_admits_weekday_at_six_and_uses_lock_and_dry_run_candidate(
    tmp_path, monkeypatch
):
    result, invocation = _run_wrapper(
        tmp_path,
        monkeypatch,
        "run_morning_scan_cron.sh",
        weekday="3",
        local_time="06:00",
    )

    assert result.returncode == 0
    assert "compose -f compose.candidate.yml run --rm" in invocation
    assert "scan" in invocation
    assert "candidate@sha256:abc" in invocation
    assert "gateway.env" in invocation


@pytest.mark.parametrize(
    ("weekday", "local_time"),
    (("7", "06:00"), ("3", "05:59")),
)
def test_morning_wrapper_rejects_weekends_and_wrong_local_time(
    tmp_path, monkeypatch, weekday, local_time
):
    result, invocation = _run_wrapper(
        tmp_path,
        monkeypatch,
        "run_morning_scan_cron.sh",
        weekday=weekday,
        local_time=local_time,
    )

    assert result.returncode == 0
    assert invocation == ""


def test_morning_wrapper_propagates_candidate_failure(tmp_path, monkeypatch):
    result, _ = _run_wrapper(
        tmp_path,
        monkeypatch,
        "run_morning_scan_cron.sh",
        weekday="3",
        local_time="06:00",
        docker_status=42,
    )

    assert result.returncode == 42


def test_morning_wrapper_refuses_overlapping_run(tmp_path, monkeypatch):
    result, invocation = _run_wrapper(
        tmp_path,
        monkeypatch,
        "run_morning_scan_cron.sh",
        weekday="3",
        local_time="06:00",
        hold_lock=True,
    )

    assert result.returncode != 0
    assert invocation == ""


def test_universe_refresh_wrapper_is_sunday_evening_and_dry_run_only(
    tmp_path, monkeypatch
):
    result, invocation = _run_wrapper(
        tmp_path,
        monkeypatch,
        "run_universe_refresh_cron.sh",
        weekday="7",
        local_time="20:00",
    )

    assert result.returncode == 0
    assert "refresh-universes" in invocation
    compose = (REPO / "compose.candidate.yml").read_text()
    assert 'command: ["--dry-run", "--scan-config"' in compose
    assert '"./data:/app/data:ro"' in compose
