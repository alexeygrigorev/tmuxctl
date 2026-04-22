from __future__ import annotations

import sys
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from click.shell_completion import CompletionItem
from typer.core import TyperGroup

from tmuxctl import scheduler, storage, tmux_api
from tmuxctl.models import Job, LogEntry, SessionInfo
from tmuxctl.utils import display_timestamp, display_unix_timestamp, format_interval, parse_interval

ROOT_COMMAND_NAMES = {
    "list",
    "l",
    "ls",
    "recent",
    "r",
    "send",
    "attach",
    "create-or-attach",
    "kill",
    "k",
    "rename",
    "attach-last",
    "attach-recent",
    "add",
    "jobs",
    "pause",
    "resume",
    "remove",
    "edit",
    "logs",
    "daemon",
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
    typer.echo(f"Help: {program_name} --help")


def _resolve_session_target(target: str, by: SessionOrder) -> str:
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


@app.callback(invoke_without_command=True)
def root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    sessions = _load_sessions(by=SessionOrder.created, limit=10)
    _print_recent_sessions(sessions)
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
) -> None:
    """Attach to an existing tmux session."""
    try:
        tmux_api.attach_session(session_name)
    except Exception as exc:
        _fail(str(exc))


@app.command("create-or-attach")
def create_or_attach(
    session_name: Annotated[str, typer.Argument(autocompletion=_complete_session_names)],
) -> None:
    """Create a tmux session if needed, then attach to it."""
    try:
        tmux_api.create_or_attach_session(session_name)
    except Exception as exc:
        _fail(str(exc))


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
) -> None:
    """Attach to the newest tmux session."""
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
        tmux_api.attach_session(sessions[index - 1].name)
    except Exception as exc:
        _fail(str(exc))


@app.command()
def add(
    session_name: Annotated[str, typer.Argument(autocompletion=_complete_session_names)],
    every: Annotated[str, typer.Option("--every", help="Recurring interval like 15m or 2h.")],
    message: Annotated[str | None, typer.Option("--message", help="Message text to send.")] = None,
    message_file: Annotated[Path | None, typer.Option("--message-file", help="Read message text from a file.")] = None,
    no_enter: Annotated[bool, typer.Option("--no-enter", help="Do not press Enter after sending.")] = False,
    enter_delay_ms: Annotated[int, typer.Option("--enter-delay-ms", min=0, help="Wait this many milliseconds before pressing Enter.")] = 200,
    start_now: Annotated[bool, typer.Option("--start-now", help="Run the job on the next daemon poll.")] = False,
) -> None:
    """Create a recurring message job for a tmux session."""
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
def jobs(
    job_id: Annotated[int | None, typer.Argument(help="Show details for a specific job.")] = None,
    session: Annotated[
        str | None,
        typer.Option("--session", "-s", help="Only list jobs for the given tmux session."),
    ] = None,
) -> None:
    """List scheduled jobs or show details for one job."""
    conn = _conn()
    if job_id is None:
        _print_jobs(storage.list_jobs(conn, session_name=session))
        return
    job = storage.get_job(conn, job_id)
    if job is None:
        typer.echo(f"No job with id {job_id}")
        raise typer.Exit(1)
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


@app.command()
def pause(
    job_id: int,
) -> None:
    """Pause a scheduled job."""
    conn = _conn()
    _require_job(conn, job_id)
    storage.set_job_enabled(conn, job_id, False)
    typer.echo(f"Paused job {job_id}")


@app.command()
def resume(
    job_id: int,
) -> None:
    """Resume a paused job."""
    conn = _conn()
    _require_job(conn, job_id)
    storage.set_job_enabled(conn, job_id, True)
    typer.echo(f"Resumed job {job_id}")


@app.command()
def remove(
    job_id: int,
) -> None:
    """Remove a scheduled job."""
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
    """Update an existing scheduled job."""
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
    """Show recent delivery logs."""
    conn = _conn()
    _print_logs(storage.list_logs(conn, limit=limit))


@app.command()
def daemon(
    poll_interval: Annotated[int, typer.Option("--poll-interval", min=1, help="Seconds between job polls.")] = 3,
    run_once: Annotated[bool, typer.Option("--run-once", help="Process due jobs once and exit.")] = False,
) -> None:
    """Run the scheduler daemon or process due jobs once."""
    if run_once:
        count = scheduler.run_once()
        typer.echo(f"Processed {count} due job(s)")
        return
    scheduler.run_daemon(poll_interval=poll_interval)


def main() -> None:
    _set_program_name(sys.argv[0])
    argv = list(sys.argv[1:])
    if argv:
        first = argv[0]
        if first.startswith(":") and len(first) > 1:
            argv = ["create-or-attach", first[1:], *argv[1:]]
        elif first.isdigit() and int(first) >= 1:
            argv = ["attach-recent", first, *argv[1:]]
        elif not first.startswith("-") and first not in ROOT_COMMAND_NAMES:
            argv = ["attach", first, *argv[1:]]
    app(args=argv)
