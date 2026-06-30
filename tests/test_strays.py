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


# ---------------------------------------------------------------------------
# orphan control-mode client detection (#215 / pocketshell #1123 item 7)
# ---------------------------------------------------------------------------
SOCK = "/tmp/tmux-1000/default"


def _client(
    name: str,
    *,
    session: str = "work",
    activity_at: int = 0,
    control_mode: bool = True,
    tty: str = "",
    socket: str = SOCK,
) -> strays.ControlClient:
    return strays.ControlClient(
        socket=socket,
        name=name,
        tty=tty or name,
        session=session,
        activity_at=activity_at,
        control_mode=control_mode,
    )


def test_parse_clients() -> None:
    out = (
        "/dev/pts/3|/dev/pts/3|work|1700000000|1\n"
        "/dev/pts/9|/dev/pts/9|work|1699000000|1\n"
        "/dev/pts/1|/dev/pts/1|work|1700000500|0\n"
    )
    clients = strays.parse_clients(SOCK, out)
    assert len(clients) == 3
    assert clients[0].name == "/dev/pts/3"
    assert clients[0].session == "work"
    assert clients[0].activity_at == 1700000000
    assert clients[0].control_mode is True
    # The third client is NOT control mode.
    assert clients[2].control_mode is False
    # target falls back to tty (== name here).
    assert clients[0].target == "/dev/pts/3"


def test_parse_clients_skips_blank_and_short_lines() -> None:
    out = "\n/dev/pts/3|/dev/pts/3|work|1700000000|1\nbroken|line\n"
    clients = strays.parse_clients(SOCK, out)
    assert len(clients) == 1
    assert clients[0].name == "/dev/pts/3"


def test_parse_clients_bad_activity_defaults_zero() -> None:
    clients = strays.parse_clients(SOCK, "/dev/pts/3|/dev/pts/3|work|nan|1\n")
    assert clients[0].activity_at == 0


def test_select_picks_older_duplicate_keeps_newest() -> None:
    # Two control clients on one session: the older one is the orphan.
    newest = _client("/dev/pts/3", activity_at=2000)
    older = _client("/dev/pts/9", activity_at=1000)
    orphans = strays.select_orphan_control_clients([newest, older])
    assert [o.name for o in orphans] == ["/dev/pts/9"]


def test_select_spares_sole_control_client() -> None:
    # A single (healthy) control client per session must never be detached.
    only = _client("/dev/pts/3", activity_at=1000)
    assert strays.select_orphan_control_clients([only]) == []


def test_select_spares_non_control_clients() -> None:
    # A control client + an interactive (non-control) shell on the SAME session:
    # the interactive client must never be touched, and a single control client
    # is not a duplicate -> nothing to detach.
    ctrl = _client("/dev/pts/3", activity_at=1000, control_mode=True)
    shell = _client("/dev/pts/1", activity_at=500, control_mode=False)
    assert strays.select_orphan_control_clients([ctrl, shell]) == []


def test_select_never_returns_non_control_even_among_duplicates() -> None:
    # Two control clients (one orphan) PLUS a non-control shell: only the older
    # control client is returned; the non-control shell is always spared.
    newest = _client("/dev/pts/3", activity_at=2000, control_mode=True)
    older = _client("/dev/pts/9", activity_at=1000, control_mode=True)
    shell = _client("/dev/pts/1", activity_at=100, control_mode=False)
    orphans = strays.select_orphan_control_clients([newest, older, shell])
    assert [o.name for o in orphans] == ["/dev/pts/9"]
    assert all(o.control_mode for o in orphans)


def test_select_three_control_clients_keeps_only_newest() -> None:
    a = _client("/dev/pts/3", activity_at=3000)
    b = _client("/dev/pts/9", activity_at=1000)
    c = _client("/dev/pts/7", activity_at=2000)
    orphans = strays.select_orphan_control_clients([a, b, c])
    # Keeps the newest (a); both older ones are orphans.
    assert sorted(o.name for o in orphans) == ["/dev/pts/7", "/dev/pts/9"]


def test_select_groups_per_session() -> None:
    # Duplicate control clients on session A; single control client on session B.
    a_new = _client("/dev/pts/3", session="A", activity_at=2000)
    a_old = _client("/dev/pts/9", session="A", activity_at=1000)
    b_only = _client("/dev/pts/5", session="B", activity_at=500)
    orphans = strays.select_orphan_control_clients([a_new, a_old, b_only])
    # Only session A's older duplicate is reaped; session B is untouched.
    assert [o.name for o in orphans] == ["/dev/pts/9"]


def test_select_groups_per_socket() -> None:
    # Same session NAME but different sockets must NOT be merged into one group.
    s1 = _client("/dev/pts/3", session="work", activity_at=1000, socket="/sock/a")
    s2 = _client("/dev/pts/9", session="work", activity_at=2000, socket="/sock/b")
    assert strays.select_orphan_control_clients([s1, s2]) == []


def test_select_equal_activity_detaches_all_but_one() -> None:
    a = _client("/dev/pts/3", activity_at=1000)
    b = _client("/dev/pts/9", activity_at=1000)
    orphans = strays.select_orphan_control_clients([a, b])
    # Exactly one survives even when activity ties.
    assert len(orphans) == 1


def test_scan_orphan_control_clients_across_sockets(monkeypatch) -> None:
    sock = "/tmp/tmux-1000/default"
    monkeypatch.setattr(strays, "list_socket_paths", lambda uid=None: [sock])

    def fake_raw(socket: str) -> str:
        assert socket == sock
        return (
            "/dev/pts/3|/dev/pts/3|work|2000|1\n"
            "/dev/pts/9|/dev/pts/9|work|1000|1\n"
        )

    monkeypatch.setattr(strays, "list_clients_raw", fake_raw)
    orphans = strays.scan_orphan_control_clients(uid=1000)
    assert [o.name for o in orphans] == ["/dev/pts/9"]


def test_scan_orphan_control_clients_no_server(monkeypatch) -> None:
    monkeypatch.setattr(strays, "list_socket_paths", lambda uid=None: ["/x"])
    monkeypatch.setattr(strays, "list_clients_raw", lambda socket: "")
    assert strays.scan_orphan_control_clients(uid=1000) == []


def test_list_clients_raw_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(strays, "_run", lambda args, timeout=5: _cp("", 1))
    assert strays.list_clients_raw(SOCK) == ""


def test_detach_client_runs_detach_not_kill(monkeypatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(args, timeout=5):
        captured["args"] = args
        return _cp("", 0)

    monkeypatch.setattr(strays, "_run", fake_run)
    assert strays.detach_client(SOCK, "/dev/pts/9") is True
    # Detaches the client, never kills the server/session.
    assert captured["args"] == [
        "tmux", "-S", SOCK, "detach-client", "-t", "/dev/pts/9"
    ]
    assert "kill-server" not in captured["args"]
    assert "kill-session" not in captured["args"]


def test_control_client_idle_days() -> None:
    c = _client("/dev/pts/9", activity_at=1000)
    assert round(c.idle_days(now=1000 + 2 * 86400)) == 2
    assert c.idle_days(now=500) == 0.0
