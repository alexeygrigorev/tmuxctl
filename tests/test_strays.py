from __future__ import annotations

import subprocess

from tmuxctl import strays


def _cp(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, "")


# ---------------------------------------------------------------------------
# parse_sessions
# ---------------------------------------------------------------------------
def test_parse_sessions() -> None:
    out = "main|1700000000|1|3\nbuild|1699000000|0|1\n"
    sessions = strays.parse_sessions("/tmp/tmux-1000/default", out)
    assert len(sessions) == 2
    assert sessions[0].name == "main"
    assert sessions[0].attached is True
    assert sessions[0].windows == 3
    assert sessions[0].activity_at == 1700000000
    assert sessions[1].name == "build"
    assert sessions[1].attached is False


def test_stray_session_idle_days() -> None:
    s = strays.StraySession(
        socket="x", name="a", activity_at=1000, attached=False, windows=1
    )
    # now = activity + 3 days
    assert round(s.idle_days(now=1000 + 3 * 86400)) == 3
    # never negative
    assert s.idle_days(now=500) == 0.0


# ---------------------------------------------------------------------------
# scan_socket
# ---------------------------------------------------------------------------
def test_scan_socket_reachable(monkeypatch) -> None:
    def fake_run(args, timeout=5):
        if "list-sessions" in args:
            return _cp("main|1700000000|1|2\n")
        if "display-message" in args:
            return _cp("12345\n")
        return _cp("", 1)

    monkeypatch.setattr(strays, "_run", fake_run)
    scan = strays.scan_socket("/tmp/tmux-1000/default", default="/tmp/tmux-1000/default")
    assert scan.reachable is True
    assert scan.is_default is True
    assert scan.pid == 12345
    assert scan.has_attached is True
    assert scan.sessions[0].name == "main"


def test_scan_socket_dead(monkeypatch) -> None:
    monkeypatch.setattr(strays, "_run", lambda args, timeout=5: _cp("", 1))
    scan = strays.scan_socket("/tmp/tmux-1000/oldsock", default="/tmp/tmux-1000/default")
    assert scan.reachable is False
    assert scan.sessions == []


# ---------------------------------------------------------------------------
# scan_all: dead sockets + orphan detection
# ---------------------------------------------------------------------------
def test_scan_all_flags_dead_sockets_and_orphans(monkeypatch, tmp_path) -> None:
    live_sock = str(tmp_path / "default")
    dead_sock = str(tmp_path / "deadsock")
    # Both socket files exist on disk.
    (tmp_path / "default").write_text("", encoding="utf-8")
    (tmp_path / "deadsock").write_text("", encoding="utf-8")

    monkeypatch.setattr(strays, "list_socket_paths", lambda uid=None: [live_sock, dead_sock])
    monkeypatch.setattr(strays, "default_socket", lambda uid=None: live_sock)

    def fake_scan(socket, *, default):
        if socket == live_sock:
            return strays.SocketScan(
                socket=socket,
                reachable=True,
                is_default=True,
                pid=111,
                sessions=[
                    strays.StraySession(
                        socket=socket, name="main", activity_at=0,
                        attached=True, windows=1,
                    )
                ],
            )
        return strays.SocketScan(socket=socket, reachable=False, is_default=False)

    monkeypatch.setattr(strays, "scan_socket", fake_scan)
    # Two server pids: 111 is the live socket, 999 is an orphan.
    monkeypatch.setattr(strays, "list_tmux_server_pids", lambda uid=None: [111, 999])

    report = strays.scan_all(uid=1000)
    assert report.dead_sockets == [dead_sock]
    assert report.orphan_pids == [999]
    assert report.scans[0].has_attached is True


def test_list_tmux_server_pids(monkeypatch) -> None:
    monkeypatch.setattr(strays, "_run", lambda args, timeout=5: _cp("100\n200\n"))
    assert strays.list_tmux_server_pids(uid=1000) == [100, 200]


def test_list_tmux_server_pids_none(monkeypatch) -> None:
    monkeypatch.setattr(strays, "_run", lambda args, timeout=5: _cp("", 1))
    assert strays.list_tmux_server_pids(uid=1000) == []
