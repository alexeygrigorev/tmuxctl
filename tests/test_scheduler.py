from __future__ import annotations

import sqlite3
from pathlib import Path

from tmuxctl import scheduler, storage


def test_run_job_reads_latest_message_from_file(monkeypatch, tmp_path: Path) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    storage.init_db(conn)

    message_file = tmp_path / "prompt.txt"
    message_file.write_text("initial text\n", encoding="utf-8")
    job = storage.create_job(
        conn,
        session_name="rk-codex",
        message="initial text",
        message_file_path=str(message_file),
        interval_seconds=60,
    )

    message_file.write_text("updated text\n", encoding="utf-8")
    sent: dict[str, object] = {}

    def fake_send_keys(session_name: str, message: str, press_enter: bool, enter_delay_ms: int) -> None:
        sent["session_name"] = session_name
        sent["message"] = message
        sent["press_enter"] = press_enter
        sent["enter_delay_ms"] = enter_delay_ms

    monkeypatch.setattr("tmuxctl.scheduler.tmux_api.send_keys", fake_send_keys)

    ok, error = scheduler.run_job(conn, job)

    assert ok is True
    assert error is None
    assert sent["message"] == "updated text"
    log_entry = storage.list_logs(conn, limit=1)[0]
    assert log_entry.message == "updated text"


def test_run_job_removes_job_after_three_consecutive_failures(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    storage.init_db(conn)

    job = storage.create_job(
        conn,
        session_name="rk-codex",
        message="check status",
        interval_seconds=60,
    )

    def fail_send_keys(session_name: str, message: str, press_enter: bool, enter_delay_ms: int) -> None:
        raise RuntimeError(f"tmux session '{session_name}' was not found")

    monkeypatch.setattr("tmuxctl.scheduler.tmux_api.send_keys", fail_send_keys)

    for _ in range(3):
        ok, error = scheduler.run_job(conn, job)
        assert ok is False
        assert error == "tmux session 'rk-codex' was not found"

    assert storage.get_job(conn, job.id) is None
    logs = storage.list_logs(conn, limit=3)
    assert [entry.status for entry in logs] == ["failed", "failed", "failed"]


def test_run_job_keeps_job_when_failure_streak_is_broken(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    storage.init_db(conn)

    job = storage.create_job(
        conn,
        session_name="rk-codex",
        message="check status",
        interval_seconds=60,
    )

    outcomes = [RuntimeError("first failure"), None, RuntimeError("second failure"), RuntimeError("third failure")]

    def flaky_send_keys(session_name: str, message: str, press_enter: bool, enter_delay_ms: int) -> None:
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome

    monkeypatch.setattr("tmuxctl.scheduler.tmux_api.send_keys", flaky_send_keys)

    for _ in range(4):
        scheduler.run_job(conn, job)

    surviving_job = storage.get_job(conn, job.id)
    assert surviving_job is not None
    logs = storage.list_logs(conn, limit=4)
    assert [entry.status for entry in logs] == ["failed", "failed", "success", "failed"]


# ---------------------------------------------------------------------------
# §4: daemon health check
# ---------------------------------------------------------------------------
def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    storage.init_db(conn)
    return conn


def _fake_report(monkeypatch, entries):
    from tmuxctl import salvage as salvage_mod

    monkeypatch.setattr(salvage_mod, "scan", lambda conn: salvage_mod.SalvageReport(entries=entries))


def _no_capacity_pressure(monkeypatch) -> None:
    monkeypatch.setattr("tmuxctl.scheduler.robust.resolve_oversubscription_max_pct", lambda: 0)


def test_check_server_health_logs_only_on_new_problem(monkeypatch) -> None:
    from tmuxctl import salvage as salvage_mod

    conn = _conn()
    _no_capacity_pressure(monkeypatch)
    _fake_report(
        monkeypatch,
        [salvage_mod.SalvageEntry(session_name="proj", status="gone", detail="d")],
    )

    state = scheduler._check_server_health(conn, auto_salvage=False, previous=scheduler._HealthState())
    assert state.unhealthy == frozenset({"proj"})
    events = storage.list_session_events(conn, session_name="__daemon__")
    assert len(events) == 1
    assert "new: proj(gone)" in events[0].detail

    # Same unhealthy set again -- no new health_check row.
    state2 = scheduler._check_server_health(conn, auto_salvage=False, previous=state)
    assert state2.unhealthy == state.unhealthy
    assert len(storage.list_session_events(conn, session_name="__daemon__")) == 1


def test_check_server_health_logs_recovery(monkeypatch) -> None:
    from tmuxctl import salvage as salvage_mod

    conn = _conn()
    _no_capacity_pressure(monkeypatch)
    _fake_report(monkeypatch, [])

    previous = scheduler._HealthState(unhealthy=frozenset({"proj"}))
    state = scheduler._check_server_health(conn, auto_salvage=False, previous=previous)

    assert state.unhealthy == frozenset()
    events = storage.list_session_events(conn, session_name="__daemon__")
    assert len(events) == 1
    assert "recovered: proj" in events[0].detail


def test_check_server_health_healthy_sessions_are_not_unhealthy(monkeypatch) -> None:
    from tmuxctl import salvage as salvage_mod

    conn = _conn()
    _no_capacity_pressure(monkeypatch)
    _fake_report(
        monkeypatch,
        [
            salvage_mod.SalvageEntry(session_name="a", status="healthy", detail="d"),
            salvage_mod.SalvageEntry(session_name="b", status="reattachable", detail="d"),
        ],
    )

    state = scheduler._check_server_health(conn, auto_salvage=False, previous=scheduler._HealthState())

    assert state.unhealthy == frozenset()
    assert storage.list_session_events(conn, session_name="__daemon__") == []


def test_check_server_health_auto_salvage_recreates_and_skips_needs_manual_reclaim(monkeypatch) -> None:
    from tmuxctl import salvage as salvage_mod

    conn = _conn()
    _no_capacity_pressure(monkeypatch)
    _fake_report(
        monkeypatch,
        [
            salvage_mod.SalvageEntry(session_name="gone-one", status="gone", detail="d", cwd="/home/x"),
            salvage_mod.SalvageEntry(session_name="squatted", status="needs-manual-reclaim", detail="d"),
        ],
    )
    recreated: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        "tmuxctl.scheduler.tmux_api.create_detached_session",
        lambda name, *, start_dir=None, mem=None: recreated.append((name, start_dir)),
    )

    scheduler._check_server_health(conn, auto_salvage=True, previous=scheduler._HealthState())

    assert recreated == [("gone-one", "/home/x")]
    detail_texts = [e.detail for e in storage.list_session_events(conn, session_name="gone-one")]
    assert any("auto-recreated" in d for d in detail_texts)


def test_check_server_health_auto_salvage_off_by_default_never_recreates(monkeypatch) -> None:
    from tmuxctl import salvage as salvage_mod

    conn = _conn()
    _no_capacity_pressure(monkeypatch)
    _fake_report(
        monkeypatch,
        [salvage_mod.SalvageEntry(session_name="gone-one", status="gone", detail="d")],
    )
    called = []
    monkeypatch.setattr(
        "tmuxctl.scheduler.tmux_api.create_detached_session",
        lambda *a, **k: called.append((a, k)),
    )

    scheduler._check_server_health(conn, auto_salvage=False, previous=scheduler._HealthState())

    assert called == []


def test_check_server_health_capacity_warning_logs_only_on_crossing(monkeypatch) -> None:
    conn = _conn()
    _fake_report(monkeypatch, [])
    monkeypatch.setattr("tmuxctl.scheduler.robust.resolve_oversubscription_max_pct", lambda: 100)
    monkeypatch.setattr("tmuxctl.scheduler.robust.system_capacity", lambda: 100)
    monkeypatch.setattr("tmuxctl.scheduler.robust.total_reserved_mem", lambda: 200)

    state = scheduler._check_server_health(conn, auto_salvage=False, previous=scheduler._HealthState())
    assert state.capacity_over is True
    assert len(storage.list_session_events(conn, session_name="__daemon__")) == 1

    # Still over on the next check -- must NOT log again.
    state2 = scheduler._check_server_health(conn, auto_salvage=False, previous=state)
    assert state2.capacity_over is True
    assert len(storage.list_session_events(conn, session_name="__daemon__")) == 1

    # Drops back under threshold, then crosses again -- logs once more.
    monkeypatch.setattr("tmuxctl.scheduler.robust.total_reserved_mem", lambda: 50)
    state3 = scheduler._check_server_health(conn, auto_salvage=False, previous=state2)
    assert state3.capacity_over is False

    monkeypatch.setattr("tmuxctl.scheduler.robust.total_reserved_mem", lambda: 200)
    scheduler._check_server_health(conn, auto_salvage=False, previous=state3)
    assert len(storage.list_session_events(conn, session_name="__daemon__")) == 2
