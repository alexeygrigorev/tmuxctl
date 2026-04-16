from __future__ import annotations

import os
import shutil
import subprocess
import time

from tmuxctl.models import SessionInfo


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


def session_exists(name: str) -> bool:
    result = _run_tmux(["has-session", "-t", name], check=False)
    return result.returncode == 0


def attach_session(session_name: str) -> None:
    if not session_exists(session_name):
        raise TmuxSessionNotFoundError(f"tmux session '{session_name}' was not found")

    inside_tmux = bool(os.environ.get("TMUX"))
    command = ["switch-client", "-t", session_name] if inside_tmux else ["attach-session", "-t", session_name]
    result = _run_tmux(command, check=False)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise TmuxCommandError(stderr or f"failed to attach to '{session_name}'")


def create_or_attach_session(session_name: str) -> None:
    if session_exists(session_name):
        attach_session(session_name)
        return

    inside_tmux = bool(os.environ.get("TMUX"))
    command = ["new-session", "-d", "-s", session_name] if inside_tmux else ["new-session", "-s", session_name]
    result = _run_tmux(command, check=False)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise TmuxCommandError(stderr or f"failed to create session '{session_name}'")

    if inside_tmux:
        attach_session(session_name)


def kill_session(session_name: str) -> None:
    if not session_exists(session_name):
        raise TmuxSessionNotFoundError(f"tmux session '{session_name}' was not found")

    result = _run_tmux(["kill-session", "-t", session_name], check=False)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise TmuxCommandError(stderr or f"failed to kill '{session_name}'")


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
