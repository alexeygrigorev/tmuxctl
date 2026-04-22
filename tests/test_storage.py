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


def test_init_db_adds_message_file_path_column() -> None:
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
            enter_delay_ms INTEGER NOT NULL DEFAULT 200,
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
    assert "message_file_path" in columns


def test_rename_session_jobs_updates_matching_jobs() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    storage.init_db(conn)

    first = storage.create_job(
        conn,
        session_name="old-name",
        message="hello",
        interval_seconds=60,
    )
    second = storage.create_job(
        conn,
        session_name="old-name",
        message="world",
        interval_seconds=120,
    )
    storage.create_job(
        conn,
        session_name="other-name",
        message="stay-put",
        interval_seconds=180,
    )

    renamed = storage.rename_session_jobs(
        conn,
        session_name="old-name",
        new_session_name="new-name",
    )

    assert renamed == 2
    assert storage.get_job(conn, first.id).session_name == "new-name"
    assert storage.get_job(conn, second.id).session_name == "new-name"
    assert len(storage.list_jobs(conn, session_name="new-name")) == 2
    assert len(storage.list_jobs(conn, session_name="old-name")) == 0
    assert len(storage.list_jobs(conn, session_name="other-name")) == 1
