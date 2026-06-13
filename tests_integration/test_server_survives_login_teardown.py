"""Integration test: the tmux server survives an SSH login teardown.

Regression net for the 2026-06-13 "all sessions gone" wipe. Root cause: the
shared tmux server inherits the cgroup of whichever SSH login first spawns it
(``session-<N>.scope``, managed by logind). When that login logs out, systemd
reaps the session scope and the server dies inside it, taking *every* session on
the server down at once — even sessions created by other logins. It was
misdiagnosed as an OOM cascade; the per-session memory caps were never at fault.

This exercises real ``systemd-run --user`` scopes + cgroup v2 on an ISOLATED
tmux socket (never the default socket), so it only runs where user scopes work
and never disturbs a real session. Like ``test_oom_integration.py`` it lives in
``tests_integration/`` (outside the default ``testpaths``) and runs on CI via
``make test-integration``.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from tmuxctl import robust


def _user_scopes_work() -> bool:
    if not robust.systemd_available():
        return False
    try:
        probe = subprocess.run(
            robust.scope_wrap(["true"], f"tmuxctl-srvprobe-{os.getpid()}", "64M", swap="0"),
            capture_output=True,
            timeout=15,
        )
        return probe.returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _user_scopes_work(),
    reason="systemd --user scopes unavailable (e.g. CI without a user systemd bus / cgroup v2)",
)


def _tmux(sock: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-L", sock, *args], capture_output=True, text=True, timeout=15
    )


def _server_up(sock: str) -> bool:
    return _tmux(sock, "list-sessions").returncode == 0


def _isolated_bootstrap_argv(sock: str, unit: str) -> list[str]:
    """Production ``server_bootstrap_argv`` retargeted at an isolated socket."""
    argv = robust.server_bootstrap_argv(unit)
    i = argv.index("tmux")
    return argv[: i + 1] + ["-L", sock] + argv[i + 1 :]


def test_server_in_login_scope_dies_with_it() -> None:
    """NEGATIVE control: a server living in a transient (login-like) scope is
    wiped — with all its sessions — when that scope is stopped. This is the bug.
    """
    sock = f"tmuxctl-it-bug-{os.getpid()}"
    login = f"tmuxctl-it-login-bug-{os.getpid()}"
    proc: subprocess.Popen | None = None
    try:
        # Start the server INSIDE a transient scope (mimics the buggy placement).
        proc = subprocess.Popen(
            [
                "systemd-run", "--user", "--scope", f"--unit={login}",
                "-p", "Slice=robust.slice", "--quiet", "--",
                "bash", "-c",
                f"tmux -L {sock} new-session -d -s loginA 'sleep 600'; exec sleep 600",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(2)
        # A second login adds its own session to the SAME server.
        _tmux(sock, "new-session", "-d", "-s", "loginB", "sleep 600")
        time.sleep(0.5)
        assert _server_up(sock), "precondition: server should be up with two sessions"

        # 'Login A' logs out: stop the scope the server lives in.
        subprocess.run(
            ["systemctl", "--user", "stop", f"{login}.scope"],
            capture_output=True, timeout=15,
        )
        time.sleep(2)
        assert not _server_up(sock), (
            "server (and loginB's session) should have been wiped when the "
            "login scope it lived in was stopped"
        )
    finally:
        _tmux(sock, "kill-server")
        subprocess.run(["systemctl", "--user", "stop", f"{login}.scope"], capture_output=True)
        if proc is not None:
            proc.terminate()


def test_server_in_own_unit_survives_login_teardown() -> None:
    """THE FIX: a server bootstrapped via ``server_bootstrap_argv`` (its own unit
    under robust.slice) survives a login teardown — server AND every session.
    """
    sock = f"tmuxctl-it-fix-{os.getpid()}"
    unit = f"tmuxctl-server-it-{os.getpid()}"
    login = f"tmuxctl-it-login-fix-{os.getpid()}"
    try:
        # Server in its OWN forking unit (Type=forking returns once daemonized).
        subprocess.run(_isolated_bootstrap_argv(sock, unit), capture_output=True, timeout=20)
        time.sleep(1.5)
        assert (
            subprocess.run(
                ["systemctl", "--user", "is-active", f"{unit}.service"],
                capture_output=True, text=True,
            ).stdout.strip()
            == "active"
        ), "server unit should be active after bootstrap"

        # A login connects and creates a session on the existing server.
        proc = subprocess.Popen(
            [
                "systemd-run", "--user", "--scope", f"--unit={login}",
                "-p", "Slice=robust.slice", "--quiet", "--",
                "bash", "-c",
                f"tmux -L {sock} new-session -d -s work 'sleep 600'; exec sleep 600",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(2)
        assert _server_up(sock), "precondition: server up with the login's session"

        # That login logs out.
        subprocess.run(
            ["systemctl", "--user", "stop", f"{login}.scope"],
            capture_output=True, timeout=15,
        )
        time.sleep(2)

        # Server survives, and the session survives (its pane lives in the
        # server's cgroup, not the login's scope — only the client disconnected).
        assert _server_up(sock), "server must survive the login teardown"
        sessions = _tmux(sock, "list-sessions", "-F", "#{session_name}").stdout.split()
        assert "work" in sessions, f"session must survive; saw {sessions!r}"
        proc.terminate()
    finally:
        _tmux(sock, "kill-server")
        subprocess.run(["systemctl", "--user", "stop", f"{unit}.service"], capture_output=True)
        subprocess.run(["systemctl", "--user", "stop", f"{login}.scope"], capture_output=True)
