"""Memory-capped systemd --user scope wrapping for tmux sessions.

Root cause this module guards against: uncapped heavy work (builds/agents)
exhausts RAM, the kernel's machine-wide OOM-killer kills the shared tmux
SERVER, and every session dies at once. By launching each session's process
tree inside a memory-capped systemd --user scope (cgroup v2), bounded by a
parent workload slice, a runaway is OOM-killed in isolation. The tmux server
runs in a separate, uncapped server slice so aggregate workload pressure cannot
select it when the workload slice hits its limit.

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
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # Python 3.10: tomllib is not in the stdlib
    import tomli as tomllib

# Built-in defaults (config precedence step 5).
DEFAULT_MEM = "12G"
DEFAULT_RESERVE = "8G"
DEFAULT_SWAP = "8G"
# Summed MemoryMax across every session scope may exceed system capacity by
# this much before a warning fires (some oversubscription is fine since
# sessions rarely all peak at once). 0 disables the check.
DEFAULT_OVERSUBSCRIPTION_MAX_PCT = 120
# MemoryHigh defaults to this fraction of MemoryMax: the kernel reclaims and
# throttles allocations above MemoryHigh, only hard-killing at MemoryMax, so a
# scope slows down and waits under transient pressure instead of OOM-dying.
DEFAULT_HIGH_FRACTION = 85  # percent of mem

_SLICE_NAME = "robust.slice"
_SERVER_SLICE_NAME = "tmuxctl-server.slice"
_PYPROJECT_FILE_NAME = "pyproject.toml"
_PROJECT_CONFIG_NAME = "cgroups.toml"

# Dedicated systemd unit that owns the shared tmux server's cgroup, so the
# server lives independently of any SSH login session scope (see
# ``server_bootstrap_argv``).
_SERVER_UNIT = "tmuxctl-server"
# Hidden session created then immediately killed to start the server cleanly;
# ``exit-empty off`` keeps the server alive afterwards with zero sessions.
_SERVER_BOOTSTRAP_SESSION = "__tmuxctl_server__"
# Strongly negative OOM score so the machine-wide OOM killer strongly avoids
# the tmux server. The server is intentionally outside ``robust.slice`` so a
# parent-slice cgroup OOM cannot pick it at all.
_SERVER_OOM_SCORE_ADJUST = "-900"

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


def server_unit_name() -> str:
    """The systemd unit base name owning the shared tmux server (no suffix).

    Legacy: only pre-migration sessions still on the shared default socket
    run under this unit. New sessions bootstrap their own dedicated server
    via :func:`session_server_unit` instead — see "Per-session tmux
    servers" in docs/oom-recovery-plan.md.
    """
    return _SERVER_UNIT


def server_slice_name() -> str:
    """The uncapped systemd user slice that owns tmux server processes.

    Shared by the legacy server unit and every per-session server unit —
    it's just the "servers live here, uncapped" slice, not itself a
    correlated-failure risk (a slice has no process of its own to kill).
    """
    return _SERVER_SLICE_NAME


def socket_name(session_name: str) -> str:
    """The ``-L``-style socket basename for a session's own dedicated server."""
    return f"tmuxctl-{session_name}"


def socket_for(session_name: str, uid: int | None = None) -> str:
    """Deterministic path to a session's own dedicated tmux socket.

    Lands under the same directory ``strays.list_socket_paths()`` already
    scans (``$TMUX_TMPDIR|/tmp/tmux-<uid>/``), so no scanner changes are
    needed to see per-session sockets. A pure function of the session name:
    no directory creation, no XDG fallback, nothing to keep in sync.
    """
    uid = os.getuid() if uid is None else uid
    tmpdir = os.environ.get("TMUX_TMPDIR") or "/tmp"
    path = f"{tmpdir}/tmux-{uid}/{socket_name(session_name)}"
    # AF_UNIX sun_path is capped at 108 bytes on Linux; fail loudly rather
    # than silently truncating to a wrong/colliding socket path.
    if len(path.encode()) >= 108:
        raise ValueError(f"socket path too long for AF_UNIX (>=108 bytes): {path!r}")
    return path


def session_server_unit(session_name: str) -> str:
    """The systemd unit base name for a session's own dedicated tmux server."""
    return f"tmuxctl-server-{session_name}"


def session_server_bootstrap_argv(session_name: str, tmux_argv: list[str]) -> list[str]:
    """Argv that starts a session's OWN tmux server, in its own persistent
    systemd unit, immediately running ``tmux_argv`` (typically a
    ``new-session -d ...``) on that session's dedicated socket.

    Unlike the legacy shared-server bootstrap, this collapses "start the
    server" and "create the session" into one step: each per-session server
    exists for exactly one session, so there's no separate empty-server
    keepalive dance (``exit-empty`` stays at its default "on" — the server
    exits on its own once that one session ends, and systemd notices the
    cgroup went empty and reaps the unit). ``OOMScoreAdjust`` keeps this
    session's own workload pressure from picking the multiplexer over the
    workload it's multiplexing, same rationale as the legacy server bootstrap
    — just scoped down to one session so a crash is contained to it alone.
    """
    unit = session_server_unit(session_name)
    return [
        "systemd-run",
        "--user",
        f"--unit={unit}",
        "-p",
        "Type=forking",
        "-p",
        f"Slice={_SERVER_SLICE_NAME}",
        "-p",
        f"OOMScoreAdjust={_SERVER_OOM_SCORE_ADJUST}",
        "--quiet",
        "--",
        "tmux",
        "-S",
        socket_for(session_name),
        *tmux_argv,
    ]


def reset_session_server_unit(session_name: str) -> None:
    """Free a dead per-session server unit so it can be re-bootstrapped.

    See :func:`_reset_unit`: a crashed per-session server can leave its
    unit ``failed``, which would make ``systemd-run --unit=`` refuse the
    name on the next create for that same session name.
    """
    _reset_unit(f"{session_server_unit(session_name)}.service")


def stop_session_server(session_name: str) -> None:
    """Best-effort stop of a session's dedicated server unit. Errors ignored.

    Usually a no-op by the time this runs: ``exit-empty`` already exits the
    server (and its now-empty cgroup lets systemd reap the unit on its own)
    the moment ``kill_session`` ends that session. Explicit stop just makes
    teardown immediate instead of racing that natural exit, and is harmless
    to call for a legacy (pre-migration, shared-server) session, where the
    unit never existed in the first place.
    """
    if not systemd_available():
        return
    unit = f"{session_server_unit(session_name)}.service"
    try:
        subprocess.run(
            ["systemctl", "--user", "stop", unit],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, FileNotFoundError):
        pass


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


def total_swap_bytes(meminfo_path: str = "/proc/meminfo") -> int:
    """Read ``SwapTotal`` from /proc/meminfo (kB) and return bytes."""
    try:
        with open(meminfo_path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("SwapTotal:"):
                    parts = line.split()
                    return int(parts[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def system_capacity(meminfo_path: str = "/proc/meminfo") -> int:
    """Physical RAM + swap: the ceiling aggregate session memory caps should
    stay within, with headroom (see :func:`resolve_oversubscription_max_pct`)."""
    return total_ram_bytes(meminfo_path) + total_swap_bytes(meminfo_path)


# ---------------------------------------------------------------------------
# Config sources
# ---------------------------------------------------------------------------
def _user_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "tmuxctl" / "cgroups.toml"
    return Path.home() / ".config" / "tmuxctl" / "cgroups.toml"


def read_user_config(path: Path | None = None) -> dict[str, str]:
    """Read ``~/.config/tmuxctl/cgroups.toml`` cgroup policy keys."""
    cfg_path = path or _user_config_path()
    try:
        with open(cfg_path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    result: dict[str, str] = {}
    for key in (
        "default_mem",
        "default_swap",
        "default_high",
        "slice_max",
        "slice_swap_max",
        "reserve",
        "oversubscription_max_pct",
        "dtach_wrap",
        "auto_salvage",
    ):
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


def read_project_value(key: str, cwd: Path | None = None) -> str | None:
    """Read a per-project tmuxctl cgroup value from cwd or its git root.

    Two sources are checked, per directory: a dedicated ``cgroups.toml``
    (top-level key, for non-Python projects), then a ``[tool.tmuxctl]`` key
    in ``pyproject.toml``. The dedicated file
    wins when both are present. First directory match wins (cwd before git
    root).
    """
    base = (cwd or Path.cwd()).resolve()
    dirs: list[Path] = [base]
    root = _git_root(base)
    if root is not None and root != base:
        dirs.append(root)

    for directory in dirs:
        value = _parse_project_cgroups(directory / _PROJECT_CONFIG_NAME, key)
        if value is not None:
            return value
        value = _parse_pyproject_value(directory / _PYPROJECT_FILE_NAME, key)
        if value is not None:
            return value
    return None


def read_project_mem(cwd: Path | None = None) -> str | None:
    """Read the per-project session mem cap from cwd or its git root."""
    return read_project_value("mem", cwd=cwd)


def _parse_project_cgroups(path: Path, key: str = "mem") -> str | None:
    """Read a top-level key from a project ``cgroups.toml`` file."""
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    value = data.get(key)
    return str(value) if value is not None else None


def _parse_pyproject_value(path: Path, key: str = "mem") -> str | None:
    """Read a ``[tool.tmuxctl]`` key from a ``pyproject.toml`` file."""
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    value = data.get("tool", {}).get("tmuxctl", {}).get(key)
    return str(value) if value is not None else None


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
      3. per-project ``cgroups.toml`` or ``pyproject.toml`` ``[tool.tmuxctl]``
         (cwd or git root)
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


def resolve_swap(
    *,
    flag: str | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    user_config: dict[str, str] | None = None,
) -> str:
    """Resolve the per-session MemorySwapMax allowance, first match wins.

    Precedence mirrors ``resolve_mem``:
      1. explicit value
      2. env var ``ROBUST_TMUX_SWAP``
      3. per-project ``cgroups.toml`` or ``pyproject.toml`` ``[tool.tmuxctl]``
      4. user config ``default_swap``
      5. built-in default ``DEFAULT_SWAP`` (8G)
    """
    env = os.environ if env is None else env

    if flag is not None:
        parse_size(flag)
        return flag

    env_swap = env.get("ROBUST_TMUX_SWAP")
    if env_swap is not None:
        parse_size(env_swap)
        return env_swap

    project_swap = read_project_value("swap", cwd=cwd)
    if project_swap is not None:
        parse_size(project_swap)
        return project_swap

    cfg = read_user_config() if user_config is None else user_config
    if cfg.get("default_swap") is not None:
        value = cfg["default_swap"]
        parse_size(value)
        return value

    return DEFAULT_SWAP


def default_high_for(mem: str) -> str:
    """The computed MemoryHigh default: ``DEFAULT_HIGH_FRACTION`` % of ``mem``."""
    high_bytes = parse_size(mem) * DEFAULT_HIGH_FRACTION // 100
    return format_size(high_bytes)


def resolve_high(
    mem: str,
    *,
    flag: str | None = None,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    user_config: dict[str, str] | None = None,
) -> str:
    """Resolve the per-session MemoryHigh soft-throttle threshold.

    Precedence mirrors ``resolve_swap``, except the built-in default is
    *computed* from ``mem`` (``DEFAULT_HIGH_FRACTION`` % of MemoryMax) rather
    than a fixed constant:
      1. explicit value
      2. env var ``ROBUST_TMUX_HIGH``
      3. per-project ``cgroups.toml`` or ``pyproject.toml`` ``[tool.tmuxctl]``
      4. user config ``default_high``
      5. computed ``DEFAULT_HIGH_FRACTION`` % of ``mem``
    """
    env = os.environ if env is None else env

    if flag is not None:
        parse_size(flag)
        return flag

    env_high = env.get("ROBUST_TMUX_HIGH")
    if env_high is not None:
        parse_size(env_high)
        return env_high

    project_high = read_project_value("high", cwd=cwd)
    if project_high is not None:
        parse_size(project_high)
        return project_high

    cfg = read_user_config() if user_config is None else user_config
    if cfg.get("default_high") is not None:
        value = cfg["default_high"]
        parse_size(value)
        return value

    return default_high_for(mem)


def resolve_oversubscription_max_pct(
    *,
    env: dict[str, str] | None = None,
    user_config: dict[str, str] | None = None,
) -> int:
    """Resolve the oversubscription warning threshold, as % of system capacity.

    Precedence: env var ``ROBUST_TMUX_OVERSUBSCRIPTION_MAX_PCT`` -> user config
    ``oversubscription_max_pct`` -> built-in default (120). ``0`` disables the
    check entirely.
    """
    env = os.environ if env is None else env

    env_value = env.get("ROBUST_TMUX_OVERSUBSCRIPTION_MAX_PCT")
    if env_value is not None:
        return int(env_value)

    cfg = read_user_config() if user_config is None else user_config
    if cfg.get("oversubscription_max_pct") is not None:
        return int(cfg["oversubscription_max_pct"])

    return DEFAULT_OVERSUBSCRIPTION_MAX_PCT


def total_reserved_mem() -> int:
    """Sum of ``MemoryMax`` across every live ``tmuxctl-*.scope`` unit, bytes.

    Enumerates systemd units directly rather than live tmux sessions, so a
    scope whose tmux process already died (but whose cgroup/unit is still
    around) still counts — exactly the case an oversubscription total must
    not silently drop. Unset/``infinity`` caps are skipped (uncapped
    fallback sessions don't reserve anything, which is itself the signal
    ``doctor`` and creation-time warnings should surface separately).
    """
    if not systemd_available():
        return 0
    try:
        result = subprocess.run(
            [
                "systemctl", "--user", "list-units", "tmuxctl-*.scope",
                "--all", "--no-legend", "--plain",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, FileNotFoundError):
        return 0
    if result.returncode != 0:
        return 0

    total = 0
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        unit = line.split()[0]
        if not unit.endswith(".scope"):
            continue
        raw = scope_property(unit, "MemoryMax")
        if raw in (None, "", "[not set]", "infinity"):
            continue
        try:
            total += int(raw)
        except ValueError:
            continue
    return total


def resolve_dtach_wrap(
    *,
    env: dict[str, str] | None = None,
    user_config: dict[str, str] | None = None,
) -> bool:
    """Resolve whether new sessions get pty-durable dtach wrapping (§1).

    Opt-in, default off: flip it on (globally via config, or per-invocation
    via the env var) once you've prototyped it against a real session --
    see docs/oom-recovery-plan.md, §1's "Scope note". Precedence: env var
    ``ROBUST_TMUX_DTACH_WRAP`` -> user config ``dtach_wrap`` -> off.
    """
    env = os.environ if env is None else env
    truthy = {"1", "true", "yes", "on"}

    env_value = env.get("ROBUST_TMUX_DTACH_WRAP")
    if env_value is not None:
        return env_value.strip().lower() in truthy

    cfg = read_user_config() if user_config is None else user_config
    if cfg.get("dtach_wrap") is not None:
        return str(cfg["dtach_wrap"]).strip().lower() in truthy

    return False


def resolve_auto_salvage(
    *,
    env: dict[str, str] | None = None,
    user_config: dict[str, str] | None = None,
) -> bool:
    """Resolve whether the daemon health check (§4) is allowed to actually
    recreate an unhealthy session on its own, versus only detecting and
    logging it for a human to run ``tmuxctl salvage --recreate``.

    Opt-in, default off: automatic recreation is a materially bigger blast
    radius than the old shared-server's idempotent ``ensure_server()``
    no-op, so it needs an explicit choice to trust. Precedence: env var
    ``ROBUST_TMUX_AUTO_SALVAGE`` -> user config ``auto_salvage`` -> off.
    """
    env = os.environ if env is None else env
    truthy = {"1", "true", "yes", "on"}

    env_value = env.get("ROBUST_TMUX_AUTO_SALVAGE")
    if env_value is not None:
        return env_value.strip().lower() in truthy

    cfg = read_user_config() if user_config is None else user_config
    if cfg.get("auto_salvage") is not None:
        return str(cfg["auto_salvage"]).strip().lower() in truthy

    return False


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


def resolve_slice_swap_max(
    *,
    user_config: dict[str, str] | None = None,
) -> str:
    """Resolve the parent-slice MemorySwapMax allowance."""
    cfg = read_user_config() if user_config is None else user_config

    if cfg.get("slice_swap_max") is not None:
        value = cfg["slice_swap_max"]
        parse_size(value)
        return value

    return DEFAULT_SWAP


# ---------------------------------------------------------------------------
# Slice unit management
# ---------------------------------------------------------------------------
def _slice_unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "systemd" / "user" / _SLICE_NAME
    return Path.home() / ".config" / "systemd" / "user" / _SLICE_NAME


def ensure_slice(
    slice_max: str,
    *,
    swap_max: str | None = None,
    unit_path: Path | None = None,
) -> bool:
    """Ensure ``~/.config/systemd/user/robust.slice`` exists with MemoryMax.

    Writes the unit (and runs ``systemctl --user daemon-reload``) only when
    the desired content differs from what is on disk. Returns True when the
    unit was created or changed. No-op + returns False if systemd is absent.
    """
    if not systemd_available():
        return False

    resolved_swap = resolve_slice_swap_max() if swap_max is None else swap_max
    parse_size(resolved_swap)

    path = unit_path or _slice_unit_path()
    desired = (
        "[Slice]\n"
        f"MemoryMax={slice_max}\n"
        f"MemorySwapMax={resolved_swap}\n"
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
def scope_wrap(
    cmd: list[str],
    unit: str,
    mem: str,
    *,
    swap: str | None = None,
    high: str | None = None,
) -> list[str]:
    """Wrap ``cmd`` so it runs inside a memory-capped systemd --user scope.

    Builds the ``systemd-run`` argv. ``cmd`` is the login shell (or any
    command) to run inside the scope; every command launched within the
    session inherits the cgroup cap.

    ``MemoryHigh`` (the soft throttle threshold, default
    ``DEFAULT_HIGH_FRACTION`` % of ``mem``) makes the scope reclaim/throttle
    under pressure before the hard ``MemoryMax`` wall, so heavy work slows and
    waits instead of being OOM-killed. ``MemorySwapMax`` gives it room to spill
    cold pages while it throttles.
    """
    resolved_swap = resolve_swap() if swap is None else swap
    parse_size(resolved_swap)
    resolved_high = resolve_high(mem) if high is None else high
    parse_size(resolved_high)

    return [
        "systemd-run",
        "--user",
        "--scope",
        f"--unit={unit}",
        "-p",
        f"MemoryHigh={resolved_high}",
        "-p",
        f"MemoryMax={mem}",
        "-p",
        f"MemorySwapMax={resolved_swap}",
        "-p",
        f"Slice={_SLICE_NAME}",
        "--quiet",
        "--",
        *cmd,
    ]


def server_bootstrap_argv(unit: str | None = None) -> list[str]:
    """Argv that starts the tmux SERVER in its OWN persistent systemd unit.

    The shared tmux server normally inherits the cgroup of whichever SSH login
    first spawns it (``session-<N>.scope``, managed by logind). When that login
    logs out, systemd reaps the session scope and the server dies inside it,
    taking *every* session on the server down at once — the failure this guards
    against (and which a per-session ``scope_wrap`` does NOT, because it caps the
    pane shell, not the server).

    Starting the server in a dedicated ``Type=forking`` service under
    ``tmuxctl-server.slice`` — independent of any login and separate from the
    memory-capped workload ``robust.slice`` — makes it survive login teardown,
    per-session OOM, and aggregate workload-slice OOM. ``exit-empty off`` keeps
    it alive with zero sessions; a hidden bootstrap session is created then
    immediately killed so the server starts cleanly. Uses the default socket
    (tmuxctl's socket).
    """
    unit = unit or server_unit_name()
    return [
        "systemd-run",
        "--user",
        f"--unit={unit}",
        "-p",
        "Type=forking",
        "-p",
        f"Slice={_SERVER_SLICE_NAME}",
        "-p",
        f"OOMScoreAdjust={_SERVER_OOM_SCORE_ADJUST}",
        "--quiet",
        "--",
        "tmux",
        "new-session",
        "-d",
        "-s",
        _SERVER_BOOTSTRAP_SESSION,
        ";",
        "set",
        "-g",
        "exit-empty",
        "off",
        ";",
        "kill-session",
        "-t",
        _SERVER_BOOTSTRAP_SESSION,
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


def _reset_unit(unit: str) -> None:
    """stop + reset-failed a fully-qualified ``--user`` unit. Errors ignored.

    A transient unit whose processes have all exited can linger in a ``failed``
    or inactive-but-loaded state instead of being garbage-collected.
    ``systemd-run --unit=<name>`` then refuses the name ("already loaded") on the
    next same-named bootstrap, so stop it and clear any failed state to release
    the name. Best-effort: errors (including no such unit) are ignored."""
    if not systemd_available():
        return
    for verb in ("stop", "reset-failed"):
        try:
            subprocess.run(
                ["systemctl", "--user", verb, unit],
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, FileNotFoundError):
            pass


def reset_scope(unit: str) -> None:
    """Free a dead session scope name so ``systemd-run --scope --unit=`` reuses it.

    See :func:`_reset_unit`: a same-named session scope that outlived its session
    blocks the next create. ``unit`` is the bare unit base (``.scope`` appended)."""
    _reset_unit(unit if unit.endswith(".scope") else f"{unit}.scope")


def reset_server_unit() -> None:
    """Free a dead ``tmuxctl-server.service`` so the server can be re-bootstrapped.

    The server runs in a fixed-name forking ``.service`` (see
    :func:`server_bootstrap_argv`). If a previous server crashed and left the
    unit ``failed``, ``systemd-run --unit=tmuxctl-server`` would refuse the name
    and the server would silently fall back to an unprotected login scope."""
    _reset_unit(f"{_SERVER_UNIT}.service")


def scope_properties(unit: str, props: list[str]) -> dict[str, str]:
    """Read several systemd properties from a session's scope in one call.

    Returns a ``{property: value}`` dict (empty when systemd is unavailable or
    the unit cannot be shown). Useful for describing a running scope's cgroup,
    memory, and CPU usage without one ``systemctl show`` per field.
    """
    if not systemd_available() or not props:
        return {}
    scope = unit if unit.endswith(".scope") else f"{unit}.scope"
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", scope, "-p", ",".join(props)],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, FileNotFoundError):
        return {}
    if result.returncode != 0:
        return {}
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


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


# ---------------------------------------------------------------------------
# Pty-durable pane wrapping (§1: dtach)
# ---------------------------------------------------------------------------
# §0 contains a session's crash to itself; it does not stop that session's
# OWN tmux server from dying (its own workload can still hit its own cap, or
# anything else can kill that one process). dtach moves the pty one hop
# below tmux: the real command runs behind a tiny dtach master that is a
# SIBLING of the tmux server, not its descendant, so the server dying never
# signals it. Opt-in (see resolve_dtach_wrap) -- prototype against one real
# session before trusting it as the default for every session.


def dtach_available() -> bool:
    """True when ``dtach`` is on PATH. Degrades gracefully when absent: the
    caller falls back to the bare (non-durable) shell, same philosophy as
    every other optional protection layer in this module."""
    return shutil.which("dtach") is not None


def dtach_socket_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return Path(base) / "tmuxctl" / "dtach"
    return Path.home() / ".local" / "state" / "tmuxctl" / "dtach"


def dtach_socket_for(session_name: str, pane_id: str = "0") -> Path:
    """Deterministic path to a pane's durable dtach socket.

    This is the layer that outlives the tmux server: the process behind it
    keeps running whether or not anything is attached, and whether or not
    the tmux server that originally spawned it is still alive. ``pane_id``
    distinguishes panes/windows beyond a session's initial one (not yet
    wired up by default -- see docs/oom-recovery-plan.md §1's note on
    default-command wrapping for interactively-created panes/windows).
    """
    directory = dtach_socket_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory / f"{session_name}-{pane_id}.sock"


def dtach_wrap(cmd: list[str], socket: Path) -> list[str]:
    """Wrap ``cmd`` so it runs behind a durable dtach master.

    ``-E`` disables dtach's own detach hotkey (otherwise it steals ``^\\``),
    ``-z`` disables its suspend key, ``-r winch`` forces a repaint on
    attach so the pane isn't blank until the app redraws on its own (apps
    that handle SIGWINCH -- ``claude``, vim, most TUIs -- redraw
    immediately; a bare shell prompt looks blank until Enter, which is
    expected).
    """
    return ["dtach", "-A", str(socket), "-E", "-z", "-r", "winch", "--", *cmd]


def dtach_attach_argv(socket: Path) -> list[str]:
    """Reattach to an already-running dtach master (the post-crash recreate
    case: the tmux server died, this session's dtach master didn't)."""
    return ["dtach", "-a", str(socket)]


def scope_cgroup_path(unit: str) -> str | None:
    """The cgroup v2 path systemd reports owning a scope's live processes."""
    return scope_property(unit, "ControlGroup")


def cgroup_proc_pids(cgroup_path: str) -> list[int]:
    """Live PIDs in a cgroup v2 path, as systemd's ``ControlGroup`` reports it."""
    if not cgroup_path.startswith("/"):
        return []
    path = Path("/sys/fs/cgroup") / cgroup_path.lstrip("/") / "cgroup.procs"
    try:
        return [int(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    except (OSError, ValueError):
        return []


def proc_comm(pid: int) -> str | None:
    """The command name (``comm``) of a process, or None if unreadable/gone."""
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def scope_occupied_only_by_dtach(unit: str) -> bool:
    """True when an 'active' scope's only live processes are dtach masters.

    Distinguishes the expected §1 post-crash recreate case (this session's
    own dtach master survived its tmux server dying, so the scope is still
    "active" by design) from genuine squatting by a foreign/unexpected
    occupant, which :func:`tmux_api._new_session_command` must still hard
    -stop rather than silently reclaim.
    """
    cgroup = scope_cgroup_path(unit if unit.endswith(".scope") else f"{unit}.scope")
    if not cgroup:
        return False
    pids = cgroup_proc_pids(cgroup)
    if not pids:
        return False
    return all(proc_comm(pid) == "dtach" for pid in pids)
