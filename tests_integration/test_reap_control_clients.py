"""Integration test: reap orphan tmux ``-CC`` control-mode clients.

The server-side safety net for pocketshell #1123 item 7 / the #215 orphan class.
The PocketShell app attaches over tmux ``-CC`` control mode and is meant to hold
exactly ONE control-mode client per session, detaching it cleanly on exit. An
app crash / force-kill strands that control client attached forever; only this
net reaps the stale duplicate.

This drives REAL ``tmux`` on an ISOLATED socket (an explicit ``-S`` path under a
pytest tmp dir — NEVER the default socket, and outside the ``/tmp/tmux-<uid>/``
glob the production scan walks, so it can never disturb a live session). Like
the other ``tests_integration/`` tests it lives outside the default ``testpaths``
and self-skips where tmux is unavailable.
"""

from __future__ import annotations

import os
import pty
import shutil
import subprocess
import time

import pytest

from tmuxctl import strays


pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="tmux is not installed",
)


def _tmux(sock: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-S", sock, *args],
        capture_output=True,
        text=True,
        timeout=15,
    )


def _control_client_count(sock: str) -> int:
    raw = strays.list_clients_raw(sock)
    return sum(1 for c in strays.parse_clients(sock, raw) if c.control_mode)


def _attach_control_client(sock: str) -> tuple[subprocess.Popen, int]:
    """Attach a tmux ``-CC`` control-mode client over a real pty.

    Control mode still calls ``tcgetattr`` on its stdin, so it must run on a pty
    (a plain pipe fails with "Inappropriate ioctl for device" and the client
    exits) — exactly how iTerm2 / PocketShell drive it. We keep the master fd
    open so the client stays attached until we tear it down.
    """
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        ["tmux", "-S", sock, "-CC", "attach-session", "-t", "work"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
    )
    os.close(slave)  # the child owns it now
    return proc, master


def test_reap_orphan_control_client_leaves_one_and_session_survives(tmp_path) -> None:
    sock = str(tmp_path / "ctl.sock")
    clients: list[tuple[subprocess.Popen, int]] = []
    try:
        # A real detached session on the isolated socket.
        assert _tmux(sock, "new-session", "-d", "-s", "work", "sleep 600").returncode == 0

        # Two -CC control-mode clients attach to it (the orphan + the live app).
        clients.append(_attach_control_client(sock))
        time.sleep(1.5)  # distinct client_activity second for the second client
        clients.append(_attach_control_client(sock))

        # Wait for both control clients to register.
        deadline = time.time() + 10
        while time.time() < deadline and _control_client_count(sock) < 2:
            time.sleep(0.2)
        assert _control_client_count(sock) == 2, "precondition: two control clients"

        # The reaper selects the stale duplicate(s) — exactly one orphan here —
        # and detaches them, keeping the most-recently-active control client.
        raw = strays.list_clients_raw(sock)
        parsed = strays.parse_clients(sock, raw)
        orphans = strays.select_orphan_control_clients(parsed)
        assert len(orphans) == 1, f"expected one orphan, saw {orphans!r}"

        for orphan in orphans:
            assert orphan.control_mode is True
            assert strays.detach_client(sock, orphan.target) is True

        # One control client remains and the SESSION survives the detach.
        deadline = time.time() + 10
        while time.time() < deadline and _control_client_count(sock) != 1:
            time.sleep(0.2)
        assert _control_client_count(sock) == 1, "exactly one control client must remain"

        sessions = _tmux(sock, "list-sessions", "-F", "#{session_name}").stdout.split()
        assert "work" in sessions, f"session must survive the detach; saw {sessions!r}"
    finally:
        for proc, master in clients:
            proc.terminate()
            try:
                os.close(master)
            except OSError:
                pass
        _tmux(sock, "kill-server")
