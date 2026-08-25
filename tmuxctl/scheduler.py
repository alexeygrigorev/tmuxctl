from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from tmuxctl import robust, salvage, storage, tmux_api
from tmuxctl.models import Job
from tmuxctl.utils import to_timestamp, utcnow

MAX_CONSECUTIVE_FAILURES = 3
_DAEMON_SESSION_SENTINEL = "__daemon__"


def _resolve_job_message(job: Job) -> str:
    if not job.message_file_path:
        return job.message
    return Path(job.message_file_path).read_text(encoding="utf-8").rstrip("\r\n")


def run_job(conn, job: Job) -> tuple[bool, str | None]:
    sent_at = utcnow()
    next_run_at = to_timestamp(sent_at + timedelta(seconds=job.interval_seconds))
    error_text = None
    status = "success"
    message = job.message

    try:
        message = _resolve_job_message(job)
        tmux_api.send_keys(
            job.session_name,
            message,
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
        message=message,
        trigger_type="scheduled",
        send_enter=job.send_enter,
        enter_delay_ms=job.enter_delay_ms,
        status=status,
        error_text=error_text,
    )

    recent_failures = storage.count_recent_consecutive_failures(conn, job.id)
    if recent_failures >= MAX_CONSECUTIVE_FAILURES:
        storage.delete_job(conn, job.id)
        return False, error_text

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


def run_daemon(
    *,
    poll_interval: int = 3,
    db_path: Path | None = None,
    health_interval: int = 60,
    auto_salvage: bool | None = None,
) -> None:
    """Run due jobs on ``poll_interval``, and (§4) confirm every known
    session's server is alive on the coarser ``health_interval`` cadence,
    piggybacking on this already-persistent daemon rather than adding a
    second one. ``auto_salvage`` (default: resolved from config, itself
    defaulting to off) controls whether an unhealthy session is actually
    recreated automatically, or only detected/logged for a human to act on
    via ``tmuxctl salvage --recreate``.
    """
    conn = storage.get_connection(db_path)
    resolved_auto_salvage = robust.resolve_auto_salvage() if auto_salvage is None else auto_salvage
    last_health_check = 0.0
    state = _HealthState()
    while True:
        for job in storage.get_due_jobs(conn):
            run_job(conn, job)
        now = time.monotonic()
        if now - last_health_check >= health_interval:
            state = _check_server_health(conn, auto_salvage=resolved_auto_salvage, previous=state)
            last_health_check = now
        time.sleep(poll_interval)


@dataclass(slots=True)
class _HealthState:
    unhealthy: frozenset[str] = frozenset()
    capacity_over: bool = False


def _check_server_health(conn, *, auto_salvage: bool, previous: _HealthState) -> _HealthState:
    """One health-check pass: find sessions the durable log/live scan
    considers unhealthy, log only the CHANGE from last time (a fresh
    problem, or one that resolved) to keep ``session_events`` from filling
    up with an identical "still broken" row every ``health_interval``, and
    (only if ``auto_salvage``) recreate what's safe to recreate.

    Returns the current state, for the caller to pass back in next time so
    transitions can be detected.
    """
    report = salvage.scan(conn)
    # "reattachable-dtach" still counts as unhealthy: the workload survived
    # (§1 did its job), but there's no server/pty on it until something
    # recreates the session -- salvage (or --recreate) is still needed.
    unhealthy = {
        e.session_name: e
        for e in report.entries
        if e.status not in ("healthy", "reattachable")
    }
    unhealthy_names = frozenset(unhealthy)

    if unhealthy_names != previous.unhealthy:
        newly_unhealthy = unhealthy_names - previous.unhealthy
        recovered = previous.unhealthy - unhealthy_names
        detail_parts = []
        if newly_unhealthy:
            detail_parts.append(
                "new: " + ", ".join(f"{n}({unhealthy[n].status})" for n in sorted(newly_unhealthy))
            )
        if recovered:
            detail_parts.append("recovered: " + ", ".join(sorted(recovered)))
        storage.record_session_event(
            conn, _DAEMON_SESSION_SENTINEL, "health_check", detail="; ".join(detail_parts)
        )
        if newly_unhealthy:
            print(
                f"tmuxctl daemon: {len(newly_unhealthy)} session(s) need attention "
                f"({', '.join(sorted(newly_unhealthy))}) -- run 'tmuxctl salvage' for details",
                file=sys.stderr,
            )

    if auto_salvage:
        for name, entry in unhealthy.items():
            if entry.status == "needs-manual-reclaim":
                continue
            try:
                tmux_api.create_detached_session(name, start_dir=entry.cwd, mem=entry.mem)
                storage.record_session_event(
                    conn, name, "health_check", detail="auto-recreated by daemon"
                )
            except Exception as exc:  # noqa: BLE001 - one bad recreate must not stop the daemon
                storage.record_session_event(
                    conn, name, "health_check", detail=f"auto-recreate failed: {exc}"
                )

    # §3: log a capacity warning only when crossing the threshold, not on
    # every tick while it stays over -- the creation-time stderr note is the
    # courtesy; this is the actual defense against missing it for weeks.
    capacity_over = previous.capacity_over
    try:
        pct = robust.resolve_oversubscription_max_pct()
        if pct > 0:
            capacity = robust.system_capacity()
            if capacity > 0:
                reserved = robust.total_reserved_mem()
                capacity_over = reserved > capacity * pct // 100
                if capacity_over and not previous.capacity_over:
                    storage.record_session_event(
                        conn, _DAEMON_SESSION_SENTINEL, "capacity_warning",
                        detail=(
                            f"reserved={robust.format_size(reserved)} "
                            f"capacity={robust.format_size(capacity)} threshold={pct}%"
                        ),
                    )
    except Exception:  # noqa: BLE001 - the guard must never crash the daemon
        pass

    return _HealthState(unhealthy=unhealthy_names, capacity_over=capacity_over)
