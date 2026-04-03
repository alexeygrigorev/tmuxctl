from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Job:
    id: int
    session_name: str
    message: str
    message_file_path: str | None
    interval_seconds: int
    enabled: bool
    send_enter: bool
    enter_delay_ms: int
    created_at: str
    updated_at: str
    last_run_at: str | None
    next_run_at: str


@dataclass(slots=True)
class LogEntry:
    id: int
    job_id: int | None
    session_name: str
    message: str
    trigger_type: str
    send_enter: bool
    enter_delay_ms: int
    status: str
    error_text: str | None
    created_at: str


@dataclass(slots=True)
class SessionInfo:
    name: str
    created_at: int
    activity_at: int
