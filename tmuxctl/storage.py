from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

from tmuxctl.models import Job, LogEntry
from tmuxctl.utils import parse_timestamp, to_timestamp, utcnow


DEFAULT_DB_PATH = Path.home() / ".config" / "tmuxctl" / "tmuxctl.db"
_UNSET = object()


def get_default_db_path() -> Path:
    return DEFAULT_DB_PATH


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or get_default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY,
            session_name TEXT NOT NULL,
            message TEXT NOT NULL,
            message_file_path TEXT NULL,
            interval_seconds INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            send_enter INTEGER NOT NULL DEFAULT 1,
            enter_delay_ms INTEGER NOT NULL DEFAULT 200,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_run_at TEXT NULL,
            next_run_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY,
            job_id INTEGER NULL,
            session_name TEXT NOT NULL,
            message TEXT NOT NULL,
            trigger_type TEXT NOT NULL DEFAULT 'manual',
            send_enter INTEGER NOT NULL DEFAULT 1,
            enter_delay_ms INTEGER NOT NULL DEFAULT 200,
            status TEXT NOT NULL,
            error_text TEXT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
    }
    if "enter_delay_ms" not in columns:
        conn.execute(
            "ALTER TABLE jobs ADD COLUMN enter_delay_ms INTEGER NOT NULL DEFAULT 200"
        )
    if "message_file_path" not in columns:
        conn.execute(
            "ALTER TABLE jobs ADD COLUMN message_file_path TEXT NULL"
        )
    log_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(logs)").fetchall()
    }
    if "trigger_type" not in log_columns:
        conn.execute(
            "ALTER TABLE logs ADD COLUMN trigger_type TEXT NOT NULL DEFAULT 'manual'"
        )
    if "send_enter" not in log_columns:
        conn.execute(
            "ALTER TABLE logs ADD COLUMN send_enter INTEGER NOT NULL DEFAULT 1"
        )
    if "enter_delay_ms" not in log_columns:
        conn.execute(
            "ALTER TABLE logs ADD COLUMN enter_delay_ms INTEGER NOT NULL DEFAULT 200"
        )
    conn.commit()


def _job_from_row(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        session_name=row["session_name"],
        message=row["message"],
        message_file_path=row["message_file_path"],
        interval_seconds=row["interval_seconds"],
        enabled=bool(row["enabled"]),
        send_enter=bool(row["send_enter"]),
        enter_delay_ms=row["enter_delay_ms"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_run_at=row["last_run_at"],
        next_run_at=row["next_run_at"],
    )


def _log_from_row(row: sqlite3.Row) -> LogEntry:
    return LogEntry(
        id=row["id"],
        job_id=row["job_id"],
        session_name=row["session_name"],
        message=row["message"],
        trigger_type=row["trigger_type"],
        send_enter=bool(row["send_enter"]),
        enter_delay_ms=row["enter_delay_ms"],
        status=row["status"],
        error_text=row["error_text"],
        created_at=row["created_at"],
    )


def create_job(
    conn: sqlite3.Connection,
    *,
    session_name: str,
    message: str,
    message_file_path: str | None = None,
    interval_seconds: int,
    send_enter: bool = True,
    enter_delay_ms: int = 200,
    start_now: bool = False,
) -> Job:
    now = utcnow()
    next_run = now if start_now else now + timedelta(seconds=interval_seconds)
    timestamp = to_timestamp(now)
    cursor = conn.execute(
        """
        INSERT INTO jobs (
            session_name, message, message_file_path, interval_seconds, enabled,
            send_enter, enter_delay_ms, created_at, updated_at, last_run_at, next_run_at
        ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, NULL, ?)
        """,
        (
            session_name,
            message,
            message_file_path,
            interval_seconds,
            int(send_enter),
            enter_delay_ms,
            timestamp,
            timestamp,
            to_timestamp(next_run),
        ),
    )
    conn.commit()
    job = get_job(conn, cursor.lastrowid)
    if job is None:
        raise RuntimeError("failed to fetch newly created job")
    return job


def list_jobs(conn: sqlite3.Connection) -> list[Job]:
    rows = conn.execute("SELECT * FROM jobs ORDER BY id ASC").fetchall()
    return [_job_from_row(row) for row in rows]


def get_job(conn: sqlite3.Connection, job_id: int) -> Job | None:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _job_from_row(row) if row else None


def update_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    session_name: str | None = None,
    message: str | None = None,
    message_file_path: str | None | object = _UNSET,
    interval_seconds: int | None = None,
    enabled: bool | None = None,
    send_enter: bool | None = None,
    enter_delay_ms: int | None = None,
    last_run_at: str | None = None,
    next_run_at: str | None = None,
) -> Job | None:
    current = get_job(conn, job_id)
    if current is None:
        return None

    values = {
        "session_name": current.session_name if session_name is None else session_name,
        "message": current.message if message is None else message,
        "message_file_path": current.message_file_path if message_file_path is _UNSET else message_file_path,
        "interval_seconds": current.interval_seconds if interval_seconds is None else interval_seconds,
        "enabled": int(current.enabled if enabled is None else enabled),
        "send_enter": int(current.send_enter if send_enter is None else send_enter),
        "enter_delay_ms": current.enter_delay_ms if enter_delay_ms is None else enter_delay_ms,
        "last_run_at": current.last_run_at if last_run_at is None else last_run_at,
        "next_run_at": current.next_run_at if next_run_at is None else next_run_at,
        "updated_at": to_timestamp(utcnow()),
        "id": job_id,
    }
    conn.execute(
        """
        UPDATE jobs
        SET session_name = :session_name,
            message = :message,
            message_file_path = :message_file_path,
            interval_seconds = :interval_seconds,
            enabled = :enabled,
            send_enter = :send_enter,
            enter_delay_ms = :enter_delay_ms,
            updated_at = :updated_at,
            last_run_at = :last_run_at,
            next_run_at = :next_run_at
        WHERE id = :id
        """,
        values,
    )
    conn.commit()
    return get_job(conn, job_id)


def set_job_enabled(conn: sqlite3.Connection, job_id: int, enabled: bool) -> Job | None:
    next_run_at = None
    if enabled:
        current = get_job(conn, job_id)
        if current is None:
            return None
        next_run_at = to_timestamp(utcnow() + timedelta(seconds=current.interval_seconds))
    return update_job(conn, job_id, enabled=enabled, next_run_at=next_run_at)


def delete_job(conn: sqlite3.Connection, job_id: int) -> bool:
    cursor = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    conn.commit()
    return cursor.rowcount > 0


def get_due_jobs(conn: sqlite3.Connection, now: str | None = None) -> list[Job]:
    current = now or to_timestamp(utcnow())
    rows = conn.execute(
        """
        SELECT * FROM jobs
        WHERE enabled = 1 AND next_run_at <= ?
        ORDER BY next_run_at ASC, id ASC
        """,
        (current,),
    ).fetchall()
    return [_job_from_row(row) for row in rows]


def insert_log(
    conn: sqlite3.Connection,
    *,
    session_name: str,
    message: str,
    trigger_type: str,
    send_enter: bool,
    enter_delay_ms: int,
    status: str,
    job_id: int | None = None,
    error_text: str | None = None,
) -> LogEntry:
    created_at = to_timestamp(utcnow())
    cursor = conn.execute(
        """
        INSERT INTO logs (
            job_id, session_name, message, trigger_type, send_enter, enter_delay_ms,
            status, error_text, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            session_name,
            message,
            trigger_type,
            int(send_enter),
            enter_delay_ms,
            status,
            error_text,
            created_at,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM logs WHERE id = ?", (cursor.lastrowid,)).fetchone()
    if row is None:
        raise RuntimeError("failed to fetch newly inserted log")
    return _log_from_row(row)


def list_logs(conn: sqlite3.Connection, limit: int = 20) -> list[LogEntry]:
    rows = conn.execute(
        "SELECT * FROM logs ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_log_from_row(row) for row in rows]


def count_recent_consecutive_failures(conn: sqlite3.Connection, job_id: int) -> int:
    rows = conn.execute(
        """
        SELECT status
        FROM logs
        WHERE job_id = ?
        ORDER BY created_at DESC, id DESC
        """,
        (job_id,),
    ).fetchall()

    failures = 0
    for row in rows:
        if row["status"] != "failed":
            break
        failures += 1
    return failures


def compute_next_run(interval_seconds: int) -> str:
    return to_timestamp(utcnow() + timedelta(seconds=interval_seconds))


def validate_existing_schedule(job: Job) -> None:
    parse_timestamp(job.next_run_at)
    if job.last_run_at:
        parse_timestamp(job.last_run_at)
