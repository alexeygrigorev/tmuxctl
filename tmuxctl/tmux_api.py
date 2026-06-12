from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
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
    args: list[str], *, check: bool = True, timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    _ensure_tmux()
    try:
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
    unit = robust.scope_unit_name(session_name)
    mem = _resolve_session_mem(cwd, flag=mem)

    if mem is not None and robust.systemd_available():
        # Ensure the parent slice exists before any capped session joins it.
        try:
            robust.ensure_slice(robust.resolve_slice_max())
        except Exception:  # noqa: BLE001 - slice bound is best-effort
            pass
        wrapped = robust.scope_wrap(_login_shell(), unit, mem)
        command = ["new-session", "-d", "-s", session_name, "-c", cwd, *wrapped]
    else:
        if mem is not None:
            print(
                "tmuxctl: systemd-run unavailable; session runs without a memory cap",
                file=sys.stderr,
            )
        command = ["new-session", "-d", "-s", session_name, "-c", cwd]

    result = _run_tmux(command, check=False)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise TmuxCommandError(stderr or f"failed to create session '{session_name}'")

    if shell_command:
        send_keys(session_name, shlex.join(shell_command), press_enter=True, enter_delay_ms=0)

    attach_session(session_name, resize_window=resize_window)


def _resolve_session_mem(cwd: str, *, flag: str | None = None) -> str | None:
    """Resolve the per-session MemoryMax ceiling for a new session."""
    try:
        return robust.resolve_mem(flag=flag, cwd=Path(cwd))
    except Exception:  # noqa: BLE001 - never block session creation on config
        return robust.DEFAULT_MEM


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
