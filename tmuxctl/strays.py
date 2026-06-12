"""System-wide tmux socket scanning for stray / orphaned sessions and servers.

Used by ``t strays`` / ``t reap`` / ``t doctor``. Scans ALL tmux sockets the
current user can reach (``/tmp/tmux-<uid>/*`` plus the default socket), lists
their sessions, flags dead socket files, and detects orphaned tmux server
processes whose pid is not the ``#{pid}`` of any reachable socket.
"""

from __future__ import annotations

import glob
import os
import subprocess
import time
from dataclasses import dataclass, field


@dataclass(slots=True)
class StraySession:
    socket: str
    name: str
    activity_at: int
    attached: bool
    windows: int

    def idle_days(self, now: float | None = None) -> float:
        ref = time.time() if now is None else now
        return max(0.0, (ref - self.activity_at) / 86400.0)


@dataclass(slots=True)
class SocketScan:
    socket: str
    reachable: bool
    is_default: bool
    pid: int | None = None
    sessions: list[StraySession] = field(default_factory=list)

    @property
    def has_attached(self) -> bool:
        return any(s.attached for s in self.sessions)


@dataclass(slots=True)
class StrayReport:
    scans: list[SocketScan]
    dead_sockets: list[str]
    orphan_pids: list[int]


def _uid() -> int:
    return os.getuid()


def default_socket(uid: int | None = None) -> str:
    uid = _uid() if uid is None else uid
    tmpdir = os.environ.get("TMUX_TMPDIR") or "/tmp"
    return f"{tmpdir}/tmux-{uid}/default"


def list_socket_paths(uid: int | None = None) -> list[str]:
    """All candidate ``-S`` socket paths: every file in /tmp/tmux-<uid>/ + default."""
    uid = _uid() if uid is None else uid
    tmpdir = os.environ.get("TMUX_TMPDIR") or "/tmp"
    paths = sorted(glob.glob(f"{tmpdir}/tmux-{uid}/*"))
    default = default_socket(uid)
    if default not in paths:
        paths.append(default)
    return paths


def _run(args: list[str], timeout: int = 5) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


_LIST_FORMAT = "#{session_name}|#{session_activity}|#{session_attached}|#{session_windows}"


def parse_sessions(socket: str, stdout: str) -> list[StraySession]:
    sessions: list[StraySession] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 4:
            continue
        name, activity, attached, windows = parts[0], parts[1], parts[2], parts[3]
        try:
            activity_at = int(activity)
        except ValueError:
            activity_at = 0
        try:
            window_count = int(windows)
        except ValueError:
            window_count = 0
        sessions.append(
            StraySession(
                socket=socket,
                name=name,
                activity_at=activity_at,
                attached=attached not in ("0", ""),
                windows=window_count,
            )
        )
    return sessions


def scan_socket(socket: str, *, default: str) -> SocketScan:
    """Probe a single socket: reachable? sessions? server pid?"""
    is_default = socket == default
    try:
        result = _run(
            ["tmux", "-S", socket, "list-sessions", "-F", _LIST_FORMAT]
        )
    except (OSError, subprocess.TimeoutExpired):
        return SocketScan(socket=socket, reachable=False, is_default=is_default)

    if result.returncode != 0:
        return SocketScan(socket=socket, reachable=False, is_default=is_default)

    sessions = parse_sessions(socket, result.stdout)
    pid = _server_pid(socket)
    return SocketScan(
        socket=socket,
        reachable=True,
        is_default=is_default,
        pid=pid,
        sessions=sessions,
    )


def _server_pid(socket: str) -> int | None:
    try:
        result = _run(
            ["tmux", "-S", socket, "display-message", "-p", "#{pid}"]
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.strip()
    try:
        return int(text)
    except ValueError:
        return None


def list_tmux_server_pids(uid: int | None = None) -> list[int]:
    """All tmux *server* process pids for the user (``pgrep -u <uid> -x tmux``)."""
    uid = _uid() if uid is None else uid
    try:
        result = _run(["pgrep", "-u", str(uid), "-x", "tmux"])
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    pids: list[int] = []
    for line in result.stdout.split():
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def scan_all(uid: int | None = None) -> StrayReport:
    """Full system-wide scan across every reachable tmux socket for the user."""
    uid = _uid() if uid is None else uid
    default = default_socket(uid)
    scans: list[SocketScan] = []
    dead_sockets: list[str] = []

    for socket in list_socket_paths(uid):
        scan = scan_socket(socket, default=default)
        scans.append(scan)
        # A socket FILE that exists but whose server is unreachable is dead.
        # (The synthesized default path may not exist on disk; only flag real files.)
        if not scan.reachable and os.path.exists(socket):
            dead_sockets.append(socket)

    reachable_pids = {scan.pid for scan in scans if scan.reachable and scan.pid}
    orphan_pids = [
        pid for pid in list_tmux_server_pids(uid) if pid not in reachable_pids
    ]

    return StrayReport(
        scans=scans,
        dead_sockets=dead_sockets,
        orphan_pids=sorted(orphan_pids),
    )
