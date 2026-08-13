from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from tmuxctl import robust
from tmuxctl.models import PaneInfo, SessionInfo


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
    check: bool = True,
    timeout: int | None = None,
    detach_child_stdio: bool = False,
) -> subprocess.CompletedProcess[str]:
    _ensure_tmux()
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
                    ["tmux", *args],
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
                ["tmux", *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
    except subprocess.TimeoutExpired as exc:
        raise TmuxCommandError(
            f"tmux command timed out after {timeout}s: tmux {' '.join(args)}"
        ) from exc
    except FileNotFoundError as exc:
        raise TmuxNotFoundError("tmux is not installed or not on PATH") from exc

    if check and result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise TmuxCommandError(stderr or "tmux command failed")
    return result


def list_sessions() -> list[str]:
    result = _run_tmux(["list-sessions", "-F", "#{session_name}"], check=False)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if "no server running" in stderr.lower():
            return []
        raise TmuxCommandError(stderr or "unable to list tmux sessions")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def list_session_info() -> list[SessionInfo]:
    result = _run_tmux(
        ["list-sessions", "-F", "#{session_name}\t#{session_created}\t#{session_activity}"],
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if "no server running" in stderr.lower():
            return []
        raise TmuxCommandError(stderr or "unable to list tmux sessions")

    sessions: list[SessionInfo] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        name, created_at, activity_at = line.split("\t")
        sessions.append(
            SessionInfo(
                name=name,
                created_at=int(created_at),
                activity_at=int(activity_at),
            )
        )
    return sessions


_PANE_FORMAT = (
    "#{window_index}\t#{pane_index}\t#{pane_pid}\t"
    "#{pane_current_command}\t#{pane_current_path}\t#{pane_active}"
)


def session_panes(session_name: str) -> list[PaneInfo]:
    """Every pane in a session, with its top process, pid, and cwd.

    Lists panes across all windows (``list-panes -s``). The pane's
    ``pane_current_command`` is the foreground process tmux sees and
    ``pane_current_path`` its working directory, so no /proc reads are needed.
    """
    if not session_exists(session_name):
        raise TmuxSessionNotFoundError(f"tmux session '{session_name}' was not found")

    result = _run_tmux(
        ["list-panes", "-s", "-t", session_name, "-F", _PANE_FORMAT], check=False
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise TmuxCommandError(stderr or f"unable to list panes for '{session_name}'")

    panes: list[PaneInfo] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
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
    # The leading '=' forces tmux to do an EXACT match. Without it, tmux's -t
    # target does prefix/fnmatch matching, so "git-pocketshell" would falsely
    # match an existing "git-pocketshell-desktop" -- and create-or-attach would
    # attach to the wrong session instead of creating a new one.
    result = _run_tmux(["has-session", "-t", f"={name}"], check=False)
    return result.returncode == 0


def current_session_name() -> str:
    if not os.environ.get("TMUX"):
        raise TmuxCommandError("session alias ':current' requires running inside tmux")

    result = _run_tmux(["display-message", "-p", "#S"], check=False)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise TmuxCommandError(stderr or "unable to determine current tmux session")

    session_name = result.stdout.strip()
    if not session_name:
        raise TmuxCommandError("unable to determine current tmux session")
    return session_name


def attach_session(session_name: str, *, resize_window: bool = False) -> None:
    if not session_exists(session_name):
        raise TmuxSessionNotFoundError(f"tmux session '{session_name}' was not found")

    inside_tmux = bool(os.environ.get("TMUX"))
    command = ["switch-client", "-t", session_name] if inside_tmux else ["attach-session", "-t", session_name]
    if resize_window:
        command.extend([";", "resize-window", "-A"])
    result = _run_tmux(command, check=False)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise TmuxCommandError(stderr or f"failed to attach to '{session_name}'")


def _login_shell() -> list[str]:
    """The interactive login shell to run as the session's top process."""
    shell = os.environ.get("SHELL") or "/bin/sh"
    return [shell, "-l"]


def _server_running() -> bool:
    """True when a tmux server is reachable on the default socket.

    Distinguishes a running-but-empty server (rc 0, no sessions) from no server
    at all ("no server running" on stderr), so :func:`ensure_server` never
    bootstraps a second server over an existing one.
    """
    result = _run_tmux(["list-sessions"], check=False)
    if result.returncode == 0:
        return True
    return "no server running" not in (result.stderr or "").lower()


def server_pid() -> int | None:
    """PID of the tmux server on the default socket, or None if not running."""
    result = _run_tmux(["display-message", "-p", "#{pid}"], check=False)
    if result.returncode != 0:
        return None
    try:
        return int((result.stdout or "").strip())
    except ValueError:
        return None


def ensure_server() -> None:
    """Ensure the tmux server runs in its own login-independent systemd unit.

    No-op when a server is already running (we never migrate or duplicate one)
    or when systemd user scopes are unavailable (the server then runs wherever
    tmux puts it, as before — graceful degrade). Otherwise bootstraps a
    persistent server outside the capped workload slice via
    :func:`robust.server_bootstrap_argv`, so a login logout, per-session OOM, or
    aggregate workload-slice OOM can no longer take down every session at once.
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

    ensure_server()
    cwd = os.getcwd()
    command = _new_session_command(session_name, cwd, flag=mem)
    result = _run_tmux(command, check=False)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise TmuxCommandError(stderr or f"failed to create session '{session_name}'")

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
    ``robust.slice``) with the attach step removed — for consumers that attach
    over their own transport (e.g. tmux ``-CC`` control mode) and would
    otherwise build raw, uncapped ``new-session`` commands.

    Idempotent: a no-op when the session already exists, so it never
    resurrects or duplicates. ``mem`` resolves the same way as the other
    verbs (flag → project config → default); without ``systemd-run``/cgroup
    v2 it falls back to a plain detached session with no error.
    """
    if session_exists(session_name):
        return

    ensure_server()
    cwd = start_dir or os.getcwd()
    command = _new_session_command(session_name, cwd, flag=mem)
    # Issue #1170: run the create with the child's stdin/stdout detached from this
    # process's stdio so a freshly-spawned (scope-wrapped, non-daemonizing) server
    # cannot inherit and hold the caller's SSH exec channel open — which would make
    # the caller's bounded output read never reach EOF and FALSE-fail the create.
    result = _run_tmux(command, check=False, detach_child_stdio=True)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise TmuxCommandError(stderr or f"failed to create session '{session_name}'")


def _new_session_command(session_name: str, cwd: str, *, flag: str | None) -> list[str]:
    """Build the ``tmux new-session -d`` argv for a new session.

    Scope-wraps the session under ``robust.slice`` with the resolved
    ``MemoryMax`` when a cap applies and systemd is available; otherwise
    returns a plain detached session (emitting a one-line stderr note when a
    cap was wanted but ``systemd-run`` is unavailable).
    """
    unit = robust.scope_unit_name(session_name)
    mem = _resolve_session_mem(cwd, flag=flag)
    swap = _resolve_session_swap(cwd)
    plain = ["new-session", "-d", "-s", session_name, "-c", cwd]

    if mem is not None and robust.systemd_available():
        # A same-named scope can outlive its session (a direct ``tmux
        # kill-session``, a server restart that reparents the pane, or a
        # backgrounded process keeping the cgroup open). ``systemd-run --unit=``
        # then refuses the name, the wrapped shell dies instantly, tmux destroys
        # the new 0-window session, and the caller sees only a misleading
        # "session was not found". Reconcile the leftover before reusing the name.
        props = robust.scope_properties(unit, ["ActiveState", "LoadState"])
        if props.get("ActiveState") == "active":
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
            return plain
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
        wrapped = robust.scope_wrap(_login_shell(), unit, mem, swap=swap, high=high)
        return ["new-session", "-d", "-s", session_name, "-c", cwd, *wrapped]

    if mem is not None:
        print(
            "tmuxctl: systemd-run unavailable; session runs without a memory cap",
            file=sys.stderr,
        )
    return plain


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
    if not session_exists(session_name):
        raise TmuxSessionNotFoundError(f"tmux session '{session_name}' was not found")

    result = _run_tmux(["kill-session", "-t", session_name], check=False)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise TmuxCommandError(stderr or f"failed to kill '{session_name}'")

    # Tear down the session's memory-capped scope so the cgroup is reaped.
    robust.stop_scope(robust.scope_unit_name(session_name))


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
    if not session_exists(session_name):
        raise TmuxSessionNotFoundError(f"tmux session '{session_name}' was not found")
    if session_exists(new_name):
        raise TmuxCommandError(f"tmux session '{new_name}' already exists")

    result = _run_tmux(["rename-session", "-t", session_name, new_name], check=False)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise TmuxCommandError(stderr or f"failed to rename '{session_name}' to '{new_name}'")


def send_keys(
    session_name: str,
    message: str,
    press_enter: bool = True,
    enter_delay_ms: int = 0,
) -> None:
    if not session_exists(session_name):
        raise TmuxSessionNotFoundError(f"tmux session '{session_name}' was not found")

    result = _run_tmux(["send-keys", "-t", session_name, message], check=False, timeout=30)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise TmuxCommandError(stderr or f"failed to send keys to '{session_name}'")

    if press_enter:
        if enter_delay_ms > 0:
            time.sleep(enter_delay_ms / 1000)
        result = _run_tmux(["send-keys", "-t", session_name, "Enter"], check=False, timeout=30)
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise TmuxCommandError(stderr or f"failed to send Enter to '{session_name}'")
