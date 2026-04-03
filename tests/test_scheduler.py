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
