from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path

from tmuxctl import storage, tmux_api
from tmuxctl.models import Job
from tmuxctl.utils import to_timestamp, utcnow


def run_job(conn, job: Job) -> tuple[bool, str | None]:
    sent_at = utcnow()
    next_run_at = to_timestamp(sent_at + timedelta(seconds=job.interval_seconds))
    error_text = None
    status = "success"

    try:
        tmux_api.send_keys(
            job.session_name,
            job.message,
            press_enter=job.send_enter,
            enter_delay_ms=job.enter_delay_ms,
        )
    except Exception as exc:
        status = "failed"
        error_text = str(exc)

    storage.insert_log(
        conn,
        job_id=job.id,
        session_name=job.session_name,
        message=job.message,
        status=status,
        error_text=error_text,
    )
    storage.update_job(
        conn,
        job.id,
        last_run_at=to_timestamp(sent_at),
        next_run_at=next_run_at,
    )
    return status == "success", error_text


def run_once(*, db_path: Path | None = None) -> int:
    conn = storage.get_connection(db_path)
    due_jobs = storage.get_due_jobs(conn)
    for job in due_jobs:
        run_job(conn, job)
    return len(due_jobs)


def run_daemon(*, poll_interval: int = 3, db_path: Path | None = None) -> None:
    conn = storage.get_connection(db_path)
    while True:
        for job in storage.get_due_jobs(conn):
            run_job(conn, job)
        time.sleep(poll_interval)
