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


@dataclass(slots=True)
class ControlClient:
    """A tmux client attached to a session, as reported by ``list-clients``.

    The PocketShell app attaches over tmux ``-CC`` control mode and is supposed
    to hold exactly ONE control-mode client per session, detaching it cleanly on
    exit (``TmuxClient.detachCleanly``). An app crash / force-kill can leave the
    control client attached forever — the #215 orphan class — and only this
    server-side net reaps it.
    """

    socket: str
    name: str
    tty: str
    session: str
    activity_at: int
    control_mode: bool

    @property
    def target(self) -> str:
        """The ``detach-client -t`` target (tmux matches name or tty)."""
        return self.tty or self.name

    def idle_days(self, now: float | None = None) -> float:
        ref = time.time() if now is None else now
        return max(0.0, (ref - self.activity_at) / 86400.0)


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


# ``list-clients`` keys evaluated in the client context: the client identifier
# (name + tty), the session it is attached to, its last-activity epoch, and the
# control-mode flag (``1`` for a tmux ``-CC`` client).
_CLIENT_FORMAT = (
    "#{client_name}|#{client_tty}|#{session_name}|"
    "#{client_activity}|#{client_control_mode}"
)


def parse_clients(socket: str, stdout: str) -> list[ControlClient]:
    """Parse ``list-clients -F _CLIENT_FORMAT`` output into ``ControlClient``s."""
    clients: list[ControlClient] = []
    for raw in stdout.splitlines():
        if not raw.strip():
            continue
        parts = raw.split("|")
        if len(parts) < 5:
            continue
        name, tty, session, activity, control = parts[:5]
        try:
            activity_at = int(activity)
        except ValueError:
            activity_at = 0
        clients.append(
            ControlClient(
                socket=socket,
                name=name,
                tty=tty,
                session=session,
                activity_at=activity_at,
                control_mode=control == "1",
            )
        )
    return clients


def list_clients_raw(socket: str) -> str:
    """Raw ``tmux -S <socket> list-clients`` output, or '' if unreachable."""
    try:
        result = _run(
            ["tmux", "-S", socket, "list-clients", "-F", _CLIENT_FORMAT]
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def select_orphan_control_clients(
    clients: list[ControlClient],
) -> list[ControlClient]:
    """Stale duplicate control-mode clients to detach (pure selection).

    The app holds exactly ONE control-mode client per session. When a session
    has MORE THAN ONE control-mode client, the older/less-recently-active ones
    are orphans from prior attaches that never detached (app crash / force-kill).
    Returns those orphans, sparing:

    * the most-recently-active control client per session (the live app), and
    * EVERY non-control client (interactive ``attach``/``switch-client`` shells).

    A session with a single control client (or none) yields nothing — there is
    no duplicate to reap, so a healthy single attach is never disturbed.
    """
    by_session: dict[tuple[str, str], list[ControlClient]] = {}
    for client in clients:
        if not client.control_mode:
            continue
        by_session.setdefault((client.socket, client.session), []).append(client)

    orphans: list[ControlClient] = []
    for group in by_session.values():
        if len(group) < 2:
            continue
        # Keep the most-recently-active client; detach the rest. Tie-break by
        # list order (keep the last-listed) so selection is deterministic.
        keeper = max(
            range(len(group)), key=lambda i: (group[i].activity_at, i)
        )
        orphans.extend(
            client for idx, client in enumerate(group) if idx != keeper
        )
    return orphans


def scan_orphan_control_clients(uid: int | None = None) -> list[ControlClient]:
    """Orphan control-mode clients across every reachable socket for the user."""
    uid = _uid() if uid is None else uid
    orphans: list[ControlClient] = []
    for socket in list_socket_paths(uid):
        raw = list_clients_raw(socket)
        if not raw:
            continue
        orphans.extend(
            select_orphan_control_clients(parse_clients(socket, raw))
        )
    return orphans


def detach_client(socket: str, target: str) -> bool:
    """``tmux detach-client`` a single client; True on success.

    Detaches ONLY the named client — it never touches the session, the server,
    or any other client, so the session and the live app survive.
    """
    try:
        result = _run(
            ["tmux", "-S", socket, "detach-client", "-t", target]
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


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
