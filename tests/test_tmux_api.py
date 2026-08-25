from __future__ import annotations

import shutil
import subprocess
import uuid
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


def _no_stale_scope(monkeypatch) -> None:
    """Pretend no same-named scope is lingering, so capped creation proceeds.

    Keeps the scope-wrapping assertions independent of the host's real systemd
    state (the dev box may have a leftover ``tmuxctl-proj.scope``).
    """
    monkeypatch.setattr(robust, "scope_properties", lambda unit, props: {})


def test_attach_session_can_resize_window(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(tmux_api, "locate_session", lambda name: "/tmp/tmux-1000/tmuxctl-git-llm-zoomcamp")

    def fake_run_tmux(args, *, socket=None, check=True, timeout=None):
        captured["args"] = args
        captured["socket"] = socket
        captured["check"] = check
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
    assert captured["socket"] == "/tmp/tmux-1000/tmuxctl-git-llm-zoomcamp"
    assert captured["check"] is False


def test_attach_session_can_resize_window_inside_tmux(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setenv("TMUX", "/tmp/tmux")
    monkeypatch.setattr(tmux_api, "locate_session", lambda name: "/tmp/tmux-1000/tmuxctl-git-llm-zoomcamp")

    def fake_run_tmux(args, *, socket=None, check=True, timeout=None):
        captured["args"] = args
        captured["socket"] = socket
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tmux_api, "_run_tmux", fake_run_tmux)

    tmux_api.attach_session("git-llm-zoomcamp", resize_window=True)

    # switch-client can't cross servers post-§0 (every session is its own
    # server), so an inside-tmux attach hands the client off via
    # detach-client -E instead, targeting the OTHER server explicitly.
    assert captured["args"] == [
        "detach-client",
        "-E",
        "tmux -S /tmp/tmux-1000/tmuxctl-git-llm-zoomcamp attach-session -t "
        "git-llm-zoomcamp ';' resize-window -A",
    ]
    # No socket= for this call: $TMUX must resolve the CURRENT server.
    assert captured["socket"] is None


def test_create_or_attach_runs_command_only_when_creating(monkeypatch) -> None:
    captured: list[list[str]] = []

    monkeypatch.delenv("TMUX", raising=False)
    socket = robust.socket_for("git-workshops")
    locate_results = iter([None, socket, socket])
    monkeypatch.setattr(tmux_api, "locate_session", lambda name: next(locate_results))

    def fake_run_tmux(args, **k):
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
    socket = robust.socket_for("git-workshops")
    locate_results = iter([None, socket, socket])
    monkeypatch.setattr(tmux_api, "locate_session", lambda name: next(locate_results))

    def fake_run_tmux(args, **k):
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
    locate_results = iter([None, robust.socket_for("proj")])
    monkeypatch.setattr(tmux_api, "locate_session", lambda name: next(locate_results))
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
    monkeypatch.setattr(tmux_api, "_login_shell", lambda session_name: ["/bin/bash", "-l"])
    monkeypatch.setattr(tmux_api.os, "getcwd", lambda: "/home/me/proj")
    _no_stale_scope(monkeypatch)
    monkeypatch.setattr(robust, "reset_session_server_unit", lambda name: None)

    def fake_subprocess_run(args, **k):
        captured.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tmux_api.subprocess, "run", fake_subprocess_run)

    tmux_api.create_or_attach_session("proj")

    # The whole create is now a single bootstrap: this session's OWN server,
    # started in its own systemd unit, immediately running the (still
    # scope-wrapped) new-session command on its own dedicated socket.
    # (`captured` may also hold an unrelated real `git rev-parse` call made
    # by the unmocked resolve_high()'s project-config lookup -- find the
    # actual bootstrap by its argv[0] rather than assuming call order.)
    bootstrap_cmd = next(c for c in captured if c[:1] == ["systemd-run"])
    assert bootstrap_cmd[:3] == ["systemd-run", "--user", "--unit=tmuxctl-server-proj"]
    wrapped = robust.scope_wrap(["/bin/bash", "-l"], "tmuxctl-proj", "7G", swap="2G")
    tmux_argv = ["new-session", "-d", "-s", "proj", "-c", "/home/me/proj", *wrapped]
    assert bootstrap_cmd[-len(tmux_argv):] == tmux_argv
    assert ensured == [("40G", "12G")]


def _scope_wrap_systemd(monkeypatch, captured: list[list[str]]) -> None:
    """Deterministic capped-creation path shared by the stale-scope tests."""
    monkeypatch.delenv("TMUX", raising=False)
    locate_results = iter([None, robust.socket_for("proj")])
    monkeypatch.setattr(tmux_api, "locate_session", lambda name: next(locate_results))
    monkeypatch.setattr(tmux_api, "attach_session", lambda *a, **k: None)
    monkeypatch.setattr(robust, "systemd_available", lambda: True)
    monkeypatch.setattr(robust, "resolve_mem", lambda *, flag=None, cwd=None: "7G")
    monkeypatch.setattr(robust, "resolve_swap", lambda *, cwd=None: "2G")
    monkeypatch.setattr(robust, "resolve_slice_max", lambda: "40G")
    monkeypatch.setattr(robust, "resolve_slice_swap_max", lambda: "12G")
    monkeypatch.setattr(robust, "ensure_slice", lambda slice_max, *, swap_max=None: True)
    monkeypatch.setattr(tmux_api, "_login_shell", lambda session_name: ["/bin/bash", "-l"])
    monkeypatch.setattr(tmux_api.os, "getcwd", lambda: "/home/me/proj")
    monkeypatch.setattr(robust, "reset_session_server_unit", lambda name: None)
    monkeypatch.setattr(
        tmux_api.subprocess, "run",
        lambda args, **k: captured.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )


def test_create_falls_back_to_uncapped_when_scope_still_active(monkeypatch) -> None:
    # A live orphaned scope (an earlier same-named session left processes
    # running) must not be silently killed: degrade to an uncapped session
    # rather than colliding on the unit name and self-destructing.
    captured: list[list[str]] = []
    _scope_wrap_systemd(monkeypatch, captured)
    monkeypatch.setattr(
        robust, "scope_properties",
        lambda unit, props: {"ActiveState": "active", "LoadState": "loaded"},
    )
    reset_calls: list[str] = []
    monkeypatch.setattr(robust, "reset_scope", lambda unit: reset_calls.append(unit))

    tmux_api.create_or_attach_session("proj")

    # The session's own server still gets its protective bootstrap, but the
    # tmux command it runs is plain (uncapped) -- no systemd-run wrapping of
    # the pane shell, and no destructive reset of the still-live scope.
    bootstrap_cmd = captured[0]
    assert bootstrap_cmd[-6:] == ["new-session", "-d", "-s", "proj", "-c", "/home/me/proj"]
    assert bootstrap_cmd.count("systemd-run") == 1  # only the server bootstrap itself
    assert reset_calls == []


def test_create_reaps_dead_scope_then_caps(monkeypatch) -> None:
    # A dead-but-lingering scope (failed/inactive) is reset to free the name,
    # then the new session is created capped as normal.
    captured: list[list[str]] = []
    _scope_wrap_systemd(monkeypatch, captured)
    monkeypatch.setattr(
        robust, "scope_properties",
        lambda unit, props: {"ActiveState": "failed", "LoadState": "loaded"},
    )
    reset_calls: list[str] = []
    monkeypatch.setattr(robust, "reset_scope", lambda unit: reset_calls.append(unit))

    tmux_api.create_or_attach_session("proj")

    assert reset_calls == ["tmuxctl-proj"]
    bootstrap_cmd = next(c for c in captured if c[:1] == ["systemd-run"])
    wrapped = robust.scope_wrap(["/bin/bash", "-l"], "tmuxctl-proj", "7G", swap="2G")
    tmux_argv = ["new-session", "-d", "-s", "proj", "-c", "/home/me/proj", *wrapped]
    assert bootstrap_cmd[-len(tmux_argv):] == tmux_argv
    # Twice: once for the server bootstrap itself, once nested for the
    # scope-wrapped (capped) pane shell it launches.
    assert bootstrap_cmd.count("systemd-run") == 2


# ---------------------------------------------------------------------------
# §1: pty-durable pane wrapping (dtach)
# ---------------------------------------------------------------------------
def test_login_shell_plain_when_dtach_disabled(monkeypatch) -> None:
    monkeypatch.setattr(robust, "resolve_dtach_wrap", lambda: False)
    monkeypatch.setattr(robust, "dtach_available", lambda: True)
    monkeypatch.setattr(tmux_api.os.environ, "get", lambda k, default=None: "/bin/zsh" if k == "SHELL" else default)
    assert tmux_api._login_shell("proj") == ["/bin/zsh", "-l"]


def test_login_shell_plain_when_dtach_not_installed(monkeypatch) -> None:
    monkeypatch.setattr(robust, "resolve_dtach_wrap", lambda: True)
    monkeypatch.setattr(robust, "dtach_available", lambda: False)
    monkeypatch.setattr(tmux_api.os.environ, "get", lambda k, default=None: "/bin/zsh" if k == "SHELL" else default)
    assert tmux_api._login_shell("proj") == ["/bin/zsh", "-l"]


def test_login_shell_wraps_in_dtach_when_enabled(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(robust, "resolve_dtach_wrap", lambda: True)
    monkeypatch.setattr(robust, "dtach_available", lambda: True)
    monkeypatch.setattr(robust, "dtach_socket_for", lambda name, pane_id="0": tmp_path / f"{name}.sock")
    monkeypatch.setattr(tmux_api.os.environ, "get", lambda k, default=None: "/bin/zsh" if k == "SHELL" else default)

    result = tmux_api._login_shell("proj")

    assert result == robust.dtach_wrap(["/bin/zsh", "-l"], tmp_path / "proj.sock")
    assert result[0] == "dtach"


def test_new_session_command_reattaches_to_surviving_dtach_master(monkeypatch) -> None:
    # The scope is "active" (a dtach master from a previous, now-dead tmux
    # server is still running in it) -- with dtach wrapping on, this must
    # reattach into the EXISTING scope, not hard-fall-back to uncapped like
    # genuine squatting would.
    monkeypatch.setattr(robust, "systemd_available", lambda: True)
    monkeypatch.setattr(tmux_api, "_resolve_session_mem", lambda cwd, flag=None: "7G")
    monkeypatch.setattr(tmux_api, "_resolve_session_swap", lambda cwd: "2G")
    monkeypatch.setattr(
        robust, "scope_properties",
        lambda unit, props: (
            {"ActiveState": "active", "LoadState": "loaded"}
            if set(props) == {"ActiveState", "LoadState"}
            else {"MemoryMax": "7516192768", "MemorySwapMax": "2147483648", "MemoryHigh": "6442450944"}
        ),
    )
    monkeypatch.setattr(robust, "resolve_dtach_wrap", lambda: True)
    monkeypatch.setattr(robust, "scope_occupied_only_by_dtach", lambda unit: True)
    monkeypatch.setattr(robust, "dtach_socket_for", lambda name, pane_id="0": Path(f"/run/tmuxctl/dtach/{name}.sock"))
    reset_calls: list[str] = []
    monkeypatch.setattr(robust, "reset_scope", lambda unit: reset_calls.append(unit))

    argv, resolved = tmux_api._new_session_command("proj", "/home/me/proj", flag=None)

    assert argv == [
        "new-session", "-d", "-s", "proj", "-c", "/home/me/proj",
        "dtach", "-a", "/run/tmuxctl/dtach/proj.sock",
    ]
    assert resolved == {
        "mem": "7516192768", "swap": "2147483648", "high": "6442450944",
        "scope_unit": "tmuxctl-proj.scope",
    }
    # Never treated as squatting: no destructive reset, no uncapped fallback.
    assert reset_calls == []


def test_new_session_command_still_falls_back_for_foreign_squatter(monkeypatch, capsys) -> None:
    # Same "active" scope, but dtach wrapping is on AND the occupant is NOT
    # purely dtach masters -- must still hard-fall-back like before §1.
    monkeypatch.setattr(robust, "systemd_available", lambda: True)
    monkeypatch.setattr(tmux_api, "_resolve_session_mem", lambda cwd, flag=None: "7G")
    monkeypatch.setattr(tmux_api, "_resolve_session_swap", lambda cwd: "2G")
    monkeypatch.setattr(
        robust, "scope_properties",
        lambda unit, props: {"ActiveState": "active", "LoadState": "loaded"},
    )
    monkeypatch.setattr(robust, "resolve_dtach_wrap", lambda: True)
    monkeypatch.setattr(robust, "scope_occupied_only_by_dtach", lambda unit: False)

    argv, resolved = tmux_api._new_session_command("proj", "/home/me/proj", flag=None)

    assert argv == ["new-session", "-d", "-s", "proj", "-c", "/home/me/proj"]
    assert resolved == {"mem": None, "swap": None, "high": None, "scope_unit": None}
    assert "still has live processes" in capsys.readouterr().err


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
    monkeypatch.setattr(tmux_api, "_login_shell", lambda session_name: ["/bin/bash", "-l"])
    _no_stale_scope(monkeypatch)
    monkeypatch.setattr(robust, "reset_session_server_unit", lambda name: None)
    monkeypatch.setattr(
        tmux_api.subprocess, "run",
        lambda args, **k: captured.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )

    tmux_api.create_detached_session("proj", start_dir="/repo")

    bootstrap_cmd = next(c for c in captured if c[:1] == ["systemd-run"])
    wrapped = robust.scope_wrap(["/bin/bash", "-l"], "tmuxctl-proj", "30G", swap="6G")
    tmux_argv = ["new-session", "-d", "-s", "proj", "-c", "/repo", *wrapped]
    assert bootstrap_cmd[-len(tmux_argv):] == tmux_argv
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
    monkeypatch.setattr(tmux_api, "_login_shell", lambda session_name: ["/bin/bash", "-l"])
    monkeypatch.setattr(tmux_api.os, "getcwd", lambda: "/work/dir")
    _no_stale_scope(monkeypatch)
    monkeypatch.setattr(robust, "reset_session_server_unit", lambda name: None)
    monkeypatch.setattr(
        tmux_api.subprocess, "run",
        lambda args, **k: captured.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )

    tmux_api.create_detached_session("proj", mem="8G")

    bootstrap_cmd = next(c for c in captured if c[:1] == ["systemd-run"])
    wrapped = robust.scope_wrap(["/bin/bash", "-l"], "tmuxctl-proj", "8G", swap="6G")
    assert bootstrap_cmd[-len(wrapped):] == wrapped


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
    order: list[str] = []
    monkeypatch.setattr(robust, "reset_server_unit", lambda: order.append("reset"))
    calls: list[list[str]] = []
    monkeypatch.setattr(
        tmux_api.subprocess, "run",
        lambda args, **k: order.append("bootstrap") or calls.append(args)
        or subprocess.CompletedProcess(args, 0, "", ""),
    )

    tmux_api.ensure_server()

    # Parent slice bound first, then the server bootstrapped in its own unit.
    assert ensured == [("40G", "12G")]
    assert calls == [robust.server_bootstrap_argv()]
    # A dead leftover unit is reaped BEFORE the bootstrap reuses the name.
    assert order == ["reset", "bootstrap"]


def test_ensure_server_warns_when_bootstrap_fails(monkeypatch, capsys) -> None:
    monkeypatch.setattr(robust, "systemd_available", lambda: True)
    monkeypatch.setattr(tmux_api, "_server_running", lambda: False)
    monkeypatch.setattr(robust, "resolve_slice_max", lambda: "40G")
    monkeypatch.setattr(robust, "resolve_slice_swap_max", lambda: "12G")
    monkeypatch.setattr(robust, "ensure_slice", lambda slice_max, *, swap_max=None: True)
    monkeypatch.setattr(robust, "reset_server_unit", lambda: None)
    monkeypatch.setattr(
        tmux_api.subprocess, "run",
        lambda args, **k: subprocess.CompletedProcess(args, 1, "", "unit already loaded"),
    )

    tmux_api.ensure_server()

    err = capsys.readouterr().err
    assert "could not bootstrap" in err
    assert "unit already loaded" in err


def test_create_detached_resets_stale_server_unit_before_bootstrapping(monkeypatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(tmux_api, "session_exists", lambda name: False)
    monkeypatch.setattr(robust, "systemd_available", lambda: True)
    monkeypatch.setattr(
        tmux_api, "_new_session_command",
        lambda name, cwd, *, flag: (["new-session", "-d", "-s", name], {}),
    )
    monkeypatch.setattr(
        robust, "reset_session_server_unit",
        lambda name: order.append("reset"),
    )
    monkeypatch.setattr(
        tmux_api.subprocess, "run",
        lambda args, **k: order.append("bootstrap") or subprocess.CompletedProcess(args, 0, "", ""),
    )

    tmux_api.create_detached_session("proj")

    # A dead leftover per-session server unit is reaped BEFORE the bootstrap
    # reuses the name, exactly like the legacy shared-server path.
    assert order == ["reset", "bootstrap"]


# --- Issue #1170: create-detached must not hang when the spawned server ---
# --- inherits and holds the caller's stdout (the SSH exec-channel fd leak). ---


def _fake_tmux_that_leaks_stdout(tmp_path, sleep_seconds: float) -> Path:
    """A stand-in ``tmux`` reproducing the #1170 fd leak: the ``new-session``
    forks a background 'server' that INHERITS and holds the caller's stdout open
    for ``sleep_seconds`` (so a pipe read blocks that long), prints a diagnostic
    to stderr, then the foreground command exits 0."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tmux = bindir / "tmux"
    tmux.write_text(
        "#!/bin/sh\n"
        f"sleep {sleep_seconds} &\n"       # the 'server' holding inherited fds
        "echo 'boot diagnostic' >&2\n"     # real tmux-side stderr to preserve
        "exit 0\n"
    )
    tmux.chmod(0o755)
    return bindir


def test_run_tmux_detached_stdio_returns_promptly_and_keeps_stderr(monkeypatch, tmp_path) -> None:
    # With the child's stdout detached to /dev/null, an inherited server holding
    # stdout can NOT stall the parent: subprocess never reads a held pipe, so the
    # foreground new-session exits and _run_tmux returns at once — while tmux's own
    # stderr is still captured to the temp file for error reporting.
    import time

    bindir = _fake_tmux_that_leaks_stdout(tmp_path, sleep_seconds=3)
    monkeypatch.setenv("PATH", f"{bindir}:{__import__('os').environ['PATH']}")

    start = time.monotonic()
    result = tmux_api._run_tmux(
        ["new-session", "-d", "-s", "proj"], check=False, detach_child_stdio=True
    )
    elapsed = time.monotonic() - start

    assert result.returncode == 0
    assert elapsed < 1.5, f"detached create hung {elapsed:.2f}s on the daemon-held fd"
    assert "boot diagnostic" in result.stderr  # real stderr preserved, not swallowed


def test_run_tmux_capture_output_hangs_on_the_held_fd(monkeypatch, tmp_path) -> None:
    # Control: the OLD path (capture_output=True) DOES block on the held stdout
    # pipe — proving the fake genuinely reproduces the #1170 leak and that the
    # detached path above is what fixes it. A short timeout fires because the read
    # is still parked on the daemon-held pipe.
    bindir = _fake_tmux_that_leaks_stdout(tmp_path, sleep_seconds=5)
    monkeypatch.setenv("PATH", f"{bindir}:{__import__('os').environ['PATH']}")

    with pytest.raises(tmux_api.TmuxCommandError):
        tmux_api._run_tmux(["new-session", "-d", "-s", "proj"], check=False, timeout=1)


def test_create_detached_uses_detached_child_stdio(monkeypatch) -> None:
    # The create-detached verb must route its create through the detached-stdio
    # path so a freshly-spawned server can never hold the caller's exec channel.
    # (No systemd here: that's the branch that forks tmux directly instead of
    # going through systemd-run, which is what issue #1170 was about.)
    monkeypatch.setattr(tmux_api, "session_exists", lambda name: False)
    monkeypatch.setattr(robust, "systemd_available", lambda: False)
    monkeypatch.setattr(
        tmux_api, "_new_session_command",
        lambda name, cwd, *, flag: (["new-session", "-d", "-s", name], {}),
    )
    seen_kwargs: list[dict] = []

    def fake_run_tmux(args, **kwargs):
        seen_kwargs.append(kwargs)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tmux_api, "_run_tmux", fake_run_tmux)

    tmux_api.create_detached_session("proj")

    assert seen_kwargs and seen_kwargs[0].get("detach_child_stdio") is True


def test_kill_session_stops_scope(monkeypatch) -> None:
    monkeypatch.setattr(tmux_api, "locate_session", lambda name: robust.socket_for("proj"))
    monkeypatch.setattr(
        tmux_api, "_run_tmux",
        lambda args, **k: subprocess.CompletedProcess(args, 0, "", ""),
    )

    stopped: list[str] = []
    monkeypatch.setattr(robust, "stop_scope", lambda unit: stopped.append(unit))
    stopped_servers: list[str] = []
    monkeypatch.setattr(
        robust, "stop_session_server", lambda name: stopped_servers.append(name)
    )

    tmux_api.kill_session("proj")

    assert stopped == ["tmuxctl-proj"]
    # The session's own dedicated server unit is stopped too (best-effort;
    # harmless no-op for a legacy shared-server session that never had one).
    assert stopped_servers == ["proj"]


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

    def fake_run_tmux(args, *, socket=None, check=True, timeout=None):
        captured["args"] = args
        captured["socket"] = socket
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tmux_api, "_run_tmux", fake_run_tmux)

    assert tmux_api.session_exists("git-pocketshell") is True
    # Leading '=' forces an exact match so a prefix like "git-pocketshell"
    # never matches an existing "git-pocketshell-desktop".
    assert captured["args"] == ["has-session", "-t", "=git-pocketshell"]
    # Resolved against this session's own dedicated socket first (§0).
    assert captured["socket"] == robust.socket_for("git-pocketshell")


def test_session_panes_parses_list_panes(monkeypatch) -> None:
    monkeypatch.setattr(tmux_api, "locate_session", lambda name: robust.socket_for("proj"))

    stdout = (
        "0::0::111::nvim::/home/a/proj::1\n"
        "1::0::222::python::/home/a/proj/sub::0\n"
    )

    def fake_run_tmux(args, *, socket=None, check=True, timeout=None):
        assert args[0] == "list-panes"
        assert "-s" in args
        assert socket == robust.socket_for("proj")
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
    monkeypatch.setattr(tmux_api, "locate_session", lambda name: None)
    raised = False
    try:
        tmux_api.session_panes("ghost")
    except tmux_api.TmuxSessionNotFoundError:
        raised = True
    assert raised


def test_list_session_info_parses_list_sessions(monkeypatch) -> None:
    stdout = "repro::1787570221::1787570300\n"
    monkeypatch.setattr(tmux_api, "_tmuxctl_socket_paths", lambda: ["/tmp/tmux-1000/default"])

    def fake_run_tmux(args, *, socket=None, check=True, timeout=None):
        assert args[:3] == [
            "list-sessions", "-F", "#{session_name}::#{session_created}::#{session_activity}",
        ]
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(tmux_api, "_run_tmux", fake_run_tmux)

    sessions = tmux_api.list_session_info()
    assert len(sessions) == 1
    assert sessions[0].name == "repro"
    assert sessions[0].created_at == 1787570221
    assert sessions[0].activity_at == 1787570300


@pytest.mark.skipif(shutil.which("tmux") is None, reason="requires a real tmux binary")
def test_list_session_info_and_session_panes_survive_real_tmux(tmp_path) -> None:
    """Issue #6: tmux rewrites literal tabs in -F output to underscores, so a
    tab-delimited format silently mangles into one unsplittable field. A
    synthesised fixture string can't catch that -- the mangling happens
    inside tmux itself -- so this drives the real binary. Also exercises §0:
    a plain session with no -S/-L lands on the legacy default socket, which
    list_session_info()/session_panes() must still find via their fallback."""
    name = f"tmuxctl-issue6-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "-c", str(tmp_path)],
        check=True,
    )
    try:
        sessions = tmux_api.list_session_info()
        matches = [s for s in sessions if s.name == name]
        assert len(matches) == 1
        assert matches[0].created_at > 0
        assert matches[0].activity_at > 0

        panes = tmux_api.session_panes(name)
        assert len(panes) == 1
        assert panes[0].cwd == str(tmp_path)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False)


def test_process_cgroup_reads_unified_line(monkeypatch, tmp_path) -> None:
    cgroup_file = tmp_path / "cgroup"
    cgroup_file.write_text(
        "0::/user.slice/user-1000.slice/session-9.scope\n", encoding="utf-8"
    )
    monkeypatch.setattr(tmux_api, "Path", lambda p: cgroup_file)
    assert tmux_api.process_cgroup(1234) == "/user.slice/user-1000.slice/session-9.scope"

    cgroup_file.write_text("", encoding="utf-8")
    assert tmux_api.process_cgroup(1234) is None
