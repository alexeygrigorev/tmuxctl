from __future__ import annotations

import sqlite3

from tmuxctl import storage


def test_init_db_adds_enter_delay_column() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY,
            session_name TEXT NOT NULL,
            message TEXT NOT NULL,
            interval_seconds INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            send_enter INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_run_at TEXT NULL,
            next_run_at TEXT NOT NULL
        );
        """
    )

    storage.init_db(conn)

    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
    }
    assert "enter_delay_ms" in columns


def test_create_job_persists_enter_delay() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    storage.init_db(conn)

    job = storage.create_job(
        conn,
        session_name="demo",
        message="hello",
        interval_seconds=60,
        enter_delay_ms=350,
    )

    assert job.enter_delay_ms == 350
    stored = storage.get_job(conn, job.id)
    assert stored is not None
    assert stored.enter_delay_ms == 350
