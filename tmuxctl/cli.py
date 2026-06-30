from __future__ import annotations

import os
import re
import subprocess
import sys
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from click.shell_completion import CompletionItem
from typer.core import TyperGroup

from tmuxctl import robust, scheduler, storage, strays as strays_mod, tmux_api
from tmuxctl.models import Job, LogEntry, SessionInfo
from tmuxctl.utils import (
    display_timestamp,
    display_unix_timestamp,
    format_bytes,
    format_cpu_time,
    format_interval,
    parse_interval,
)

ROOT_COMMAND_NAMES = {
    "list",
    "l",
    "ls",
    "j",
    "recent",
    "r",
    "send",
    "attach",
    "create-or-attach",
    "create-detached",
    "kill",
    "k",
    "limit",
    "rename",
    "attach-last",
    "attach-recent",
    "jobs",
    "add",
    "pause",
    "pause-current-jobs",
    "resume",
    "resume-current-jobs",
    "remove",
    "edit",
    "logs",
    "daemon",
    "doctor",
    "describe",
    "info",
    "strays",
    "reap",
    "reap-clients",
}

PROGRAM_NAME = "tmuxctl"


class RootGroup(TyperGroup):
    def shell_complete(self, ctx, incomplete: str) -> list[CompletionItem]:
        items = list(super().shell_complete(ctx, incomplete))
        return _extend_root_completion(items, incomplete)


app = typer.Typer(
    cls=RootGroup,
    help="Control tmux sessions and recurring messages.",
    no_args_is_help=False,
)

jobs_app = typer.Typer(
    help="Manage recurring jobs.",
    no_args_is_help=False,
)


class SessionOrder(str, Enum):
    created = "created"
    activity = "activity"


def _conn() -> object:
    return storage.get_connection()


def _set_program_name(argv0: str) -> None:
    global PROGRAM_NAME
    PROGRAM_NAME = "t" if Path(argv0).name == "t" else "tmuxctl"


def _program_name() -> str:
    return PROGRAM_NAME


def _current_directory_session_name(
    *,
    cwd: Path | None = None,
    home: Path | None = None,
    suffix: str | None = None,
) -> str:
    current = (cwd or Path.cwd()).resolve()
    home_path = (home or Path.home()).resolve()

    if current == home_path:
        parts = ("home", current.name)
    else:
        try:
            relative = current.relative_to(home_path)
            parts = relative.parts or (current.name,)
        except ValueError:
            parts = tuple(part for part in current.parts if part not in {current.anchor, ""})

    if not parts:
        parts = tuple(part for part in current.parts if part not in {current.anchor, ""})

    normalized_parts = []
    for part in parts:
        normalized = _normalize_session_name_part(part)
        if normalized:
            normalized_parts.append(normalized)

    if not normalized_parts:
        _fail("unable to derive session name from current directory")

    session_name = "-".join(normalized_parts)
    if suffix is not None:
        normalized_suffix = _normalize_session_name_part(suffix)
        if not normalized_suffix:
            _fail("unable to derive session suffix")
        session_name = f"{session_name}-{normalized_suffix}"
    return session_name


def _normalize_session_name_part(value: str) -> str:
    normalized = re.sub(r"[.:]+", "_", value)
    return re.sub(r"[^A-Za-z0-9_-]+", "-", normalized).strip("-")


def _normalize_session_name(value: str) -> str:
    return _normalize_session_name_part(value)


def _fail(message: str, *, code: int = 1) -> None:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=code)


def _complete_session_names(incomplete: str) -> list[str]:
    try:
        sessions = tmux_api.list_sessions()
    except Exception:
        return []
    return [session for session in sessions if session.startswith(incomplete)]


def _extend_root_completion(
    items: list[CompletionItem],
    incomplete: str,
) -> list[CompletionItem]:
    seen = {item.value for item in items}

    if incomplete.startswith(":"):
        session_prefix = incomplete[1:]
        session_values = [f":{name}" for name in _complete_session_names(session_prefix)]
    else:
        session_values = _complete_session_names(incomplete)

    for value in session_values:
        if value in seen:
            continue
        items.append(CompletionItem(value))
        seen.add(value)

    return items


def _resolve_message(
    *,
    message: str | None,
    message_file: Path | None,
) -> str:
    if message is not None and message_file is not None:
        _fail("choose either --message or --message-file, not both")
    if message is None and message_file is None:
        _fail("one of --message or --message-file is required")
    if message_file is None:
        return message or ""

    try:
        file_message = message_file.read_text(encoding="utf-8")
    except OSError as exc:
        _fail(f"unable to read message file '{message_file}': {exc}")

    return file_message.rstrip("\r\n")


def _resolve_job_message_source(
    *,
    message: str | None,
    message_file: Path | None,
) -> tuple[str, str | None]:
    resolved_message = _resolve_message(message=message, message_file=message_file)
    if message_file is None:
        return resolved_message, None
    return resolved_message, str(message_file)


def _require_job(conn, job_id: int) -> Job:
    job = storage.get_job(conn, job_id)
    if job is None:
        _fail(f"job {job_id} was not found")
    return job


def _resolve_session_name(name: str) -> str:
    if name != ":current":
        normalized = _normalize_session_name(name)
        if not normalized:
            _fail("unable to derive session name")
        return normalized
    try:
        return tmux_api.current_session_name()
    except Exception as exc:
        _fail(str(exc))


def _require_session_jobs(conn, session_name: str) -> list[Job]:
    jobs = storage.list_jobs(conn, session_name=session_name)
    if not jobs:
        _fail(f"no jobs found for tmux session '{session_name}'")
    return jobs


def _print_jobs(jobs: list[Job]) -> None:
    typer.echo("ID  ENABLED  SESSION  EVERY  DELAY  SOURCE  NEXT RUN             DETAIL")
    for job in jobs:
        enabled = "yes" if job.enabled else "no"
        next_run = display_timestamp(job.next_run_at)
        source = "file" if job.message_file_path else "inline"
        detail_value = job.message_file_path or job.message
        detail = detail_value if len(detail_value) <= 40 else f"{detail_value[:37]}..."
        typer.echo(
            f"{job.id:<3} {enabled:<8} {job.session_name:<8} "
            f"{format_interval(job.interval_seconds):<6} {job.enter_delay_ms:<6} "
            f"{source:<7} {next_run:<20} {detail}"
        )


def _print_job_detail(job: Job) -> None:
    source = "file" if job.message_file_path else "inline"
    typer.echo(f"ID:       {job.id}")
    typer.echo(f"Session:  {job.session_name}")
    typer.echo(f"Enabled:  {'yes' if job.enabled else 'no'}")
    typer.echo(f"Every:    {format_interval(job.interval_seconds)}")
    typer.echo(f"Delay:    {job.enter_delay_ms}ms")
    typer.echo(f"Source:   {source}")
    if job.message_file_path:
        typer.echo(f"File:     {job.message_file_path}")
    typer.echo(f"Next run: {display_timestamp(job.next_run_at)}")
    if job.last_run_at:
        typer.echo(f"Last run: {display_timestamp(job.last_run_at)}")
    typer.echo(f"Message:\n{job.message}")


def _print_logs(entries: list[LogEntry]) -> None:
    typer.echo("TIME                 SRC        JOB  SESSION        ENTER DELAY  STATUS   ERROR")
    for entry in entries:
        job_id = "-" if entry.job_id is None else str(entry.job_id)
        error = entry.error_text or ""
        source = entry.trigger_type
        enter = "yes" if entry.send_enter else "no"
        delay = entry.enter_delay_ms if entry.send_enter else 0
        typer.echo(
            f"{display_timestamp(entry.created_at):<20} {source:<10} {job_id:<4} "
            f"{entry.session_name:<14} {enter:<5} {delay:<6} {entry.status:<8} {error}"
        )


def _sort_sessions(sessions: list[SessionInfo], by: SessionOrder) -> list[SessionInfo]:
    key = (
        (lambda session: session.created_at)
        if by == SessionOrder.created
        else (lambda session: session.activity_at)
    )
    return sorted(sessions, key=key, reverse=True)


def _load_sessions(*, by: SessionOrder, limit: int | None = None) -> list[SessionInfo]:
    try:
        sessions = _sort_sessions(tmux_api.list_session_info(), by)
    except Exception as exc:
        _fail(str(exc))
    if limit is not None:
        sessions = sessions[:limit]
    return sessions


def _print_recent_sessions(sessions: list[SessionInfo]) -> None:
    typer.echo("IDX  SESSION               CREATED")
    for index, session in enumerate(sessions, start=1):
        typer.echo(
            f"{index:<4} {session.name:<21} {display_unix_timestamp(session.created_at):<20}"
        )
    program_name = _program_name()
    typer.echo("")
    typer.echo(f"Join a session: {program_name} <id> or {program_name} <session>")
    typer.echo(f"Create a new one: {program_name} :<session>")
    typer.echo(f"Use current folder: {program_name} - or {program_name} -name")
    typer.echo(f"Help: {program_name} --help")


def _resolve_session_target(target: str, by: SessionOrder) -> str:
    if target == ":current":
        return _resolve_session_name(target)
    if target.isdigit():
        index = int(target)
        if index < 1:
            _fail("index must be 1 or greater")
        try:
            sessions = _sort_sessions(tmux_api.list_session_info(), by)
        except Exception as exc:
            _fail(str(exc))
        if not sessions:
            _fail("no tmux sessions found")
        if index > len(sessions):
            _fail(f"only {len(sessions)} tmux session(s) found")
        return sessions[index - 1].name
    return target


def _list_jobs(*, session: str | None = None) -> None:
    conn = _conn()
    if session is not None:
        session = _resolve_session_name(session)
    _print_jobs(storage.list_jobs(conn, session_name=session))


def _show_job(job_id: int) -> None:
    conn = _conn()
    job = storage.get_job(conn, job_id)
    if job is None:
        typer.echo(f"No job with id {job_id}")
        raise typer.Exit(1)
    _print_job_detail(job)


@app.callback(invoke_without_command=True)
def root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    sessions = _load_sessions(by=SessionOrder.created, limit=10)
    _print_recent_sessions(sessions)
    raise typer.Exit()


@jobs_app.callback(invoke_without_command=True)
def jobs_root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    _list_jobs()
    raise typer.Exit()


@app.command("list")
def list_sessions(
    by: Annotated[SessionOrder, typer.Option("--by", help="Sort by session creation time or last activity.")] = SessionOrder.created,
) -> None:
    """List tmux sessions sorted by creation time or activity."""
    sessions = _load_sessions(by=by)
    _print_recent_sessions(sessions)


@app.command("l", hidden=True)
def list_sessions_alias(
    by: Annotated[SessionOrder, typer.Option("--by", help="Sort by session creation time or last activity.")] = SessionOrder.created,
) -> None:
    list_sessions(by=by)


@app.command("ls", hidden=True)
def list_sessions_ls_alias(
    by: Annotated[SessionOrder, typer.Option("--by", help="Sort by session creation time or last activity.")] = SessionOrder.created,
) -> None:
    list_sessions(by=by)


@app.command()
def recent(
    limit: Annotated[int, typer.Option("--limit", min=1, help="Number of sessions to show.")] = 10,
    by: Annotated[SessionOrder, typer.Option("--by", help="Sort by session creation time or last activity.")] = SessionOrder.created,
) -> None:
    """Show the most recent tmux sessions."""
    sessions = _load_sessions(by=by, limit=limit)
    _print_recent_sessions(sessions)


@app.command("r", hidden=True)
def recent_alias(
    limit: Annotated[int, typer.Option("--limit", min=1, help="Number of sessions to show.")] = 10,
    by: Annotated[SessionOrder, typer.Option("--by", help="Sort by session creation time or last activity.")] = SessionOrder.created,
) -> None:
    recent(limit=limit, by=by)


@app.command()
def send(
    session_name: Annotated[str, typer.Argument(autocompletion=_complete_session_names)],
    message: Annotated[str | None, typer.Option("--message", help="Message text to send.")] = None,
    message_file: Annotated[Path | None, typer.Option("--message-file", help="Read message text from a file.")] = None,
    no_enter: Annotated[bool, typer.Option("--no-enter", help="Do not press Enter after sending.")] = False,
    enter_delay_ms: Annotated[int, typer.Option("--enter-delay-ms", min=0, help="Wait this many milliseconds before pressing Enter.")] = 200,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Validate the target but do not send anything.")] = False,
) -> None:
    """Send a message to a tmux session."""
    conn = _conn()
    session_name = _resolve_session_name(session_name)
    resolved_message = _resolve_message(message=message, message_file=message_file)
    if not tmux_api.session_exists(session_name):
        storage.insert_log(
            conn,
            session_name=session_name,
            message=resolved_message,
            trigger_type="manual",
            send_enter=not no_enter,
            enter_delay_ms=enter_delay_ms,
            status="failed",
            error_text=f"tmux session '{session_name}' was not found",
        )
        _fail(f"tmux session '{session_name}' was not found")

    if dry_run:
        typer.echo(f"Would send to {session_name}: {resolved_message}")
        return

    try:
        tmux_api.send_keys(
            session_name,
            resolved_message,
            press_enter=not no_enter,
            enter_delay_ms=enter_delay_ms,
        )
    except Exception as exc:
        storage.insert_log(
            conn,
            session_name=session_name,
            message=resolved_message,
            trigger_type="manual",
            send_enter=not no_enter,
            enter_delay_ms=enter_delay_ms,
            status="failed",
            error_text=str(exc),
        )
        _fail(str(exc))

    storage.insert_log(
        conn,
        session_name=session_name,
        message=resolved_message,
        trigger_type="manual",
        send_enter=not no_enter,
        enter_delay_ms=enter_delay_ms,
        status="success",
    )
    typer.echo(f"Sent message to {session_name}")


@app.command()
def attach(
    session_name: Annotated[str, typer.Argument(autocompletion=_complete_session_names)],
    resize_window: Annotated[
        bool,
        typer.Option("--resize-window", "-r", help="Run resize-window -A after attaching."),
    ] = False,
) -> None:
    """Attach to an existing tmux session."""
    session_name = _resolve_session_name(session_name)
    try:
        tmux_api.attach_session(session_name, resize_window=resize_window)
    except Exception as exc:
        _fail(str(exc))


@app.command("create-or-attach")
def create_or_attach(
    session_name: Annotated[str, typer.Argument(autocompletion=_complete_session_names)],
    command: Annotated[
        list[str] | None,
        typer.Argument(help="Command to run when creating a new session."),
    ] = None,
    resize_window: Annotated[
        bool,
        typer.Option("--resize-window", "-r", help="Run resize-window -A after attaching."),
    ] = False,
    mem: Annotated[
        str | None,
        typer.Option("--mem", help="Memory cap for this session's scope, e.g. 24G."),
    ] = None,
) -> None:
    """Create a tmux session if needed, then attach to it."""
    session_name = _resolve_session_name(session_name)
    try:
        tmux_api.create_or_attach_session(
            session_name,
            resize_window=resize_window,
            shell_command=command,
            mem=mem,
        )
    except Exception as exc:
        _fail(str(exc))


@app.command("create-detached")
def create_detached(
    session_name: Annotated[str, typer.Argument(autocompletion=_complete_session_names)],
    start_dir: Annotated[
        str | None,
        typer.Option("--start-dir", "-c", help="Working directory for the new session."),
    ] = None,
    mem: Annotated[
        str | None,
        typer.Option("--mem", help="Memory cap for this session's scope, e.g. 24G."),
    ] = None,
) -> None:
    """Create a memory-capped detached session without attaching.

    Idempotent: a no-op if the session already exists. For consumers that
    attach over their own transport (e.g. tmux -CC control mode).
    """
    session_name = _resolve_session_name(session_name)
    try:
        tmux_api.create_detached_session(
            session_name,
            start_dir=start_dir,
            mem=mem,
        )
    except Exception as exc:
        _fail(str(exc))
    typer.echo(session_name)


@app.command()
def kill(
    target: str,
    by: Annotated[SessionOrder, typer.Option("--by", help="Interpret numeric IDs using session creation time or last activity.")] = SessionOrder.created,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Kill a tmux session by name or recent-session index."""
    session_name = _resolve_session_target(target, by)

    if not yes:
        confirmed = typer.confirm(f"Kill tmux session '{session_name}'?", default=False)
        if not confirmed:
            typer.echo("Aborted.")
            raise typer.Exit(code=1)

    try:
        tmux_api.kill_session(session_name)
    except Exception as exc:
        _fail(str(exc))
    typer.echo(f"Killed session {session_name}")


@app.command("k", hidden=True)
def kill_alias(
    target: str,
    by: Annotated[SessionOrder, typer.Option("--by", help="Interpret numeric IDs using session creation time or last activity.")] = SessionOrder.created,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
) -> None:
    kill(target=target, by=by, yes=yes)


@app.command()
def limit(
    target: Annotated[str, typer.Argument(autocompletion=_complete_session_names)],
    mem: Annotated[
        str | None,
        typer.Option("--mem", help="New live MemoryMax for this session's scope, e.g. 30G."),
    ] = None,
    swap: Annotated[
        str | None,
        typer.Option("--swap", help="New live MemorySwapMax for this session's scope, e.g. 8G."),
    ] = None,
    high: Annotated[
        str | None,
        typer.Option("--high", help="New live MemoryHigh soft-throttle threshold, e.g. 24G."),
    ] = None,
    by: Annotated[
        SessionOrder,
        typer.Option("--by", help="Interpret numeric IDs using session creation time or last activity."),
    ] = SessionOrder.created,
) -> None:
    """Change memory/swap/high limits for an already-running tmuxctl session."""
    session_name = _resolve_session_target(target, by)
    try:
        tmux_api.set_session_limits(session_name, mem=mem, swap=swap, high=high)
    except Exception as exc:
        _fail(str(exc))

    parts = []
    if high is not None:
        parts.append(f"MemoryHigh={high}")
    if mem is not None:
        parts.append(f"MemoryMax={mem}")
    if swap is not None:
        parts.append(f"MemorySwapMax={swap}")
    typer.echo(f"Updated {session_name}: {' '.join(parts)}")


@app.command()
def rename(
    target: Annotated[str, typer.Argument(autocompletion=_complete_session_names)],
    new_name: str,
    by: Annotated[SessionOrder, typer.Option("--by", help="Interpret numeric IDs using session creation time or last activity.")] = SessionOrder.created,
) -> None:
    """Rename a tmux session and retarget its scheduled jobs."""
    session_name = _resolve_session_target(target, by)
    conn = _conn()

    try:
        tmux_api.rename_session(session_name, new_name)
    except Exception as exc:
        _fail(str(exc))

    renamed_jobs = storage.rename_session_jobs(
        conn,
        session_name=session_name,
        new_session_name=new_name,
    )
    typer.echo(
        f"Renamed session {session_name} to {new_name}"
        f" ({renamed_jobs} job(s) updated)"
    )


@app.command("attach-last")
def attach_last(
    by: Annotated[SessionOrder, typer.Option("--by", help="Pick the newest session by creation time or last activity.")] = SessionOrder.created,
    resize_window: Annotated[
        bool,
        typer.Option("--resize-window", "-r", help="Run resize-window -A after attaching."),
    ] = False,
) -> None:
    """Attach to the newest tmux session."""
    try:
        sessions = _sort_sessions(tmux_api.list_session_info(), by)
    except Exception as exc:
        _fail(str(exc))
    if not sessions:
        _fail("no tmux sessions found")
    try:
        tmux_api.attach_session(sessions[0].name, resize_window=resize_window)
    except Exception as exc:
        _fail(str(exc))


@app.command("attach-recent")
def attach_recent(
    index: Annotated[int, typer.Argument(help="1-based index from the recent sessions list.")] = 1,
    by: Annotated[SessionOrder, typer.Option("--by", help="Pick sessions by creation time or last activity.")] = SessionOrder.created,
    resize_window: Annotated[
        bool,
        typer.Option("--resize-window", "-r", help="Run resize-window -A after attaching."),
    ] = False,
) -> None:
    """Attach to a tmux session from the recent-session list."""
    if index < 1:
        _fail("index must be 1 or greater")
    try:
        sessions = _sort_sessions(tmux_api.list_session_info(), by)
    except Exception as exc:
        _fail(str(exc))
    if not sessions:
        _fail("no tmux sessions found")
    if index > len(sessions):
        _fail(f"only {len(sessions)} tmux session(s) found")
    try:
        tmux_api.attach_session(sessions[index - 1].name, resize_window=resize_window)
    except Exception as exc:
        _fail(str(exc))


@jobs_app.command("add")
def jobs_add(
    session_name: Annotated[str, typer.Argument(autocompletion=_complete_session_names)],
    every: Annotated[str, typer.Option("--every", help="Recurring interval like 15m or 2h.")],
    message: Annotated[str | None, typer.Option("--message", help="Message text to send.")] = None,
    message_file: Annotated[Path | None, typer.Option("--message-file", help="Read message text from a file.")] = None,
    no_enter: Annotated[bool, typer.Option("--no-enter", help="Do not press Enter after sending.")] = False,
    enter_delay_ms: Annotated[int, typer.Option("--enter-delay-ms", min=0, help="Wait this many milliseconds before pressing Enter.")] = 200,
    start_now: Annotated[bool, typer.Option("--start-now", help="Run the job on the next daemon poll.")] = False,
) -> None:
    """Create a recurring message job for a tmux session."""
    session_name = _resolve_session_name(session_name)
    resolved_message, message_file_path = _resolve_job_message_source(
        message=message,
        message_file=message_file,
    )
    if not tmux_api.session_exists(session_name):
        _fail(f"tmux session '{session_name}' was not found")
    try:
        interval_seconds = parse_interval(every)
    except ValueError as exc:
        _fail(str(exc))

    conn = _conn()
    job = storage.create_job(
        conn,
        session_name=session_name,
        message=resolved_message,
        message_file_path=message_file_path,
        interval_seconds=interval_seconds,
        send_enter=not no_enter,
        enter_delay_ms=enter_delay_ms,
        start_now=start_now,
    )
    typer.echo(f"Created job {job.id} for {job.session_name} every {format_interval(job.interval_seconds)}")


@jobs_app.command("list")
def jobs_list(
    session: Annotated[
        str | None,
        typer.Option("--session", "-s", help="Only list jobs for the given tmux session."),
    ] = None,
) -> None:
    """List scheduled jobs."""
    _list_jobs(session=session)


@jobs_app.command("show")
def jobs_show(
    job_id: Annotated[int, typer.Argument(help="Show details for a specific job.")],
) -> None:
    """Show details for one job."""
    _show_job(job_id)


@jobs_app.command("pause")
def jobs_pause(
    job_id: int,
) -> None:
    """Pause a scheduled job."""
    conn = _conn()
    _require_job(conn, job_id)
    storage.set_job_enabled(conn, job_id, False)
    typer.echo(f"Paused job {job_id}")


@jobs_app.command("pause-current")
def jobs_pause_current() -> None:
    """Pause all scheduled jobs for the current tmux session."""
    conn = _conn()
    session_name = _resolve_session_name(":current")
    _require_session_jobs(conn, session_name)
    changed = storage.set_session_jobs_enabled(
        conn,
        session_name=session_name,
        enabled=False,
    )
    typer.echo(f"Paused {changed} job(s) for session {session_name}")


@jobs_app.command("resume")
def jobs_resume(
    job_id: int,
) -> None:
    """Resume a paused job."""
    conn = _conn()
    _require_job(conn, job_id)
    storage.set_job_enabled(conn, job_id, True)
    typer.echo(f"Resumed job {job_id}")


@jobs_app.command("resume-current")
def jobs_resume_current() -> None:
    """Resume all scheduled jobs for the current tmux session."""
    conn = _conn()
    session_name = _resolve_session_name(":current")
    _require_session_jobs(conn, session_name)
    changed = storage.set_session_jobs_enabled(
        conn,
        session_name=session_name,
        enabled=True,
    )
    typer.echo(f"Resumed {changed} job(s) for session {session_name}")


@jobs_app.command("remove")
def jobs_remove(
    job_id: int,
) -> None:
    """Remove a scheduled job."""
    conn = _conn()
    if not storage.delete_job(conn, job_id):
        _fail(f"job {job_id} was not found")
    typer.echo(f"Removed job {job_id}")


@jobs_app.command("edit")
def jobs_edit(
    job_id: int,
    message: Annotated[str | None, typer.Option("--message", help="Replace the stored message text.")] = None,
    message_file: Annotated[Path | None, typer.Option("--message-file", help="Replace the stored message text from a file.")] = None,
    every: Annotated[str | None, typer.Option("--every", help="Replace the recurring interval.")] = None,
    session: Annotated[str | None, typer.Option("--session", help="Replace the tmux session name.")] = None,
    enter_delay_ms: Annotated[int | None, typer.Option("--enter-delay-ms", min=0, help="Replace the Enter delay in milliseconds.")] = None,
    enable: Annotated[bool, typer.Option("--enable", help="Enable the job.")] = False,
    disable: Annotated[bool, typer.Option("--disable", help="Disable the job.")] = False,
) -> None:
    """Update an existing scheduled job."""
    conn = _conn()
    job = _require_job(conn, job_id)

    if session is not None:
        session = _resolve_session_name(session)

    if session is not None and not tmux_api.session_exists(session):
        _fail(f"tmux session '{session}' was not found")

    interval_seconds = None
    if every is not None:
        try:
            interval_seconds = parse_interval(every)
        except ValueError as exc:
            _fail(str(exc))

    enabled = None
    if enable and disable:
        _fail("choose either --enable or --disable, not both")
    if enable:
        enabled = True
    if disable:
        enabled = False

    next_run_at = None
    if interval_seconds is not None and job.enabled and enabled is not False:
        next_run_at = storage.compute_next_run(interval_seconds)
    if enabled is True:
        base_interval = interval_seconds if interval_seconds is not None else job.interval_seconds
        next_run_at = storage.compute_next_run(base_interval)

    resolved_message = None
    message_file_path = None
    if message is not None or message_file is not None:
        resolved_message, message_file_path = _resolve_job_message_source(
            message=message,
            message_file=message_file,
        )

    updated = storage.update_job(
        conn,
        job_id,
        session_name=session,
        message=resolved_message,
        message_file_path=message_file_path,
        interval_seconds=interval_seconds,
        enter_delay_ms=enter_delay_ms,
        enabled=enabled,
        next_run_at=next_run_at,
    )
    if updated is None:
        _fail(f"job {job_id} was not found")
    typer.echo(f"Updated job {job_id}")


@jobs_app.command("logs")
def jobs_logs(
    limit: Annotated[int, typer.Option("--limit", min=1, help="Number of log rows to show.")] = 20,
) -> None:
    """Show recent delivery logs."""
    conn = _conn()
    _print_logs(storage.list_logs(conn, limit=limit))


@jobs_app.command("daemon")
def jobs_daemon(
    poll_interval: Annotated[int, typer.Option("--poll-interval", min=1, help="Seconds between job polls.")] = 3,
    run_once: Annotated[bool, typer.Option("--run-once", help="Process due jobs once and exit.")] = False,
) -> None:
    """Run the scheduler daemon or process due jobs once."""
    if run_once:
        count = scheduler.run_once()
        typer.echo(f"Processed {count} due job(s)")
        return
    scheduler.run_daemon(poll_interval=poll_interval)


@app.command("daemon", hidden=True)
def daemon_alias(
    poll_interval: Annotated[int, typer.Option("--poll-interval", min=1, help="Seconds between job polls.")] = 3,
    run_once: Annotated[bool, typer.Option("--run-once", help="Process due jobs once and exit.")] = False,
) -> None:
    """Backward-compatible alias for ``tmuxctl jobs daemon``."""
    jobs_daemon(poll_interval=poll_interval, run_once=run_once)


def _run_text(args: list[str]) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, check=False)
    except (OSError, FileNotFoundError):
        return ""
    return result.stdout


def _format_idle(days: float) -> str:
    if days >= 1:
        return f"{days:.0f}d"
    hours = days * 24
    if hours >= 1:
        return f"{hours:.0f}h"
    return "<1h"


@app.command()
def doctor() -> None:
    """Diagnose OOM risk: RAM, cgroup OOM kills, and robust session scopes."""
    typer.echo("== RAM ==")
    free_out = _run_text(["free", "-h"])
    typer.echo(free_out.rstrip() if free_out else "(free unavailable)")

    typer.echo("")
    typer.echo("== cgroup OOM kills ==")
    uid = os.getuid()
    events_path = Path(
        f"/sys/fs/cgroup/user.slice/user-{uid}.slice/"
        f"user@{uid}.service/memory.events"
    )
    try:
        events_text = events_path.read_text(encoding="utf-8")
    except OSError:
        events_text = ""
    if events_text:
        oom_kill = "?"
        for line in events_text.splitlines():
            if line.startswith("oom_kill "):
                oom_kill = line.split()[1]
        typer.echo(f"oom_kill = {oom_kill}  (source: {events_path})")
        if oom_kill not in ("0", "?"):
            typer.echo("  ^ non-zero: the OOM-killer has fired in your user slice")
    else:
        typer.echo(f"(no memory.events at {events_path})")

    typer.echo("")
    typer.echo("== robust session scopes ==")
    if not robust.systemd_available():
        typer.echo("systemd-run unavailable; sessions run uncapped (no scopes)")
    else:
        try:
            live = tmux_api.list_sessions()
        except Exception:
            live = []
        if not live:
            typer.echo("(no live sessions)")
        for name in live:
            unit = robust.scope_unit_name(name)
            props = robust.scope_properties(
                unit, ["MemoryHigh", "MemoryMax", "MemorySwapMax", "MemoryPeak"]
            )
            high = props.get("MemoryHigh") or "-"
            mem = props.get("MemoryMax") or "-"
            swap = props.get("MemorySwapMax") or "-"
            peak = props.get("MemoryPeak") or "-"
            typer.echo(
                f"  {name:<24} MemoryHigh={high}  MemoryMax={mem}  "
                f"MemorySwapMax={swap}  MemoryPeak={peak}"
            )

    typer.echo("")
    typer.echo("== tmux server placement ==")
    if not robust.systemd_available():
        typer.echo("(systemd unavailable)")
    else:
        pid = tmux_api.server_pid()
        if pid is None:
            typer.echo("(no tmux server running)")
        else:
            cgroup = tmux_api.process_cgroup(pid) or "?"
            login_scoped = (
                "/session-" in cgroup
                and cgroup.endswith(".scope")
                and "robust.slice" not in cgroup
            )
            if login_scoped:
                typer.echo(f"  WARNING: server pid {pid} lives in a login session scope:")
                typer.echo(f"    {cgroup}")
                typer.echo(
                    "    => it DIES when that SSH login logs out, taking ALL sessions"
                )
                typer.echo(
                    "       with it. Cold-restart the server through tmuxctl so it moves"
                )
                typer.echo(
                    f"       into {robust.server_unit_name()} under {robust.server_slice_name()}."
                )
            elif "robust.slice" in cgroup:
                typer.echo(f"  WARNING: server pid {pid} lives in the workload slice:")
                typer.echo(f"    {cgroup}")
                typer.echo(
                    "    => aggregate workload OOM can still kill the shared tmux server."
                )
                typer.echo(
                    "       Cold-restart it through tmuxctl so it moves into "
                    f"{robust.server_slice_name()}."
                )
            elif robust.server_unit_name() in cgroup or robust.server_slice_name() in cgroup:
                typer.echo(f"  ok: server pid {pid} is login-independent ({cgroup})")
            else:
                typer.echo(f"  server pid {pid}: {cgroup}")

    typer.echo("")
    typer.echo("== crash survivors (dead but registered sessions) ==")
    report = strays_mod.scan_all()
    dead_found = False
    for scan in report.scans:
        for session in scan.sessions:
            if session.windows == 0:
                dead_found = True
                typer.echo(f"  {session.name} on {scan.socket} (0 windows)")
    if report.orphan_pids:
        dead_found = True
        typer.echo(f"  orphaned tmux server pids: {report.orphan_pids}")
    orphan_clients = strays_mod.scan_orphan_control_clients()
    for client in orphan_clients:
        dead_found = True
        typer.echo(
            f"  orphan control client {client.target} on session "
            f"{client.session} ({client.socket}) — 't reap-clients'"
        )
    if not dead_found:
        typer.echo("(none)")


_SCOPE_PROPS = [
    "ActiveState",
    "ControlGroup",
    "MemoryCurrent",
    "MemoryPeak",
    "MemoryMax",
    "MemorySwapCurrent",
    "MemorySwapMax",
    "CPUUsageNSec",
    "TasksCurrent",
]


_CGROUP_ROOT = Path("/sys/fs/cgroup")
_TOP_MEMORY_ROWS = 5


def _scope_bytes(values: dict[str, str], key: str) -> int | None:
    """Parse a numeric systemd byte/count property, treating unset as None."""
    raw = values.get(key, "")
    if raw in ("", "[not set]", "infinity"):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _cgroup_proc_pids(cgroup: str) -> list[int]:
    """Read live PIDs from a cgroup v2 path as reported by systemd."""
    if not cgroup.startswith("/"):
        return []
    path = _CGROUP_ROOT / cgroup.lstrip("/") / "cgroup.procs"
    try:
        return [int(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    except (OSError, ValueError):
        return []


def _proc_comm(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _proc_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()


def _proc_rss_kb(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                return int(parts[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _proc_pgid(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        fields_after_comm = stat.rsplit(") ", 1)[1].split()
        return int(fields_after_comm[2])
    except (IndexError, ValueError):
        return None


def _shorten(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    return value[: max(0, width - 3)] + "..."


def _describe_memory_breakdown_lines(cgroup: str) -> list[str]:
    """Summarize RSS inside a cgroup by executable name and process group."""
    by_command: dict[str, tuple[int, int]] = {}
    by_pgid: dict[int, tuple[int, int, str]] = {}

    for pid in _cgroup_proc_pids(cgroup):
        rss_kb = _proc_rss_kb(pid)
        comm = _proc_comm(pid)
        if rss_kb is None or comm is None:
            continue

        total, count = by_command.get(comm, (0, 0))
        by_command[comm] = (total + rss_kb, count + 1)

        pgid = _proc_pgid(pid)
        if pgid is None:
            continue
        args = _proc_cmdline(pid) or comm
        pg_total, pg_count, sample = by_pgid.get(pgid, (0, 0, args))
        by_pgid[pgid] = (pg_total + rss_kb, pg_count + 1, sample)

    if not by_command and not by_pgid:
        return []

    lines = ["Memory by command (RSS):"]
    for comm, (rss_kb, count) in sorted(
        by_command.items(), key=lambda item: item[1][0], reverse=True
    )[:_TOP_MEMORY_ROWS]:
        lines.append(
            f"  {comm:<18} {format_bytes(rss_kb * 1024):>7}  {count:>4} process(es)"
        )

    if by_pgid:
        lines.append("Memory by process group (RSS):")
        for pgid, (rss_kb, count, sample) in sorted(
            by_pgid.items(), key=lambda item: item[1][0], reverse=True
        )[:_TOP_MEMORY_ROWS]:
            lines.append(
                f"  {format_bytes(rss_kb * 1024):>7}  {count:>4} process(es)  "
                f"PGID={pgid:<8} {_shorten(sample, 72)}"
            )

    return lines


def _uncapped_scope_lines(session_name: str, panes: list) -> list[str]:
    """Scope section for a session with no active robust cgroup.

    Reads the real cgroup of the session's process (from /proc) so a session
    started outside tmuxctl shows where it actually lives — a plain
    ``session-NN.scope`` rather than a memory-capped ``tmuxctl-*.scope``.
    """
    lines = ["Scope:    none — session is uncapped (not started by tmuxctl)"]
    ref = next((pane for pane in panes if pane.active), panes[0] if panes else None)
    if ref is not None and ref.pid:
        cgroup = tmux_api.process_cgroup(ref.pid)
        if cgroup:
            lines.append(f"Cgroup:   {cgroup}")
    lines.append("          No per-session RAM/CPU cap; the box-wide OOM-killer can")
    lines.append("          take the whole tmux server. Start a capped one with:")
    lines.append(f"          {_program_name()} :{session_name} --mem 24G")
    return lines


def _describe_scope_lines(session_name: str, panes: list) -> list[str]:
    """Render the cgroup / memory / CPU section for a session's robust scope."""
    unit = robust.scope_unit_name(session_name)
    scope = f"{unit}.scope"
    if not robust.systemd_available():
        return ["Scope:    none — systemd-run unavailable, session runs uncapped"]

    values = robust.scope_properties(unit, _SCOPE_PROPS)
    if values.get("ActiveState") != "active":
        return _uncapped_scope_lines(session_name, panes)

    lines = [f"Scope:    {scope}  (active)"]
    cgroup = values.get("ControlGroup")
    if cgroup:
        lines.append(f"Cgroup:   {cgroup}")

    current = _scope_bytes(values, "MemoryCurrent")
    cap = _scope_bytes(values, "MemoryMax")
    extras = []
    peak = _scope_bytes(values, "MemoryPeak")
    if peak is not None:
        extras.append(f"peak {format_bytes(peak)}")
    swap_current = _scope_bytes(values, "MemorySwapCurrent")
    swap_cap = _scope_bytes(values, "MemorySwapMax")
    if swap_current is not None:
        swap_str = format_bytes(swap_current)
        if swap_cap is not None:
            swap_str = f"{swap_str} / {format_bytes(swap_cap)}"
        extras.append(f"swap {swap_str}")
    mem = format_bytes(current) if current is not None else "-"
    cap_str = format_bytes(cap) if cap is not None else "unlimited"
    extra_str = f"  ({', '.join(extras)})" if extras else ""
    lines.append(f"Memory:   {mem} / {cap_str}{extra_str}")
    if cgroup:
        lines.extend(_describe_memory_breakdown_lines(cgroup))

    cpu = _scope_bytes(values, "CPUUsageNSec")
    if cpu is not None:
        lines.append(f"CPU time: {format_cpu_time(cpu)}")
    tasks = values.get("TasksCurrent")
    if tasks and tasks not in ("[not set]", "infinity"):
        lines.append(f"Tasks:    {tasks}")
    return lines


@app.command()
def describe(
    target: Annotated[
        str,
        typer.Argument(
            autocompletion=_complete_session_names,
            help="Session name, ':current', or a recent-session index.",
        ),
    ],
    by: Annotated[
        SessionOrder,
        typer.Option("--by", help="Interpret numeric IDs using session creation time or last activity."),
    ] = SessionOrder.created,
) -> None:
    """Describe a session: process, working directory, cgroup, RAM, and CPU."""
    session_name = _resolve_session_target(target, by)
    if not tmux_api.session_exists(session_name):
        _fail(f"tmux session '{session_name}' was not found")
    try:
        panes = tmux_api.session_panes(session_name)
    except Exception as exc:
        _fail(str(exc))

    windows = len({pane.window_index for pane in panes})
    typer.echo(f"Session:  {session_name}  ({windows} window(s), {len(panes)} pane(s))")
    typer.echo("")
    typer.echo("WIN.PANE  PID      COMMAND          DIRECTORY")
    for pane in panes:
        marker = "*" if pane.active else ""
        typer.echo(
            f"{pane.label + marker:<9} {pane.pid:<8} {pane.command:<16} {pane.cwd}"
        )

    typer.echo("")
    for line in _describe_scope_lines(session_name, panes):
        typer.echo(line)


@app.command("info", hidden=True)
def describe_alias(
    target: Annotated[str, typer.Argument(autocompletion=_complete_session_names)],
    by: Annotated[SessionOrder, typer.Option("--by")] = SessionOrder.created,
) -> None:
    describe(target=target, by=by)


def _print_stray_report(report: strays_mod.StrayReport, *, stale: int | None) -> None:
    typer.echo("SOCKET                          SESSION            ATTACHED IDLE   WIN")
    any_row = False
    for scan in report.scans:
        if not scan.reachable:
            continue
        for session in scan.sessions:
            idle = session.idle_days()
            if stale is not None and idle < stale:
                continue
            any_row = True
            attached = "yes" if session.attached else "no"
            sock_label = scan.socket if len(scan.socket) <= 30 else "..." + scan.socket[-27:]
            typer.echo(
                f"{sock_label:<31} {session.name:<18} {attached:<8} "
                f"{_format_idle(idle):<6} {session.windows}"
            )
    if not any_row:
        typer.echo("(no matching sessions)")

    if report.dead_sockets:
        typer.echo("")
        typer.echo("Dead socket files (removable):")
        for sock in report.dead_sockets:
            typer.echo(f"  {sock}")

    if report.orphan_pids:
        typer.echo("")
        typer.echo("Orphaned tmux server pids (no reachable socket):")
        for pid in report.orphan_pids:
            typer.echo(f"  {pid}")


def _print_orphan_control_clients(
    orphans: list[strays_mod.ControlClient],
) -> None:
    if not orphans:
        return
    typer.echo("")
    typer.echo("Orphan control-mode clients (detach with 't reap-clients --yes'):")
    for client in orphans:
        typer.echo(
            f"  {client.target}  session={client.session}  "
            f"idle={_format_idle(client.idle_days())}  socket={client.socket}"
        )


@app.command()
def strays(
    stale: Annotated[
        int | None,
        typer.Option("--stale", min=0, help="Only show sessions idle >= N days."),
    ] = None,
) -> None:
    """Scan ALL tmux sockets for stray sessions, dead sockets, orphan servers."""
    report = strays_mod.scan_all()
    _print_stray_report(report, stale=stale)
    _print_orphan_control_clients(strays_mod.scan_orphan_control_clients())


@app.command()
def reap(
    stale: Annotated[
        int,
        typer.Option("--stale", min=0, help="Reap detached sessions idle >= N days."),
    ] = 14,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Actually kill/remove (otherwise dry-run)."),
    ] = False,
) -> None:
    """Kill detached idle servers and remove dead socket files (guarded)."""
    report = strays_mod.scan_all()

    servers_to_kill: list[strays_mod.SocketScan] = []
    for scan in report.scans:
        if not scan.reachable:
            continue
        # GUARD: never touch a server with any attached session.
        if scan.has_attached:
            continue
        if not scan.sessions:
            continue
        # Kill only when every session is idle >= stale days.
        if all(session.idle_days() >= stale for session in scan.sessions):
            servers_to_kill.append(scan)

    if not servers_to_kill and not report.dead_sockets:
        typer.echo("Nothing to reap.")
        return

    action = "Killing" if yes else "Would kill"
    for scan in servers_to_kill:
        names = ", ".join(s.name for s in scan.sessions)
        typer.echo(f"{action} server {scan.socket} (sessions: {names})")
        if yes:
            subprocess.run(
                ["tmux", "-S", scan.socket, "kill-server"],
                capture_output=True,
                text=True,
                check=False,
            )

    rm_action = "Removing" if yes else "Would remove"
    for sock in report.dead_sockets:
        typer.echo(f"{rm_action} dead socket file {sock}")
        if yes:
            try:
                os.remove(sock)
            except OSError as exc:
                typer.echo(f"  (failed: {exc})")

    if not yes:
        typer.echo("")
        typer.echo("Dry-run. Re-run with --yes to apply.")


@app.command("reap-clients")
def reap_clients(
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Actually detach (otherwise dry-run)."),
    ] = False,
) -> None:
    """Detach orphan control-mode clients (stale -CC duplicates) (guarded).

    The PocketShell app attaches over tmux ``-CC`` control mode and holds
    exactly ONE control-mode client per session, detaching it cleanly on exit.
    An app crash / force-kill can strand that control client attached forever.
    This finds sessions carrying more than one control-mode client and detaches
    the older/idle duplicates, keeping the most-recently-active one. It NEVER
    kills a session, a server, or a non-control (interactive) client.
    """
    orphans = strays_mod.scan_orphan_control_clients()
    if not orphans:
        typer.echo("No orphan control-mode clients.")
        return

    action = "Detaching" if yes else "Would detach"
    for client in orphans:
        typer.echo(
            f"{action} orphan control client {client.target} "
            f"(session {client.session}, socket {client.socket}, "
            f"idle {_format_idle(client.idle_days())})"
        )
        if yes and not strays_mod.detach_client(client.socket, client.target):
            typer.echo("  (failed)")

    if not yes:
        typer.echo("")
        typer.echo("Dry-run. Re-run with --yes to detach.")


app.add_typer(jobs_app, name="jobs")
app.add_typer(jobs_app, name="j", hidden=True)


def main() -> None:
    _set_program_name(sys.argv[0])
    argv = list(sys.argv[1:])
    if argv:
        first = argv[0]
        if first == "-":
            argv = ["create-or-attach", _current_directory_session_name(), *argv[1:]]
        elif first.startswith("-") and not first.startswith("--") and len(first) > 1:
            argv = [
                "create-or-attach",
                _current_directory_session_name(suffix=first[1:]),
                *argv[1:],
            ]
        elif first == ":current":
            argv = ["create-or-attach", first, *argv[1:]]
        elif first.startswith(":") and len(first) > 1:
            argv = ["create-or-attach", first[1:], *argv[1:]]
        elif first.isdigit() and int(first) >= 1:
            argv = ["attach-recent", first, *argv[1:]]
        elif not first.startswith("-") and first not in ROOT_COMMAND_NAMES:
            argv = ["attach", first, *argv[1:]]
    app(args=argv)
