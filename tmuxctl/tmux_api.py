from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from tmuxctl import robust, storage, strays
from tmuxctl.models import PaneInfo, SessionInfo


def _record_event(session_name: str, event: str, **fields) -> None:
    """Best-effort session-event log write; never raises, never blocks a
    create/kill/rename on a locked or unavailable db."""
    try:
        conn = storage.get_connection()
        storage.record_session_event(conn, session_name, event, **fields)
        conn.close()
    except Exception:  # noqa: BLE001 - logging must never block session lifecycle
        pass


class TmuxError(RuntimeError):
    pass


class TmuxNotFoundError(TmuxError):
    pass


class TmuxSessionNotFoundError(TmuxError):
    pass


class TmuxCommandError(TmuxError):
    pass


def _ensure_tmux() -> None:
    if shutil.which("tmux") is None:
        raise TmuxNotFoundError("tmux is not installed or not on PATH")


def _run_tmux(
    args: list[str],
    *,
    socket: str | None = None,
    check: bool = True,
    timeout: int | None = None,
    detach_child_stdio: bool = False,
) -> subprocess.CompletedProcess[str]:
    _ensure_tmux()
    argv = ["tmux", "-S", socket, *args] if socket else ["tmux", *args]
    try:
        if detach_child_stdio:
            # Issue #1170 (PocketShell): a `tmux new-session -d` that has to START
            # the server (no server yet) forks the server as a child. When tmuxctl
            # is invoked over an SSH exec channel and the server is scope-wrapped
            # (systemd-run --scope keeps it attached instead of daemonizing), the
            # server INHERITS this process's stdout/stderr. If those are pipes
            # (`capture_output=True`), `subprocess.run` blocks reading them until
            # the *daemon* closes them — which is never — so the create never
            # returns and the caller (PocketShell's bounded exec read) FALSE-fails
            # on its timeout even though the session was created. Point the child's
            # stdin/stdout at /dev/null (so an inherited server holds /dev/null, not
            # the caller's channel) and capture stderr to a real temp FILE (a file
            # fd the daemon may inherit harmlessly — subprocess never blocks reading
            # a pipe). The foreground `new-session -d` exits promptly, so the create
            # returns at once, WITHOUT losing tmux's real stderr for error reporting.
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errf:
                completed = subprocess.run(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=errf,
                    text=True,
                    check=False,
                    timeout=timeout,
                )
                errf.seek(0)
                stderr = errf.read()
            result = subprocess.CompletedProcess(
                completed.args, completed.returncode, "", stderr
            )
        else:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
    except subprocess.TimeoutExpired as exc:
        raise TmuxCommandError(
            f"tmux command timed out after {timeout}s: {' '.join(argv)}"
        ) from exc
    except FileNotFoundError as exc:
        raise TmuxNotFoundError("tmux is not installed or not on PATH") from exc

    if check and result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise TmuxCommandError(stderr or "tmux command failed")
    return result


def _socket_has_session(socket: str, name: str) -> bool:
    result = _run_tmux(
        ["has-session", "-t", f"={name}"], socket=socket, check=False, timeout=5
    )
    return result.returncode == 0


def locate_session(session_name: str) -> str | None:
    """Which socket this session currently lives on, if any.

    Checks the session's own dedicated per-session socket first (§0:
    structural isolation — every session created after the move to
    per-session servers lives here, alone), then falls back to the legacy
    shared/default socket for sessions created before that migration. None
    if the session exists on neither. This is the single choke point every
    other per-session function resolves its target socket through, so a
    session's identity is "wherever it actually is," not an assumption.
    """
    own = robust.socket_for(session_name)
    if _socket_has_session(own, session_name):
        return own
    default = strays.default_socket()
    if own != default and _socket_has_session(default, session_name):
        return default
    return None


def _tmuxctl_socket_paths() -> list[str]:
    """Every socket that could hold a tmuxctl session: each session's own
    dedicated socket, plus the legacy shared/default socket (sessions
    created before the migration to per-session servers)."""
    default = strays.default_socket()
    return [
        path
        for path in strays.list_socket_paths()
        if os.path.basename(path).startswith("tmuxctl-") or path == default
    ]


def list_sessions() -> list[str]:
    return [info.name for info in list_session_info()]


def list_session_info() -> list[SessionInfo]:
    """Every tmuxctl session across every socket it could live on.

    One session now means one server (§0), so this scans every candidate
    socket rather than asking a single shared server. A socket that's
    unreachable, times out, or errors is skipped rather than raised — one
    session's server having a bad day must not break `list`/`recent` for
    every other session, the same isolation principle applied to the CLI
    itself.
    """
    sessions: list[SessionInfo] = []
    seen: set[str] = set()
    for socket in _tmuxctl_socket_paths():
        try:
            result = _run_tmux(
                [
                    "list-sessions",
                    "-F",
                    "#{session_name}::#{session_created}::#{session_activity}",
                ],
                socket=socket,
                check=False,
                timeout=5,
            )
        except TmuxCommandError:
            continue
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("::")
            if len(parts) != 3:
                continue
            name, created_at, activity_at = parts
            if name in seen:
                continue
            seen.add(name)
            try:
                sessions.append(
                    SessionInfo(
                        name=name,
                        created_at=int(created_at),
                        activity_at=int(activity_at),
                    )
                )
            except ValueError:
                continue
    return sessions


def session_path(session_name: str) -> str | None:
    """The session's working directory (``#{session_path}``), or None if unknown."""
    socket = locate_session(session_name)
    if socket is None:
        return None
    result = _run_tmux(
        ["display-message", "-p", "-t", session_name, "#{session_path}"],
        socket=socket,
        check=False,
    )
    if result.returncode != 0:
        return None
    path = (result.stdout or "").strip()
    return path or None


# "::" rather than a tab: tmux's `-F` output mangles literal tab characters
# in field values (confirmed on tmux 3.4 and 3.6b — see issue #6), silently
# collapsing every field onto one line. "::" survives, and tmux forbids ":"
# in session/pane text this format never emits anyway.
_PANE_FORMAT = (
    "#{window_index}::#{pane_index}::#{pane_pid}::"
    "#{pane_current_command}::#{pane_current_path}::#{pane_active}"
)


def session_panes(session_name: str) -> list[PaneInfo]:
    """Every pane in a session, with its top process, pid, and cwd.

    Lists panes across all windows (``list-panes -s``). The pane's
    ``pane_current_command`` is the foreground process tmux sees and
    ``pane_current_path`` its working directory, so no /proc reads are needed.
    """
    socket = locate_session(session_name)
    if socket is None:
        raise TmuxSessionNotFoundError(f"tmux session '{session_name}' was not found")

    result = _run_tmux(
        ["list-panes", "-s", "-t", session_name, "-F", _PANE_FORMAT],
        socket=socket,
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise TmuxCommandError(stderr or f"unable to list panes for '{session_name}'")

    panes: list[PaneInfo] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("::")
        if len(parts) < 6:
            continue
        window_index, pane_index, pane_pid, command, cwd, active = parts[:6]
        panes.append(
            PaneInfo(
                window_index=int(window_index) if window_index.isdigit() else 0,
                pane_index=int(pane_index) if pane_index.isdigit() else 0,
                pid=int(pane_pid) if pane_pid.isdigit() else 0,
                command=command,
                cwd=cwd,
                active=active not in ("0", ""),
            )
        )
    return panes


def session_exists(name: str) -> bool:
    return locate_session(name) is not None


def current_session_name() -> str:
    if not os.environ.get("TMUX"):
        raise TmuxCommandError("session alias ':current' requires running inside tmux")

    # No socket: $TMUX already tells tmux which server the current client is
    # on, which is exactly the server we want to ask.
    result = _run_tmux(["display-message", "-p", "#S"], check=False)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise TmuxCommandError(stderr or "unable to determine current tmux session")

    session_name = result.stdout.strip()
    if not session_name:
        raise TmuxCommandError("unable to determine current tmux session")
    return session_name


def attach_session(session_name: str, *, resize_window: bool = False) -> None:
    socket = locate_session(session_name)
    if socket is None:
        raise TmuxSessionNotFoundError(f"tmux session '{session_name}' was not found")

    inside_tmux = bool(os.environ.get("TMUX"))
    if inside_tmux:
        # switch-client only works within the SAME tmux server. Post-§0,
        # every session is its own server, so a plain switch-client can no
        # longer reach it -- hand the current client off to a fresh attach
        # against the TARGET server instead. No socket= here: $TMUX already
        # points detach-client at the CURRENT (source) server, which is the
        # one that needs to receive this command.
        attach_argv = ["tmux", "-S", socket, "attach-session", "-t", session_name]
        if resize_window:
            attach_argv.extend([";", "resize-window", "-A"])
        result = _run_tmux(["detach-client", "-E", shlex.join(attach_argv)], check=False)
    else:
        command = ["attach-session", "-t", session_name]
        if resize_window:
            command.extend([";", "resize-window", "-A"])
        result = _run_tmux(command, socket=socket, check=False)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise TmuxCommandError(stderr or f"failed to attach to '{session_name}'")


def _login_shell(session_name: str) -> list[str]:
    """The interactive login shell to run as the session's top process.

    When §1 dtach wrapping is enabled (``resolve_dtach_wrap()``, default
    off) and ``dtach`` is installed, wraps it behind a durable dtach master
    so the shell -- and whatever's running in it, e.g. an unattended
    ``claude`` session -- keeps running even if this session's own tmux
    server dies later. Only the session's initial pane is covered; panes/
    windows created interactively afterward are not (see
    docs/oom-recovery-plan.md §1's default-command note).
    """
    shell = os.environ.get("SHELL") or "/bin/sh"
    plain = [shell, "-l"]
    if robust.resolve_dtach_wrap() and robust.dtach_available():
        return robust.dtach_wrap(plain, robust.dtach_socket_for(session_name))
    return plain


def _server_running() -> bool:
    """True when a tmux server is reachable on the legacy shared/default socket.

    Distinguishes a running-but-empty server (rc 0, no sessions) from no
    server at all ("no server running" on stderr), so :func:`ensure_server`
    never bootstraps a second server over an existing one.
    """
    result = _run_tmux(["list-sessions"], check=False)
    if result.returncode == 0:
        return True
    return "no server running" not in (result.stderr or "").lower()


def server_pid() -> int | None:
    """PID of the tmux server on the legacy shared/default socket, or None."""
    result = _run_tmux(["display-message", "-p", "#{pid}"], check=False)
    if result.returncode != 0:
        return None
    try:
        return int((result.stdout or "").strip())
    except ValueError:
        return None


def session_server_pid(session_name: str) -> int | None:
    """PID of a session's own dedicated tmux server, or None if unreachable."""
    socket = robust.socket_for(session_name)
    if not _socket_has_session(socket, session_name):
        return None
    result = _run_tmux(["display-message", "-p", "#{pid}"], socket=socket, check=False)
    if result.returncode != 0:
        return None
    try:
        return int((result.stdout or "").strip())
    except ValueError:
        return None


def ensure_server() -> None:
    """Ensure the LEGACY shared tmux server runs in its own login-independent
    systemd unit. Only meaningful during the §0 migration window, for
    sessions still on the shared/default socket -- new sessions bootstrap
    their own dedicated server instead (see :func:`create_or_attach_session`)
    and never call this. Kept as a manual recovery primitive (e.g. if the
    shared server dies before every legacy session has been individually
    recreated onto its own server -- 'tmuxctl salvage' points here).

    No-op when a server is already running (we never migrate or duplicate
    one) or when systemd user scopes are unavailable (the server then runs
    wherever tmux puts it, as before — graceful degrade).
    """
    if not robust.systemd_available():
        return
    if _server_running():
        return
    try:
        robust.ensure_slice(
            robust.resolve_slice_max(),
            swap_max=robust.resolve_slice_swap_max(),
        )
    except Exception:  # noqa: BLE001 - slice bound is best-effort
        pass
    # A crashed server can leave its fixed-name unit in a failed/loaded state;
    # systemd-run would then refuse the name and tmux would fall back to an
    # unprotected login-scoped server. Free the dead name before bootstrapping.
    robust.reset_server_unit()
    try:
        result = subprocess.run(
            robust.server_bootstrap_argv(),
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, FileNotFoundError):
        return
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        print(
            "tmuxctl: could not bootstrap the persistent tmux server"
            f"{f' ({detail})' if detail else ''}; it may run unprotected in the "
            "login scope.",
            file=sys.stderr,
        )


def create_or_attach_session(
    session_name: str,
    *,
    resize_window: bool = False,
    shell_command: list[str] | None = None,
    mem: str | None = None,
) -> None:
    if session_exists(session_name):
        attach_session(session_name, resize_window=resize_window)
        return

    cwd = os.getcwd()
    resolved = _bootstrap_and_create(session_name, cwd, flag=mem)
    _record_event(session_name, "created", start_dir=cwd, **resolved)

    if shell_command:
        send_keys(session_name, shlex.join(shell_command), press_enter=True, enter_delay_ms=0)

    attach_session(session_name, resize_window=resize_window)


def create_detached_session(
    session_name: str,
    *,
    start_dir: str | None = None,
    mem: str | None = None,
) -> None:
    """Create a memory-capped detached session without attaching.

    The creation half of :func:`create_or_attach_session` (scope-wrapped under
    ``robust.slice``, running on its own dedicated per-session server) with
    the attach step removed — for consumers that attach over their own
    transport (e.g. tmux ``-CC`` control mode) and would otherwise build raw,
    uncapped ``new-session`` commands.

    Idempotent: a no-op when the session already exists, so it never
    resurrects or duplicates. ``mem`` resolves the same way as the other
    verbs (flag → project config → default); without ``systemd-run``/cgroup
    v2 it falls back to a plain detached session with no error.
    """
    if session_exists(session_name):
        return

    cwd = start_dir or os.getcwd()
    resolved = _bootstrap_and_create(session_name, cwd, flag=mem)
    _record_event(session_name, "created", start_dir=cwd, **resolved)


def _bootstrap_and_create(
    session_name: str, cwd: str, *, flag: str | None
) -> dict[str, str | None]:
    """Start this session's OWN tmux server and create the session on it, in
    one step (§0). A different process, a different cgroup, a different
    socket than every other session — so this session's crash, from OOM or
    anything else, has no path to reach any other session.

    Returns the resolved mem/swap/high/scope_unit/socket_path for the event
    log, mirroring ``_new_session_command``'s ``resolved`` shape.
    """
    tmux_argv, resolved = _new_session_command(session_name, cwd, flag=flag)
    resolved = {**resolved, "socket_path": robust.socket_for(session_name)}

    if robust.systemd_available():
        # A crashed per-session server can leave its unit failed/loaded,
        # which would make systemd-run refuse the name on the next create.
        robust.reset_session_server_unit(session_name)
        bootstrap = robust.session_server_bootstrap_argv(session_name, tmux_argv)
        try:
            result = subprocess.run(bootstrap, capture_output=True, text=True, check=False)
        except (OSError, FileNotFoundError) as exc:
            raise TmuxCommandError(
                f"failed to bootstrap server for '{session_name}': {exc}"
            ) from exc
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise TmuxCommandError(
                stderr or f"failed to bootstrap server for '{session_name}'"
            )
        return resolved

    # No systemd: fall back to a plain per-session tmux server (still its
    # own socket/process, just unprotected) -- same graceful-degrade
    # philosophy as the memory-cap wrapping itself.
    # Issue #1170: detach the child's stdio so a freshly-forked server
    # cannot inherit and hold open the caller's SSH exec channel.
    result = _run_tmux(
        tmux_argv,
        socket=robust.socket_for(session_name),
        check=False,
        detach_child_stdio=True,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise TmuxCommandError(stderr or f"failed to create session '{session_name}'")
    return resolved


def _new_session_command(
    session_name: str, cwd: str, *, flag: str | None
) -> tuple[list[str], dict[str, str | None]]:
    """Build the ``tmux new-session -d`` argv for a new session.

    Scope-wraps the session under ``robust.slice`` with the resolved
    ``MemoryMax`` when a cap applies and systemd is available; otherwise
    returns a plain detached session (emitting a one-line stderr note when a
    cap was wanted but ``systemd-run`` is unavailable).

    Returns ``(argv, resolved)`` where ``resolved`` carries the mem/swap/high
    values and scope unit actually applied (or None where uncapped), for the
    caller to write into the session event log without re-resolving.
    """
    unit = robust.scope_unit_name(session_name)
    mem = _resolve_session_mem(cwd, flag=flag)
    swap = _resolve_session_swap(cwd)
    plain = ["new-session", "-d", "-s", session_name, "-c", cwd]
    uncapped = {"mem": None, "swap": None, "high": None, "scope_unit": None}

    if mem is not None and robust.systemd_available():
        # A same-named scope can outlive its session (a direct ``tmux
        # kill-session``, a server restart that reparents the pane, or a
        # backgrounded process keeping the cgroup open). ``systemd-run --unit=``
        # then refuses the name, the wrapped shell dies instantly, tmux destroys
        # the new 0-window session, and the caller sees only a misleading
        # "session was not found". Reconcile the leftover before reusing the name.
        props = robust.scope_properties(unit, ["ActiveState", "LoadState"])
        if props.get("ActiveState") == "active":
            if robust.resolve_dtach_wrap() and robust.scope_occupied_only_by_dtach(unit):
                # §1: the scope being "active" is EXPECTED here -- this
                # session's own dtach master survived its tmux server dying,
                # which is the entire point. Reattach into the scope that
                # already owns and caps it, instead of treating this as
                # squatting or building a colliding second scope.
                existing = robust.scope_properties(
                    unit, ["MemoryMax", "MemorySwapMax", "MemoryHigh"]
                )
                reattach = robust.dtach_attach_argv(robust.dtach_socket_for(session_name))
                resolved = {
                    "mem": existing.get("MemoryMax"),
                    "swap": existing.get("MemorySwapMax"),
                    "high": existing.get("MemoryHigh"),
                    "scope_unit": f"{unit}.scope",
                }
                return ["new-session", "-d", "-s", session_name, "-c", cwd, *reattach], resolved
            # Live processes from an earlier session still own the scope; we must
            # not kill them silently, so degrade to an uncapped session (as when
            # systemd-run is unavailable) and tell the user how to reclaim it.
            print(
                f"tmuxctl: scope {unit}.scope still has live processes from an "
                f"earlier '{session_name}' session; new session runs WITHOUT a "
                f"memory cap. Reclaim the name with "
                f"'systemctl --user stop {unit}.scope'.",
                file=sys.stderr,
            )
            return plain, uncapped
        if props.get("LoadState") == "loaded":
            # Dead-but-lingering (failed/inactive) scope: free the name to reuse.
            robust.reset_scope(unit)

        # Ensure the parent slice exists before any capped session joins it.
        try:
            robust.ensure_slice(
                robust.resolve_slice_max(),
                swap_max=robust.resolve_slice_swap_max(),
            )
        except Exception:  # noqa: BLE001 - slice bound is best-effort
            pass
        high = _resolve_session_high(cwd, mem)
        _warn_if_oversubscribed(mem)
        wrapped = robust.scope_wrap(_login_shell(session_name), unit, mem, swap=swap, high=high)
        resolved = {"mem": mem, "swap": swap, "high": high, "scope_unit": f"{unit}.scope"}
        return ["new-session", "-d", "-s", session_name, "-c", cwd, *wrapped], resolved

    if mem is not None:
        print(
            "tmuxctl: systemd-run unavailable; session runs without a memory cap",
            file=sys.stderr,
        )
    return plain, uncapped


def _warn_if_oversubscribed(new_mem: str) -> None:
    """Defense-in-depth warning (§3): print a one-line stderr note when this
    session's cap pushes the sum of every scope's MemoryMax past the
    configured percentage of system capacity. Warn-only, never blocks
    creation — the daemon health check and 'doctor' are the primary
    defenses; this is a courtesy at the point of the decision.
    """
    try:
        pct = robust.resolve_oversubscription_max_pct()
        if pct <= 0:
            return
        capacity = robust.system_capacity()
        if capacity <= 0:
            return
        reserved = robust.total_reserved_mem() + robust.parse_size(new_mem)
        threshold = capacity * pct // 100
        if reserved > threshold:
            print(
                f"tmuxctl: total session memory caps "
                f"({robust.format_size(reserved)}) now exceed {pct}% of system "
                f"capacity ({robust.format_size(capacity)}); consider lowering "
                "--mem or running 'tmuxctl doctor'",
                file=sys.stderr,
            )
    except Exception:  # noqa: BLE001 - the guard must never block session creation
        pass


def _resolve_session_mem(cwd: str, *, flag: str | None = None) -> str | None:
    """Resolve the per-session MemoryMax ceiling for a new session."""
    try:
        return robust.resolve_mem(flag=flag, cwd=Path(cwd))
    except Exception:  # noqa: BLE001 - never block session creation on config
        return robust.DEFAULT_MEM


def _resolve_session_swap(cwd: str) -> str:
    """Resolve the per-session MemorySwapMax allowance for a new session."""
    try:
        return robust.resolve_swap(cwd=Path(cwd))
    except Exception:  # noqa: BLE001 - never block session creation on config
        return robust.DEFAULT_SWAP


def _resolve_session_high(cwd: str, mem: str) -> str:
    """Resolve the per-session MemoryHigh soft-throttle for a new session."""
    try:
        return robust.resolve_high(mem, cwd=Path(cwd))
    except Exception:  # noqa: BLE001 - never block session creation on config
        return robust.default_high_for(mem)


def process_cgroup(pid: int) -> str | None:
    """The cgroup v2 path a process lives in, from /proc/<pid>/cgroup.

    Reveals whether a session not started by tmuxctl is capped: a plain login
    session reads ``…/session-NN.scope`` while a robust session reads
    ``…/robust.slice/tmuxctl-<name>.scope``. Returns None if unreadable.
    """
    try:
        text = Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        # cgroup v2 unified hierarchy line: "0::/user.slice/...".
        if line.startswith("0::"):
            return line[3:].strip() or None
    return None


def kill_session(session_name: str) -> None:
    socket = locate_session(session_name)
    if socket is None:
        raise TmuxSessionNotFoundError(f"tmux session '{session_name}' was not found")

    result = _run_tmux(["kill-session", "-t", session_name], socket=socket, check=False)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise TmuxCommandError(stderr or f"failed to kill '{session_name}'")

    # Tear down the session's memory-capped scope so the cgroup is reaped,
    # and its own dedicated server unit if it had one. Both are best-effort
    # no-ops if already gone: `exit-empty` already exits a per-session
    # server the moment its one session ends, and a legacy (pre-§0,
    # shared-server) session never had its own server unit to begin with.
    robust.stop_scope(robust.scope_unit_name(session_name))
    robust.stop_session_server(session_name)


def set_session_limits(
    session_name: str,
    *,
    mem: str | None = None,
    swap: str | None = None,
    high: str | None = None,
) -> None:
    """Update MemoryMax/MemorySwapMax/MemoryHigh for a running tmuxctl scope."""
    if mem is None and swap is None and high is None:
        raise TmuxCommandError("provide --mem, --swap, --high, or a combination")
    if not session_exists(session_name):
        raise TmuxSessionNotFoundError(f"tmux session '{session_name}' was not found")
    if not robust.systemd_available():
        raise TmuxCommandError("systemd-run unavailable; session limits cannot be changed")

    properties: list[str] = []
    if high is not None:
        robust.parse_size(high)
        properties.append(f"MemoryHigh={high}")
    if mem is not None:
        robust.parse_size(mem)
        properties.append(f"MemoryMax={mem}")
    if swap is not None:
        robust.parse_size(swap)
        properties.append(f"MemorySwapMax={swap}")

    scope = f"{robust.scope_unit_name(session_name)}.scope"
    try:
        result = subprocess.run(
            ["systemctl", "--user", "set-property", scope, *properties],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, FileNotFoundError) as exc:
        raise TmuxCommandError("systemctl unavailable; session limits cannot be changed") from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise TmuxCommandError(stderr or f"failed to update limits for '{session_name}'")


def rename_session(session_name: str, new_name: str) -> None:
    socket = locate_session(session_name)
    if socket is None:
        raise TmuxSessionNotFoundError(f"tmux session '{session_name}' was not found")
    if session_exists(new_name):
        raise TmuxCommandError(f"tmux session '{new_name}' already exists")

    result = _run_tmux(
        ["rename-session", "-t", session_name, new_name], socket=socket, check=False
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise TmuxCommandError(stderr or f"failed to rename '{session_name}' to '{new_name}'")

    # Move this session's own dedicated socket file to match its new name,
    # so name-based lookups (locate_session) keep finding it post-rename. A
    # bound AF_UNIX socket's directory entry can be renamed in place -- the
    # kernel doesn't care about the path once bound, so this doesn't disturb
    # the running server or any already-connected client.
    #
    # KNOWN LIMITATION: the scope and server-unit names stay tied to the
    # ORIGINAL (pre-rename) session name -- systemd units can't be renamed
    # in place. kill_session/set_session_limits compute those names from the
    # CURRENT name, so a renamed session's scope/server unit is not found by
    # its new name (a pre-existing limitation predating per-session
    # servers -- see docs/oom-recovery-plan.md, §0's "Rename" note).
    if socket == robust.socket_for(session_name):
        try:
            os.rename(socket, robust.socket_for(new_name))
        except OSError:
            pass


def send_keys(
    session_name: str,
    message: str,
    press_enter: bool = True,
    enter_delay_ms: int = 0,
) -> None:
    socket = locate_session(session_name)
    if socket is None:
        raise TmuxSessionNotFoundError(f"tmux session '{session_name}' was not found")

    result = _run_tmux(
        ["send-keys", "-t", session_name, message], socket=socket, check=False, timeout=30
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise TmuxCommandError(stderr or f"failed to send keys to '{session_name}'")

    if press_enter:
        if enter_delay_ms > 0:
            time.sleep(enter_delay_ms / 1000)
        result = _run_tmux(
            ["send-keys", "-t", session_name, "Enter"], socket=socket, check=False, timeout=30
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise TmuxCommandError(stderr or f"failed to send Enter to '{session_name}'")
