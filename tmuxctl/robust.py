"""Memory-capped systemd --user scope wrapping for tmux sessions.

Root cause this module guards against: uncapped heavy work (builds/agents)
exhausts RAM, the kernel's machine-wide OOM-killer kills the shared tmux
SERVER, and every session dies at once. By launching each session's process
tree inside a memory-capped systemd --user scope (cgroup v2), bounded by a
parent slice, a runaway is OOM-killed in isolation and the tmux server
(outside any scope) always survives.

All functions degrade gracefully: when ``systemd-run`` is unavailable
(containers/CI), the session runs uncapped and a one-line warning is emitted
instead of blocking session creation.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

# Built-in defaults (config precedence step 5).
DEFAULT_MEM = "12G"
DEFAULT_RESERVE = "8G"

_SLICE_NAME = "robust.slice"
_PROJECT_FILE_NAME = ".robust-tmux"

_SIZE_UNITS = {
    "B": 1,
    "K": 1024,
    "KB": 1024,
    "KIB": 1024,
    "M": 1024**2,
    "MB": 1024**2,
    "MIB": 1024**2,
    "G": 1024**3,
    "GB": 1024**3,
    "GIB": 1024**3,
    "T": 1024**4,
    "TB": 1024**4,
    "TIB": 1024**4,
}


# ---------------------------------------------------------------------------
# Size parsing / formatting
# ---------------------------------------------------------------------------
def parse_size(value: str | int) -> int:
    """Parse a human size like ``24G`` / ``512M`` / ``1024`` into bytes."""
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        raise ValueError("empty size value")
    match = re.fullmatch(r"(?i)\s*([0-9]+(?:\.[0-9]+)?)\s*([a-z]*)\s*", text)
    if not match:
        raise ValueError(f"invalid size: {value!r}")
    number = float(match.group(1))
    unit = match.group(2).upper() or "B"
    if unit not in _SIZE_UNITS:
        raise ValueError(f"invalid size unit in {value!r}")
    return int(number * _SIZE_UNITS[unit])


def format_size(num_bytes: int) -> str:
    """Format a byte count back to a compact ``systemd``-friendly string."""
    for unit, factor in (("T", 1024**4), ("G", 1024**3), ("M", 1024**2), ("K", 1024)):
        if num_bytes % factor == 0 and num_bytes >= factor:
            return f"{num_bytes // factor}{unit}"
    return str(num_bytes)


# ---------------------------------------------------------------------------
# Capability detection
# ---------------------------------------------------------------------------
def systemd_available() -> bool:
    """True when ``systemd-run`` is on PATH (cgroup v2 user scopes usable)."""
    return shutil.which("systemd-run") is not None


def _warn(message: str) -> None:
    print(f"tmuxctl: {message}", file=sys.stderr)


def scope_unit_name(session_name: str) -> str:
    """The systemd unit base name for a session (without ``.scope``)."""
    return f"tmuxctl-{session_name}"


# ---------------------------------------------------------------------------
# RAM total
# ---------------------------------------------------------------------------
def total_ram_bytes(meminfo_path: str = "/proc/meminfo") -> int:
    """Read ``MemTotal`` from /proc/meminfo (kB) and return bytes."""
    try:
        with open(meminfo_path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    # Format: "MemTotal:  16314084 kB"
                    return int(parts[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


# ---------------------------------------------------------------------------
# Config sources
# ---------------------------------------------------------------------------
def _user_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "tmuxctl" / "robust.toml"
    return Path.home() / ".config" / "tmuxctl" / "robust.toml"


def read_user_config(path: Path | None = None) -> dict[str, str]:
    """Read ``~/.config/tmuxctl/robust.toml`` keys default_mem/slice_max/reserve."""
    cfg_path = path or _user_config_path()
    try:
        with open(cfg_path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    result: dict[str, str] = {}
    for key in ("default_mem", "slice_max", "reserve"):
        if key in data and data[key] is not None:
            result[key] = str(data[key])
    return result


def _git_root(start: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    return Path(top) if top else None


def read_project_mem(cwd: Path | None = None) -> str | None:
    """Read ``mem`` from a ``.robust-tmux`` file in cwd or its git root.

    The file may contain ``mem = "24G"`` (TOML-ish) or a bare size line
    like ``24G``. First match wins (cwd before git root).
    """
    base = (cwd or Path.cwd()).resolve()
    candidates: list[Path] = [base / _PROJECT_FILE_NAME]
    root = _git_root(base)
    if root is not None:
        candidate = root / _PROJECT_FILE_NAME
        if candidate not in candidates:
            candidates.append(candidate)

    for candidate in candidates:
        value = _parse_project_file(candidate)
        if value is not None:
            return value
    return None


def _parse_project_file(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # "mem = 24G" / 'mem = "24G"'
        match = re.match(r"^mem\s*=\s*(.+)$", line, re.IGNORECASE)
        if match:
            return match.group(1).strip().strip("'\"")
        # bare size line
        if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?\s*[A-Za-z]*", line):
            return line
    return None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def resolve_mem(
    *,
    flag: str | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    user_config: dict[str, str] | None = None,
) -> str:
    """Resolve the per-session MemoryMax ceiling, first match wins.

    Precedence:
      1. explicit ``--mem`` flag
      2. env var ``ROBUST_TMUX_MEM``
      3. per-project ``.robust-tmux`` (cwd or git root)
      4. user config ``default_mem``
      5. built-in default ``DEFAULT_MEM`` (12G)
    """
    env = os.environ if env is None else env

    # 1. flag
    if flag:
        parse_size(flag)  # validate
        return flag

    # 2. env
    env_mem = env.get("ROBUST_TMUX_MEM")
    if env_mem:
        parse_size(env_mem)
        return env_mem

    # 3. project file
    project_mem = read_project_mem(cwd=cwd)
    if project_mem:
        parse_size(project_mem)
        return project_mem

    # 4. user config
    cfg = read_user_config() if user_config is None else user_config
    if cfg.get("default_mem"):
        value = cfg["default_mem"]
        parse_size(value)
        return value

    # 5. default
    return DEFAULT_MEM


def resolve_slice_max(
    *,
    user_config: dict[str, str] | None = None,
    meminfo_path: str = "/proc/meminfo",
) -> str:
    """Resolve the parent-slice ceiling (slice_max).

    Precedence:
      1. user config ``slice_max``
      2. computed ``RAM_total - reserve`` (reserve from user config or 8G default)
    """
    cfg = read_user_config() if user_config is None else user_config

    if cfg.get("slice_max"):
        value = cfg["slice_max"]
        parse_size(value)
        return value

    reserve = cfg.get("reserve") or DEFAULT_RESERVE
    reserve_bytes = parse_size(reserve)
    ram = total_ram_bytes(meminfo_path)
    slice_bytes = ram - reserve_bytes
    if slice_bytes <= 0:
        # Tiny box / unreadable meminfo: fall back to the per-session default
        # so the slice never undercuts a single capped session.
        slice_bytes = parse_size(DEFAULT_MEM)
    return format_size(slice_bytes)


# ---------------------------------------------------------------------------
# Slice unit management
# ---------------------------------------------------------------------------
def _slice_unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "systemd" / "user" / _SLICE_NAME
    return Path.home() / ".config" / "systemd" / "user" / _SLICE_NAME


def ensure_slice(slice_max: str, *, unit_path: Path | None = None) -> bool:
    """Ensure ``~/.config/systemd/user/robust.slice`` exists with MemoryMax.

    Writes the unit (and runs ``systemctl --user daemon-reload``) only when
    the desired content differs from what is on disk. Returns True when the
    unit was created or changed. No-op + returns False if systemd is absent.
    """
    if not systemd_available():
        return False

    path = unit_path or _slice_unit_path()
    desired = (
        "[Slice]\n"
        f"MemoryMax={slice_max}\n"
        "MemorySwapMax=0\n"
    )
    try:
        existing = path.read_text(encoding="utf-8")
    except OSError:
        existing = None

    if existing == desired:
        return False

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(desired, encoding="utf-8")
    except OSError as exc:
        _warn(f"could not write {path}: {exc}; sessions run without slice bound")
        return False

    try:
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, FileNotFoundError):
        pass
    return True


# ---------------------------------------------------------------------------
# Scope wrapping
# ---------------------------------------------------------------------------
def scope_wrap(cmd: list[str], unit: str, mem: str) -> list[str]:
    """Wrap ``cmd`` so it runs inside a memory-capped systemd --user scope.

    Builds the ``systemd-run`` argv. ``cmd`` is the login shell (or any
    command) to run inside the scope; every command launched within the
    session inherits the cgroup cap.
    """
    return [
        "systemd-run",
        "--user",
        "--scope",
        f"--unit={unit}",
        "-p",
        f"MemoryMax={mem}",
        "-p",
        "MemorySwapMax=0",
        "-p",
        f"Slice={_SLICE_NAME}",
        "--quiet",
        "--",
        *cmd,
    ]


def stop_scope(unit: str) -> None:
    """Stop a session's scope so its cgroup is reaped. Errors are ignored."""
    if not systemd_available():
        return
    scope = unit if unit.endswith(".scope") else f"{unit}.scope"
    try:
        subprocess.run(
            ["systemctl", "--user", "stop", scope],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, FileNotFoundError):
        pass


def scope_property(unit: str, prop: str) -> str | None:
    """Read a single systemd property from a session's scope, or None."""
    if not systemd_available():
        return None
    scope = unit if unit.endswith(".scope") else f"{unit}.scope"
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", scope, "-p", prop, "--value"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None
