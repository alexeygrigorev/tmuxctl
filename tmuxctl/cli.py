from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated

import typer

from tmuxctl import scheduler, storage, tmux_api
from tmuxctl.models import Job, LogEntry, SessionInfo
from tmuxctl.utils import display_timestamp, display_unix_timestamp, format_interval, parse_interval

app = typer.Typer(help="Control tmux sessions and recurring messages.")


class SessionOrder(str, Enum):
    created = "created"
    activity = "activity"


def _conn() -> object:
    return storage.get_connection()


def _fail(message: str, *, code: int = 1) -> None:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=code)


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


def _print_recent_sessions(sessions: list[SessionInfo], by: SessionOrder) -> None:
    typer.echo("IDX  SESSION               CREATED              LAST ACTIVE          ATTACH")
    for index, session in enumerate(sessions, start=1):
        typer.echo(
            f"{index:<4} {session.name:<21} "
            f"{display_unix_timestamp(session.created_at):<20} "
            f"{display_unix_timestamp(session.activity_at):<20} "
            f"tmuxctl attach {session.name}"
        )
    if sessions:
        typer.echo(f"\nNewest by {by.value}: tmuxctl attach-last --by {by.value}")
        typer.echo(f"Nth recent: tmuxctl attach-recent <n> --by {by.value}")


@app.command("list")
def list_sessions() -> None:
    try:
        sessions = tmux_api.list_sessions()
    except Exception as exc:
        _fail(str(exc))
    typer.echo("SESSION")
    for session in sessions:
        typer.echo(session)


@app.command()
def recent(
    limit: Annotated[int, typer.Option("--limit", min=1, help="Number of sessions to show.")] = 10,
    by: Annotated[SessionOrder, typer.Option("--by", help="Sort by session creation time or last activity.")] = SessionOrder.created,
) -> None:
    try:
        sessions = _sort_sessions(tmux_api.list_session_info(), by)[:limit]
    except Exception as exc:
        _fail(str(exc))
    _print_recent_sessions(sessions, by)


@app.command()
def send(
    session_name: str,
    message: Annotated[str | None, typer.Option("--message", help="Message text to send.")] = None,
    message_file: Annotated[Path | None, typer.Option("--message-file", help="Read message text from a file.")] = None,
    no_enter: Annotated[bool, typer.Option("--no-enter", help="Do not press Enter after sending.")] = False,
    enter_delay_ms: Annotated[int, typer.Option("--enter-delay-ms", min=0, help="Wait this many milliseconds before pressing Enter.")] = 200,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Validate the target but do not send anything.")] = False,
) -> None:
    conn = _conn()
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
def attach(session_name: str) -> None:
    try:
        tmux_api.attach_session(session_name)
    except Exception as exc:
        _fail(str(exc))


@app.command("attach-last")
def attach_last(
    by: Annotated[SessionOrder, typer.Option("--by", help="Pick the newest session by creation time or last activity.")] = SessionOrder.created,
) -> None:
    try:
        sessions = _sort_sessions(tmux_api.list_session_info(), by)
    except Exception as exc:
        _fail(str(exc))
    if not sessions:
        _fail("no tmux sessions found")
    try:
        tmux_api.attach_session(sessions[0].name)
    except Exception as exc:
        _fail(str(exc))


@app.command("attach-recent")
def attach_recent(
    index: Annotated[int, typer.Argument(help="1-based index from the recent sessions list.")] = 1,
    by: Annotated[SessionOrder, typer.Option("--by", help="Pick sessions by creation time or last activity.")] = SessionOrder.created,
) -> None:
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
        tmux_api.attach_session(sessions[index - 1].name)
    except Exception as exc:
        _fail(str(exc))


@app.command("a1", hidden=True)
def attach_recent_1() -> None:
    attach_recent(1)


@app.command("a2", hidden=True)
def attach_recent_2() -> None:
    attach_recent(2)


@app.command("a3", hidden=True)
def attach_recent_3() -> None:
    attach_recent(3)


@app.command()
def add(
    session_name: str,
    every: Annotated[str, typer.Option("--every", help="Recurring interval like 15m or 2h.")],
    message: Annotated[str | None, typer.Option("--message", help="Message text to send.")] = None,
    message_file: Annotated[Path | None, typer.Option("--message-file", help="Read message text from a file.")] = None,
    no_enter: Annotated[bool, typer.Option("--no-enter", help="Do not press Enter after sending.")] = False,
    enter_delay_ms: Annotated[int, typer.Option("--enter-delay-ms", min=0, help="Wait this many milliseconds before pressing Enter.")] = 200,
    start_now: Annotated[bool, typer.Option("--start-now", help="Run the job on the next daemon poll.")] = False,
) -> None:
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


@app.command()
def jobs() -> None:
    conn = _conn()
    _print_jobs(storage.list_jobs(conn))


@app.command()
def pause(
    job_id: int,
) -> None:
    conn = _conn()
    _require_job(conn, job_id)
    storage.set_job_enabled(conn, job_id, False)
    typer.echo(f"Paused job {job_id}")


@app.command()
def resume(
    job_id: int,
) -> None:
    conn = _conn()
    _require_job(conn, job_id)
    storage.set_job_enabled(conn, job_id, True)
    typer.echo(f"Resumed job {job_id}")


@app.command()
def remove(
    job_id: int,
) -> None:
    conn = _conn()
    if not storage.delete_job(conn, job_id):
        _fail(f"job {job_id} was not found")
    typer.echo(f"Removed job {job_id}")


@app.command()
def edit(
    job_id: int,
    message: Annotated[str | None, typer.Option("--message", help="Replace the stored message text.")] = None,
    message_file: Annotated[Path | None, typer.Option("--message-file", help="Replace the stored message text from a file.")] = None,
    every: Annotated[str | None, typer.Option("--every", help="Replace the recurring interval.")] = None,
    session: Annotated[str | None, typer.Option("--session", help="Replace the tmux session name.")] = None,
    enter_delay_ms: Annotated[int | None, typer.Option("--enter-delay-ms", min=0, help="Replace the Enter delay in milliseconds.")] = None,
    enable: Annotated[bool, typer.Option("--enable", help="Enable the job.")] = False,
    disable: Annotated[bool, typer.Option("--disable", help="Disable the job.")] = False,
) -> None:
    conn = _conn()
    job = _require_job(conn, job_id)

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


@app.command()
def logs(
    limit: Annotated[int, typer.Option("--limit", min=1, help="Number of log rows to show.")] = 20,
) -> None:
    conn = _conn()
    _print_logs(storage.list_logs(conn, limit=limit))


@app.command()
def daemon(
    poll_interval: Annotated[int, typer.Option("--poll-interval", min=1, help="Seconds between job polls.")] = 3,
    run_once: Annotated[bool, typer.Option("--run-once", help="Process due jobs once and exit.")] = False,
) -> None:
    if run_once:
        count = scheduler.run_once()
        typer.echo(f"Processed {count} due job(s)")
        return
    scheduler.run_daemon(poll_interval=poll_interval)


def main() -> None:
    app()
