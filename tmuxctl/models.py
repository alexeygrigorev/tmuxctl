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
class SessionEvent:
    id: int
    session_name: str
    event: str
    start_dir: str | None
    mem: str | None
    swap: str | None
    high: str | None
    scope_unit: str | None
    socket_path: str | None
    server_pid: int | None
    detail: str | None
    created_at: str


@dataclass(slots=True)
class SessionInfo:
    name: str
    created_at: int
    activity_at: int


@dataclass(slots=True)
class PaneInfo:
    window_index: int
    pane_index: int
    pid: int
    command: str
    cwd: str
    active: bool

    @property
    def label(self) -> str:
        return f"{self.window_index}.{self.pane_index}"
