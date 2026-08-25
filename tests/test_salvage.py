from __future__ import annotations

import sqlite3
import subprocess

import pytest

from tmuxctl import robust, salvage, storage, tmux_api


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    storage.init_db(conn)
    return conn


def test_scope_units_parses_list_units(monkeypatch) -> None:
    monkeypatch.setattr(robust, "systemd_available", lambda: True)

    def fake_run(args, **kwargs):
        assert args[:4] == ["systemctl", "--user", "list-units", "tmuxctl-*.scope"]
        return subprocess.CompletedProcess(
            args, 0,
            "tmuxctl-proj.scope         loaded active running Session proj\n"
            "tmuxctl-git-foo.scope      loaded failed failed  Session git-foo\n",
            "",
        )

    monkeypatch.setattr(salvage.subprocess, "run", fake_run)
    assert salvage._scope_units() == ["tmuxctl-proj", "tmuxctl-git-foo"]


def test_scope_units_empty_without_systemd(monkeypatch) -> None:
    monkeypatch.setattr(robust, "systemd_available", lambda: False)
    assert salvage._scope_units() == []


def test_session_name_from_scope_unit() -> None:
    assert salvage._session_name_from_scope_unit("tmuxctl-git-foo") == "git-foo"
    assert salvage._session_name_from_scope_unit("weird-unit") == "weird-unit"


def test_classify_scope_healthy_when_own_socket_reachable(monkeypatch) -> None:
    monkeypatch.setattr(salvage, "_socket_has_session", lambda socket, name: True)
    entry = salvage._classify_scope("proj", "tmuxctl-proj")
    assert entry.status == "healthy"
    assert entry.reattach_command == f"tmux -S {robust.socket_for('proj')} attach -t proj"


def test_classify_scope_healthy_on_legacy_shared_socket(monkeypatch) -> None:
    # Regression: every session created before the §0 migration lives on
    # the legacy shared/default socket, not its own -- that must still read
    # as healthy, not stale-work (its scope's only live process is the pane
    # shell itself, comm != "tmux", which would otherwise misclassify it).
    monkeypatch.setattr(salvage, "_socket_has_session", lambda socket, name: False)
    monkeypatch.setattr(tmux_api, "locate_session", lambda name: "/tmp/tmux-1000/default")
    cgroup_checked = []
    monkeypatch.setattr(robust, "scope_cgroup_path", lambda unit: cgroup_checked.append(unit) or None)

    entry = salvage._classify_scope("proj", "tmuxctl-proj")

    assert entry.status == "healthy"
    assert entry.reattach_command == "tmux -S /tmp/tmux-1000/default attach -t proj"
    assert cgroup_checked == []  # short-circuited before ever looking at the cgroup


def test_classify_scope_gone_when_no_live_pids(monkeypatch) -> None:
    monkeypatch.setattr(salvage, "_socket_has_session", lambda socket, name: False)
    monkeypatch.setattr(tmux_api, "locate_session", lambda name: None)
    monkeypatch.setattr(robust, "scope_cgroup_path", lambda unit: "/robust.slice/tmuxctl-proj.scope")
    monkeypatch.setattr(robust, "cgroup_proc_pids", lambda cgroup: [])
    entry = salvage._classify_scope("proj", "tmuxctl-proj")
    assert entry.status == "gone"


def test_classify_scope_needs_manual_reclaim_for_foreign_tmux(monkeypatch) -> None:
    monkeypatch.setattr(salvage, "_socket_has_session", lambda socket, name: False)
    monkeypatch.setattr(tmux_api, "locate_session", lambda name: None)
    monkeypatch.setattr(robust, "scope_cgroup_path", lambda unit: "/robust.slice/tmuxctl-proj.scope")
    monkeypatch.setattr(robust, "cgroup_proc_pids", lambda cgroup: [123])
    monkeypatch.setattr(salvage, "_proc_comm", lambda pid: "tmux")
    entry = salvage._classify_scope("proj", "tmuxctl-proj")
    assert entry.status == "needs-manual-reclaim"
    assert entry.pid == 123
    assert "systemctl --user stop tmuxctl-proj.scope" in entry.detail


def test_classify_scope_reattachable_dtach_when_survivor(monkeypatch) -> None:
    monkeypatch.setattr(salvage, "_socket_has_session", lambda socket, name: False)
    monkeypatch.setattr(tmux_api, "locate_session", lambda name: None)
    monkeypatch.setattr(robust, "scope_cgroup_path", lambda unit: "/robust.slice/tmuxctl-proj.scope")
    monkeypatch.setattr(robust, "cgroup_proc_pids", lambda cgroup: [111, 222])
    monkeypatch.setattr(salvage, "_proc_comm", lambda pid: "dtach")
    monkeypatch.setattr(robust, "dtach_socket_for", lambda name, pane_id="0": f"/run/tmuxctl/dtach/{name}.sock")

    entry = salvage._classify_scope("proj", "tmuxctl-proj")

    assert entry.status == "reattachable-dtach"
    assert entry.reattach_command == "dtach -a /run/tmuxctl/dtach/proj.sock"
    assert entry.pid == 111


def test_classify_scope_stale_work_with_deleted_cwd(monkeypatch) -> None:
    monkeypatch.setattr(salvage, "_socket_has_session", lambda socket, name: False)
    monkeypatch.setattr(tmux_api, "locate_session", lambda name: None)
    monkeypatch.setattr(robust, "scope_cgroup_path", lambda unit: "/robust.slice/tmuxctl-proj.scope")
    monkeypatch.setattr(robust, "cgroup_proc_pids", lambda cgroup: [999])
    monkeypatch.setattr(salvage, "_proc_comm", lambda pid: "python3")
    monkeypatch.setattr(salvage, "_proc_cwd", lambda pid: "/tmp/deleted-worktree")
    monkeypatch.setattr(salvage, "_proc_cmdline", lambda pid: "python3 manage.py runserver")
    monkeypatch.setattr(salvage.os.path, "exists", lambda p: False)

    entry = salvage._classify_scope("proj", "tmuxctl-proj")

    assert entry.status == "stale-work"
    assert entry.pid == 999
    assert entry.cwd == "/tmp/deleted-worktree"
    assert "DELETED" in entry.detail
    assert "runserver" in entry.detail


def test_classify_scope_stale_work_with_existing_cwd(monkeypatch) -> None:
    monkeypatch.setattr(salvage, "_socket_has_session", lambda socket, name: False)
    monkeypatch.setattr(tmux_api, "locate_session", lambda name: None)
    monkeypatch.setattr(robust, "scope_cgroup_path", lambda unit: "/robust.slice/tmuxctl-proj.scope")
    monkeypatch.setattr(robust, "cgroup_proc_pids", lambda cgroup: [999])
    monkeypatch.setattr(salvage, "_proc_comm", lambda pid: "node")
    monkeypatch.setattr(salvage, "_proc_cwd", lambda pid: "/home/me/proj")
    monkeypatch.setattr(salvage, "_proc_cmdline", lambda pid: "node dev-server.js")
    monkeypatch.setattr(salvage.os.path, "exists", lambda p: True)

    entry = salvage._classify_scope("proj", "tmuxctl-proj")

    assert entry.status == "stale-work"
    assert "cwd OK" in entry.detail


def test_scan_from_log_recovers_gone_session() -> None:
    conn = _conn()
    storage.record_session_event(
        conn, "git-old-proj", "created",
        start_dir="/home/me/git-old-proj", mem="12G",
    )

    entries = salvage._scan_from_log(conn, seen_names=set())

    assert len(entries) == 1
    assert entries[0].session_name == "git-old-proj"
    assert entries[0].status == "gone"
    assert entries[0].cwd == "/home/me/git-old-proj"
    assert entries[0].mem == "12G"


def test_scan_from_log_skips_deliberately_killed_sessions() -> None:
    conn = _conn()
    storage.record_session_event(conn, "git-old-proj", "created", start_dir="/x")
    storage.record_session_event(conn, "git-old-proj", "killed")

    entries = salvage._scan_from_log(conn, seen_names=set())

    assert entries == []


def test_scan_from_log_recreated_after_kill_is_not_skipped() -> None:
    conn = _conn()
    storage.record_session_event(conn, "git-old-proj", "created", start_dir="/x")
    storage.record_session_event(conn, "git-old-proj", "killed")
    storage.record_session_event(conn, "git-old-proj", "created", start_dir="/y")

    entries = salvage._scan_from_log(conn, seen_names=set())

    assert len(entries) == 1
    assert entries[0].cwd == "/y"


def test_scan_from_log_skips_already_seen_names() -> None:
    conn = _conn()
    storage.record_session_event(conn, "git-old-proj", "created", start_dir="/x")
    entries = salvage._scan_from_log(conn, seen_names={"git-old-proj"})
    assert entries == []


def test_scan_combines_scopes_live_sessions_and_log(monkeypatch) -> None:
    conn = _conn()
    storage.record_session_event(conn, "git-reboot-victim", "created", start_dir="/home/me/rv")

    monkeypatch.setattr(salvage, "_scope_units", lambda: ["tmuxctl-git-capped"])
    monkeypatch.setattr(
        salvage, "_classify_scope",
        lambda name, unit: salvage.SalvageEntry(session_name=name, status="healthy", detail="ok"),
    )
    monkeypatch.setattr(tmux_api, "list_sessions", lambda: ["git-capped", "git-uncapped"])
    monkeypatch.setattr(tmux_api, "locate_session", lambda name: f"/tmp/tmux-1000/tmuxctl-{name}")

    report = salvage.scan(conn)
    names = {e.session_name: e.status for e in report.entries}

    assert names["git-capped"] == "healthy"  # from the scope, not double-counted
    assert names["git-uncapped"] == "healthy"  # live but no scope
    assert names["git-reboot-victim"] == "gone"  # from the log only
    assert len(report.entries) == 3
