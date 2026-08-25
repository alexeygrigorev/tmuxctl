"""Post-crash recovery scan: for every tmuxctl session, is there something
live to reattach to, or something that needs recreating?

Structured like ``strays.py`` (dataclasses + pure scan functions, no CLI
concerns) -- ``cli.py`` gets a thin ``salvage`` command that calls this and
prints. §0 (per-session servers) + §1 (dtach) mean the common case is now
"something is still alive, reattach to it directly" rather than reverse-
engineering an orphaned, pty-less process tree from scratch.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from tmuxctl import robust, storage, tmux_api


@dataclass(slots=True)
class SalvageEntry:
    session_name: str
    status: str  # see STATUSES below
    detail: str
    reattach_command: str | None = None
    cwd: str | None = None
    mem: str | None = None
    pid: int | None = None


@dataclass(slots=True)
class SalvageReport:
    entries: list[SalvageEntry] = field(default_factory=list)


# healthy               -- live on its own server, nothing to do
# reattachable          -- a bare tmux server survived in the scope; attach directly
# reattachable-dtach     -- this session's own dtach master survived its dead server (§1)
# stale-work            -- the server (and any dtach master) are gone, but something
#                          else is still running and holding the scope open
# gone                  -- no live process at all; recreate from the event log
# needs-manual-reclaim  -- a FOREIGN occupant holds the scope; --recreate must not touch it
STATUSES = (
    "healthy",
    "reattachable",
    "reattachable-dtach",
    "stale-work",
    "gone",
    "needs-manual-reclaim",
)


def _run(args: list[str], timeout: int = 5) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=timeout)


def _socket_has_session(socket: str, name: str) -> bool:
    try:
        result = _run(["tmux", "-S", socket, "has-session", "-t", f"={name}"])
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _scope_units() -> list[str]:
    """Every ``tmuxctl-*.scope`` unit's bare name (no ``.scope``), live or
    dead-but-loaded, via the same ``list-units`` pattern used elsewhere."""
    if not robust.systemd_available():
        return []
    try:
        result = _run(
            ["systemctl", "--user", "list-units", "tmuxctl-*.scope", "--all", "--no-legend", "--plain"],
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    units = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        unit = line.split()[0]
        if unit.endswith(".scope"):
            units.append(unit[: -len(".scope")])
    return units


def _session_name_from_scope_unit(unit: str) -> str:
    prefix = "tmuxctl-"
    return unit[len(prefix):] if unit.startswith(prefix) else unit


def _proc_comm(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _proc_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", errors="replace").strip()


def _proc_cwd(pid: int) -> str | None:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def _classify_scope(name: str, unit: str) -> SalvageEntry:
    own_socket = robust.socket_for(name)
    if _socket_has_session(own_socket, name):
        return SalvageEntry(
            session_name=name,
            status="healthy",
            detail="live on its own server",
            reattach_command=f"tmux -S {own_socket} attach -t {name}",
        )

    # Not on its own per-session socket -- before concluding anything is
    # wrong, check the legacy shared/default socket too. Every session
    # created before the §0 migration (which, on a freshly-upgraded
    # install, is EVERY session) still lives there, perfectly healthy; the
    # scope's live process is the pane shell itself (comm != "tmux"), which
    # would otherwise misclassify a live legacy session as stale-work.
    legacy_socket = tmux_api.locate_session(name)
    if legacy_socket is not None and legacy_socket != own_socket:
        return SalvageEntry(
            session_name=name,
            status="healthy",
            detail="live on the legacy shared server (not yet migrated to §0)",
            reattach_command=f"tmux -S {legacy_socket} attach -t {name}",
        )

    cgroup = robust.scope_cgroup_path(unit if unit.endswith(".scope") else f"{unit}.scope")
    pids = robust.cgroup_proc_pids(cgroup) if cgroup else []
    if not pids:
        return SalvageEntry(
            session_name=name, status="gone",
            detail=f"scope {unit}.scope has no live processes",
        )

    by_comm: dict[int, str | None] = {pid: _proc_comm(pid) for pid in pids}

    tmux_pids = [pid for pid, comm in by_comm.items() if comm == "tmux"]
    if tmux_pids:
        # A tmux server is alive somewhere in this scope but not answering
        # on this session's OWN socket (e.g. a legacy/default-socket
        # session whose scope happens to still be tracked, or the name was
        # squatted by something unrelated). Either way this is not ours to
        # silently reclaim.
        return SalvageEntry(
            session_name=name, status="needs-manual-reclaim",
            detail=(
                f"scope {unit}.scope has a tmux server (pid {tmux_pids[0]}) not "
                f"reachable on its own socket ({own_socket}) — "
                f"reclaim with 'systemctl --user stop {unit}.scope' once you've "
                "confirmed nothing important is running there"
            ),
            pid=tmux_pids[0],
        )

    dtach_pids = [pid for pid, comm in by_comm.items() if comm == "dtach"]
    if dtach_pids and len(dtach_pids) == len(pids):
        # §1: this session's own dtach master(s) survived its tmux server
        # dying -- exactly the case the feature exists for. Not squatting.
        dtach_socket = robust.dtach_socket_for(name)
        return SalvageEntry(
            session_name=name, status="reattachable-dtach",
            detail=f"dtach master pid {dtach_pids[0]} survived its tmux server dying",
            reattach_command=f"dtach -a {dtach_socket}",
            pid=dtach_pids[0],
        )

    # Neither a tmux server nor (only) dtach masters: some other work is
    # still running and keeping the scope/cgroup open (a backgrounded build,
    # a dev server, ...). Recoverable by recreating the session and letting
    # the user decide what to do with the orphaned work, but flag whether
    # its cwd still exists on disk (the deleted-worktree case).
    pid = pids[0]
    cwd = _proc_cwd(pid)
    cwd_status = "OK" if cwd and os.path.exists(cwd) else "DELETED"
    cmdline = _proc_cmdline(pid) or "?"
    return SalvageEntry(
        session_name=name, status="stale-work",
        detail=f"{cmdline} pid {pid}, cwd {cwd_status} ({cwd or '?'})",
        cwd=cwd,
        pid=pid,
    )


def _scan_from_log(conn, seen_names: set[str]) -> list[SalvageEntry]:
    """Sessions the durable log (§2) remembers that have no live scope AND
    no live socket at all -- e.g. after a reboot, or a scope that was fully
    reaped. The log is the only surviving record of where they were and
    what cap they had.
    """
    events = storage.list_session_events(conn, limit=100_000)
    names = sorted({e.session_name for e in events} - seen_names)
    entries: list[SalvageEntry] = []
    for name in names:
        last_created = storage.last_session_event(conn, name, event="created")
        if last_created is None:
            continue
        last_killed = storage.last_session_event(conn, name, event="killed")
        # A session deliberately killed after it was last created (and never
        # recreated since) is gone on purpose, not a crash victim -- skip it.
        # Compare by row id, not created_at: the timestamp is truncated to
        # whole seconds, so a kill+recreate in the same second would
        # otherwise be misread as "killed most recently".
        if last_killed is not None and last_killed.id >= last_created.id:
            continue
        entries.append(
            SalvageEntry(
                session_name=name, status="gone",
                detail=(
                    f"last seen {last_created.created_at} (session_events); "
                    f"will recreate at {last_created.start_dir or '?'}"
                ),
                cwd=last_created.start_dir,
                mem=last_created.mem,
            )
        )
    return entries


def scan(conn=None) -> SalvageReport:
    entries: list[SalvageEntry] = []
    seen_names: set[str] = set()

    for unit in _scope_units():
        name = _session_name_from_scope_unit(unit)
        if name in seen_names:
            continue
        seen_names.add(name)
        entries.append(_classify_scope(name, unit))

    # Sessions live and well but with no scope entry at all (uncapped --
    # systemd was unavailable at creation, or the cap was declined because
    # an earlier scope was still squatting).
    try:
        for name in tmux_api.list_sessions():
            if name in seen_names:
                continue
            seen_names.add(name)
            socket = tmux_api.locate_session(name)
            entries.append(
                SalvageEntry(
                    session_name=name, status="healthy",
                    detail="live, uncapped (no scope)",
                    reattach_command=f"tmux -S {socket} attach -t {name}" if socket else None,
                )
            )
    except Exception:  # noqa: BLE001 - a listing failure must not break the scan
        pass

    if conn is not None:
        entries.extend(_scan_from_log(conn, seen_names))

    return SalvageReport(entries=entries)
