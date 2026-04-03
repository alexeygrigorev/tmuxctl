from __future__ import annotations

import sqlite3
from pathlib import Path

from tmuxctl import scheduler, storage


def test_run_job_reads_latest_message_from_file(monkeypatch, tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    storage.init_db(conn)

    message_file = tmp_path / "prompt.txt"
    message_file.write_text("initial text\n", encoding="utf-8")
    job = storage.create_job(
        conn,
        session_name="rk-codex",
        message="initial text",
        message_file_path=str(message_file),
        interval_seconds=60,
    )

    message_file.write_text("updated text\n", encoding="utf-8")
    sent: dict[str, object] = {}

    def fake_send_keys(session_name: str, message: str, press_enter: bool, enter_delay_ms: int) -> None:
        sent["session_name"] = session_name
        sent["message"] = message
        sent["press_enter"] = press_enter
        sent["enter_delay_ms"] = enter_delay_ms

    monkeypatch.setattr("tmuxctl.scheduler.tmux_api.send_keys", fake_send_keys)

    ok, error = scheduler.run_job(conn, job)

    assert ok is True
    assert error is None
    assert sent["message"] == "updated text"
    log_entry = storage.list_logs(conn, limit=1)[0]
    assert log_entry.message == "updated text"


def test_run_job_removes_job_after_three_consecutive_failures(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    storage.init_db(conn)

    job = storage.create_job(
        conn,
        session_name="rk-codex",
        message="check status",
        interval_seconds=60,
    )

    def fail_send_keys(session_name: str, message: str, press_enter: bool, enter_delay_ms: int) -> None:
        raise RuntimeError(f"tmux session '{session_name}' was not found")

    monkeypatch.setattr("tmuxctl.scheduler.tmux_api.send_keys", fail_send_keys)

    for _ in range(3):
        ok, error = scheduler.run_job(conn, job)
        assert ok is False
        assert error == "tmux session 'rk-codex' was not found"

    assert storage.get_job(conn, job.id) is None
    logs = storage.list_logs(conn, limit=3)
    assert [entry.status for entry in logs] == ["failed", "failed", "failed"]


def test_run_job_keeps_job_when_failure_streak_is_broken(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    storage.init_db(conn)

    job = storage.create_job(
        conn,
        session_name="rk-codex",
        message="check status",
        interval_seconds=60,
    )

    outcomes = [RuntimeError("first failure"), None, RuntimeError("second failure"), RuntimeError("third failure")]

    def flaky_send_keys(session_name: str, message: str, press_enter: bool, enter_delay_ms: int) -> None:
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome

    monkeypatch.setattr("tmuxctl.scheduler.tmux_api.send_keys", flaky_send_keys)

    for _ in range(4):
        scheduler.run_job(conn, job)

    surviving_job = storage.get_job(conn, job.id)
    assert surviving_job is not None
    logs = storage.list_logs(conn, limit=4)
    assert [entry.status for entry in logs] == ["failed", "failed", "success", "failed"]
