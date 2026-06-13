from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tmuxctl import robust, tmux_api


def _disable_systemd(monkeypatch) -> None:
    """Force the no-systemd path so create_or_attach uses a plain new-session.

    Keeps the historical argv assertions meaningful and deterministic across
    machines (the dev box has systemd; CI usually does not).
    """
    monkeypatch.setattr(robust, "systemd_available", lambda: False)
    monkeypatch.setattr(tmux_api.os, "getcwd", lambda: "/work/dir")


def test_attach_session_can_resize_window(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(tmux_api, "session_exists", lambda session_name: True)

    def fake_run_tmux(args, *, check=True, timeout=10):
        captured["args"] = args
        captured["check"] = check
        captured["timeout"] = timeout
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tmux_api, "_run_tmux", fake_run_tmux)

    tmux_api.attach_session("git-llm-zoomcamp", resize_window=True)

    assert captured["args"] == [
        "attach-session",
        "-t",
        "git-llm-zoomcamp",
        ";",
        "resize-window",
        "-A",
    ]
    assert captured["check"] is False


def test_attach_session_can_resize_window_inside_tmux(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setenv("TMUX", "/tmp/tmux")
    monkeypatch.setattr(tmux_api, "session_exists", lambda session_name: True)

    def fake_run_tmux(args, *, check=True, timeout=10):
        captured["args"] = args
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tmux_api, "_run_tmux", fake_run_tmux)

    tmux_api.attach_session("git-llm-zoomcamp", resize_window=True)

    assert captured["args"] == [
        "switch-client",
        "-t",
        "git-llm-zoomcamp",
        ";",
        "resize-window",
        "-A",
    ]


def test_create_or_attach_runs_command_only_when_creating(monkeypatch) -> None:
    captured: list[list[str]] = []

    monkeypatch.delenv("TMUX", raising=False)
    session_exists_results = iter([False, True, True])
    monkeypatch.setattr(tmux_api, "session_exists", lambda session_name: next(session_exists_results))

    def fake_run_tmux(args, *, check=True, timeout=10):
        captured.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tmux_api, "_run_tmux", fake_run_tmux)
    _disable_systemd(monkeypatch)

    tmux_api.create_or_attach_session("git-workshops", shell_command=["cy"])

    assert captured == [
        ["new-session", "-d", "-s", "git-workshops", "-c", "/work/dir"],
        ["send-keys", "-t", "git-workshops", "cy"],
        ["send-keys", "-t", "git-workshops", "Enter"],
        ["attach-session", "-t", "git-workshops"],
    ]


def test_create_or_attach_preserves_create_command_quoting(monkeypatch) -> None:
    captured: list[list[str]] = []

    monkeypatch.delenv("TMUX", raising=False)
    session_exists_results = iter([False, True, True])
    monkeypatch.setattr(tmux_api, "session_exists", lambda session_name: next(session_exists_results))

    def fake_run_tmux(args, *, check=True, timeout=10):
        captured.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tmux_api, "_run_tmux", fake_run_tmux)
    _disable_systemd(monkeypatch)

    tmux_api.create_or_attach_session(
        "git-workshops",
        shell_command=["python", "-c", "print('hello world')"],
    )

    assert captured[1] == [
        "send-keys",
        "-t",
        "git-workshops",
        "python -c 'print('\"'\"'hello world'\"'\"')'",
    ]


def test_create_or_attach_ignores_command_when_attaching(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(tmux_api, "session_exists", lambda session_name: True)

    def fake_attach_session(session_name: str, *, resize_window: bool = False) -> None:
        captured["session_name"] = session_name
        captured["resize_window"] = resize_window

    monkeypatch.setattr(tmux_api, "attach_session", fake_attach_session)

    tmux_api.create_or_attach_session("git-workshops", shell_command=["cy"])

    assert captured == {"session_name": "git-workshops", "resize_window": False}


def test_create_or_attach_wraps_shell_in_scope_when_systemd(monkeypatch) -> None:
    captured: list[list[str]] = []

    monkeypatch.delenv("TMUX", raising=False)
    session_exists_results = iter([False, True])
    monkeypatch.setattr(tmux_api, "session_exists", lambda session_name: next(session_exists_results))
    monkeypatch.setattr(tmux_api, "attach_session", lambda *a, **k: None)

    # Deterministic systemd-available path.
    monkeypatch.setattr(robust, "systemd_available", lambda: True)
    monkeypatch.setattr(robust, "resolve_mem", lambda *, flag=None, cwd=None: "7G")
    monkeypatch.setattr(robust, "resolve_swap", lambda *, cwd=None: "2G")
    monkeypatch.setattr(robust, "resolve_slice_max", lambda: "40G")
    monkeypatch.setattr(robust, "resolve_slice_swap_max", lambda: "12G")
    ensured: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        robust,
        "ensure_slice",
        lambda slice_max, *, swap_max=None: ensured.append((slice_max, swap_max)) or True,
    )
    monkeypatch.setattr(tmux_api, "_login_shell", lambda: ["/bin/bash", "-l"])
    monkeypatch.setattr(tmux_api.os, "getcwd", lambda: "/home/me/proj")

    def fake_run_tmux(args, *, check=True, timeout=10):
        captured.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tmux_api, "_run_tmux", fake_run_tmux)
    monkeypatch.setattr(tmux_api, "ensure_server", lambda: None)

    tmux_api.create_or_attach_session("proj")

    create_cmd = captured[0]
    assert create_cmd[:6] == ["new-session", "-d", "-s", "proj", "-c", "/home/me/proj"]
    # The wrapped login shell follows, built by robust.scope_wrap.
    assert create_cmd[6:] == robust.scope_wrap(
        ["/bin/bash", "-l"], "tmuxctl-proj", "7G", swap="2G"
    )
    assert "systemd-run" in create_cmd
    assert ensured == [("40G", "12G")]


def test_create_detached_is_noop_when_session_exists(monkeypatch) -> None:
    monkeypatch.setattr(tmux_api, "session_exists", lambda session_name: True)

    called: list[list[str]] = []
    monkeypatch.setattr(
        tmux_api, "_run_tmux",
        lambda args, **k: called.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )

    tmux_api.create_detached_session("proj")

    assert called == []  # idempotent: never resurrect or duplicate


def test_create_detached_plain_when_no_systemd(monkeypatch) -> None:
    captured: list[list[str]] = []
    monkeypatch.setattr(tmux_api, "session_exists", lambda session_name: False)
    monkeypatch.setattr(tmux_api, "attach_session", lambda *a, **k: pytest.fail("must not attach"))
    _disable_systemd(monkeypatch)

    monkeypatch.setattr(
        tmux_api, "_run_tmux",
        lambda args, **k: captured.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )

    tmux_api.create_detached_session("git-workshops")

    # Detached, never attached, plain (uncapped) session at the resolved cwd.
    assert captured == [["new-session", "-d", "-s", "git-workshops", "-c", "/work/dir"]]


def test_create_detached_wraps_scope_and_honors_start_dir(monkeypatch) -> None:
    captured: list[list[str]] = []
    monkeypatch.setattr(tmux_api, "session_exists", lambda session_name: False)
    monkeypatch.setattr(tmux_api, "attach_session", lambda *a, **k: pytest.fail("must not attach"))

    monkeypatch.setattr(robust, "systemd_available", lambda: True)
    seen_cwd: list = []
    monkeypatch.setattr(
        robust, "resolve_mem",
        lambda *, flag=None, cwd=None: seen_cwd.append((flag, cwd)) or "30G",
    )
    monkeypatch.setattr(robust, "resolve_swap", lambda *, cwd=None: "6G")
    monkeypatch.setattr(robust, "resolve_slice_max", lambda: "40G")
    monkeypatch.setattr(robust, "resolve_slice_swap_max", lambda: "12G")
    monkeypatch.setattr(robust, "ensure_slice", lambda slice_max, *, swap_max=None: True)
    monkeypatch.setattr(tmux_api, "_login_shell", lambda: ["/bin/bash", "-l"])
    monkeypatch.setattr(
        tmux_api, "_run_tmux",
        lambda args, **k: captured.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )

    monkeypatch.setattr(tmux_api, "ensure_server", lambda: None)
    tmux_api.create_detached_session("proj", start_dir="/repo")

    create_cmd = captured[0]
    assert create_cmd[:6] == ["new-session", "-d", "-s", "proj", "-c", "/repo"]
    assert create_cmd[6:] == robust.scope_wrap(
        ["/bin/bash", "-l"], "tmuxctl-proj", "30G", swap="6G"
    )
    assert "systemd-run" in create_cmd
    # mem resolved against the start_dir, so the repo's cgroups.toml policy wins.
    assert seen_cwd == [(None, Path("/repo"))]


def test_create_detached_flag_overrides_mem(monkeypatch) -> None:
    captured: list[list[str]] = []
    monkeypatch.setattr(tmux_api, "session_exists", lambda session_name: False)
    monkeypatch.setattr(robust, "systemd_available", lambda: True)
    monkeypatch.setattr(robust, "resolve_swap", lambda *, cwd=None: "6G")
    monkeypatch.setattr(robust, "resolve_slice_max", lambda: "40G")
    monkeypatch.setattr(robust, "resolve_slice_swap_max", lambda: "12G")
    monkeypatch.setattr(robust, "ensure_slice", lambda slice_max, *, swap_max=None: True)
    monkeypatch.setattr(tmux_api, "_login_shell", lambda: ["/bin/bash", "-l"])
    monkeypatch.setattr(tmux_api.os, "getcwd", lambda: "/work/dir")
    monkeypatch.setattr(
        tmux_api, "_run_tmux",
        lambda args, **k: captured.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )

    monkeypatch.setattr(tmux_api, "ensure_server", lambda: None)
    tmux_api.create_detached_session("proj", mem="8G")

    assert captured[0][6:] == robust.scope_wrap(
        ["/bin/bash", "-l"], "tmuxctl-proj", "8G", swap="6G"
    )


def test_server_running_true_on_empty_server(monkeypatch) -> None:
    # rc 0 with no sessions = server up (exit-empty off), still "running".
    monkeypatch.setattr(
        tmux_api, "_run_tmux",
        lambda args, **k: subprocess.CompletedProcess(args, 0, "", ""),
    )
    assert tmux_api._server_running() is True


def test_server_running_false_when_no_server(monkeypatch) -> None:
    monkeypatch.setattr(
        tmux_api, "_run_tmux",
        lambda args, **k: subprocess.CompletedProcess(args, 1, "", "no server running on /tmp/x"),
    )
    assert tmux_api._server_running() is False


def test_ensure_server_noop_without_systemd(monkeypatch) -> None:
    monkeypatch.setattr(robust, "systemd_available", lambda: False)
    monkeypatch.setattr(
        tmux_api, "_server_running",
        lambda: pytest.fail("must not probe when systemd is unavailable"),
    )
    monkeypatch.setattr(
        tmux_api.subprocess, "run",
        lambda *a, **k: pytest.fail("must not bootstrap a server without systemd"),
    )
    tmux_api.ensure_server()  # graceful no-op


def test_ensure_server_skips_when_server_already_running(monkeypatch) -> None:
    monkeypatch.setattr(robust, "systemd_available", lambda: True)
    monkeypatch.setattr(tmux_api, "_server_running", lambda: True)
    monkeypatch.setattr(
        tmux_api.subprocess, "run",
        lambda *a, **k: pytest.fail("must not bootstrap over an existing server"),
    )
    tmux_api.ensure_server()


def test_ensure_server_bootstraps_in_own_unit_when_no_server(monkeypatch) -> None:
    monkeypatch.setattr(robust, "systemd_available", lambda: True)
    monkeypatch.setattr(tmux_api, "_server_running", lambda: False)
    monkeypatch.setattr(robust, "resolve_slice_max", lambda: "40G")
    monkeypatch.setattr(robust, "resolve_slice_swap_max", lambda: "12G")
    ensured: list = []
    monkeypatch.setattr(
        robust, "ensure_slice",
        lambda slice_max, *, swap_max=None: ensured.append((slice_max, swap_max)) or True,
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        tmux_api.subprocess, "run",
        lambda args, **k: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )

    tmux_api.ensure_server()

    # Parent slice bound first, then the server bootstrapped in its own unit.
    assert ensured == [("40G", "12G")]
    assert calls == [robust.server_bootstrap_argv()]


def test_create_detached_ensures_server_before_creating(monkeypatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(tmux_api, "session_exists", lambda name: False)
    monkeypatch.setattr(tmux_api, "ensure_server", lambda: order.append("ensure_server"))
    monkeypatch.setattr(
        tmux_api, "_new_session_command",
        lambda name, cwd, *, flag: ["new-session", "-d", "-s", name],
    )
    monkeypatch.setattr(
        tmux_api, "_run_tmux",
        lambda args, **k: order.append("new-session") or subprocess.CompletedProcess(args, 0, "", ""),
    )

    tmux_api.create_detached_session("proj")

    # The server must be in its safe unit BEFORE the first session is created,
    # otherwise that session spawns the server inside the caller's login scope.
    assert order == ["ensure_server", "new-session"]


def test_kill_session_stops_scope(monkeypatch) -> None:
    monkeypatch.setattr(tmux_api, "session_exists", lambda session_name: True)
    monkeypatch.setattr(
        tmux_api, "_run_tmux",
        lambda args, **k: subprocess.CompletedProcess(args, 0, "", ""),
    )

    stopped: list[str] = []
    monkeypatch.setattr(robust, "stop_scope", lambda unit: stopped.append(unit))

    tmux_api.kill_session("proj")

    assert stopped == ["tmuxctl-proj"]


def test_set_session_limits_updates_scope_properties(monkeypatch) -> None:
    monkeypatch.setattr(tmux_api, "session_exists", lambda session_name: True)
    monkeypatch.setattr(robust, "systemd_available", lambda: True)
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tmux_api.subprocess, "run", fake_run)

    tmux_api.set_session_limits("proj", mem="30G", swap="8G")

    assert calls == [
        [
            "systemctl",
            "--user",
            "set-property",
            "tmuxctl-proj.scope",
            "MemoryMax=30G",
            "MemorySwapMax=8G",
        ]
    ]


def test_set_session_limits_updates_high(monkeypatch) -> None:
    monkeypatch.setattr(tmux_api, "session_exists", lambda session_name: True)
    monkeypatch.setattr(robust, "systemd_available", lambda: True)
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tmux_api.subprocess, "run", fake_run)

    tmux_api.set_session_limits("proj", mem="30G", high="24G")

    assert calls == [
        [
            "systemctl",
            "--user",
            "set-property",
            "tmuxctl-proj.scope",
            "MemoryHigh=24G",
            "MemoryMax=30G",
        ]
    ]


def test_set_session_limits_requires_limit(monkeypatch) -> None:
    with pytest.raises(tmux_api.TmuxCommandError, match="provide --mem"):
        tmux_api.set_session_limits("proj")


def test_set_session_limits_reports_systemctl_failure(monkeypatch) -> None:
    monkeypatch.setattr(tmux_api, "session_exists", lambda session_name: True)
    monkeypatch.setattr(robust, "systemd_available", lambda: True)
    monkeypatch.setattr(
        tmux_api.subprocess,
        "run",
        lambda args, **k: subprocess.CompletedProcess(args, 1, "", "not a scope"),
    )

    with pytest.raises(tmux_api.TmuxCommandError, match="not a scope"):
        tmux_api.set_session_limits("proj", mem="30G")


def test_session_exists_uses_exact_match(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_tmux(args, *, check=True, timeout=None):
        captured["args"] = args
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tmux_api, "_run_tmux", fake_run_tmux)

    assert tmux_api.session_exists("git-pocketshell") is True
    # Leading '=' forces an exact match so a prefix like "git-pocketshell"
    # never matches an existing "git-pocketshell-desktop".
    assert captured["args"] == ["has-session", "-t", "=git-pocketshell"]


def test_session_panes_parses_list_panes(monkeypatch) -> None:
    monkeypatch.setattr(tmux_api, "session_exists", lambda name: True)

    stdout = "0\t0\t111\tnvim\t/home/a/proj\t1\n1\t0\t222\tpython\t/home/a/proj/sub\t0\n"

    def fake_run_tmux(args, *, check=True, timeout=None):
        assert args[0] == "list-panes"
        assert "-s" in args
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(tmux_api, "_run_tmux", fake_run_tmux)

    panes = tmux_api.session_panes("proj")
    assert [p.label for p in panes] == ["0.0", "1.0"]
    assert panes[0].command == "nvim"
    assert panes[0].cwd == "/home/a/proj"
    assert panes[0].active is True
    assert panes[1].pid == 222
    assert panes[1].active is False


def test_session_panes_requires_existing_session(monkeypatch) -> None:
    monkeypatch.setattr(tmux_api, "session_exists", lambda name: False)
    raised = False
    try:
        tmux_api.session_panes("ghost")
    except tmux_api.TmuxSessionNotFoundError:
        raised = True
    assert raised


def test_process_cgroup_reads_unified_line(monkeypatch, tmp_path) -> None:
    cgroup_file = tmp_path / "cgroup"
    cgroup_file.write_text(
        "0::/user.slice/user-1000.slice/session-9.scope\n", encoding="utf-8"
    )
    monkeypatch.setattr(tmux_api, "Path", lambda p: cgroup_file)
    assert tmux_api.process_cgroup(1234) == "/user.slice/user-1000.slice/session-9.scope"

    cgroup_file.write_text("", encoding="utf-8")
    assert tmux_api.process_cgroup(1234) is None
