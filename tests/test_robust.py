from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tmuxctl import robust


# ---------------------------------------------------------------------------
# parse_size / format_size
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        ("1024", 1024),
        ("1K", 1024),
        ("1KB", 1024),
        ("1M", 1024**2),
        ("1G", 1024**3),
        ("24G", 24 * 1024**3),
        ("1T", 1024**4),
        ("1.5G", int(1.5 * 1024**3)),
        (4096, 4096),
    ],
)
def test_parse_size(value, expected) -> None:
    assert robust.parse_size(value) == expected


def test_parse_size_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        robust.parse_size("12Z")
    with pytest.raises(ValueError):
        robust.parse_size("")


def test_format_size_roundtrip() -> None:
    assert robust.format_size(24 * 1024**3) == "24G"
    assert robust.format_size(512 * 1024**2) == "512M"
    assert robust.format_size(1024**4) == "1T"


# ---------------------------------------------------------------------------
# scope_wrap
# ---------------------------------------------------------------------------
def test_scope_wrap_builds_expected_argv() -> None:
    argv = robust.scope_wrap(["/bin/bash", "-l"], "tmuxctl-proj", "7G")
    expected_high = robust.format_size(robust.parse_size("7G") * 85 // 100)
    assert argv == [
        "systemd-run",
        "--user",
        "--scope",
        "--unit=tmuxctl-proj",
        "-p",
        f"MemoryHigh={expected_high}",
        "-p",
        "MemoryMax=7G",
        "-p",
        "MemorySwapMax=8G",
        "-p",
        "Slice=robust.slice",
        "--quiet",
        "--",
        "/bin/bash",
        "-l",
    ]


def test_scope_wrap_high_defaults_to_85_percent_of_mem() -> None:
    argv = robust.scope_wrap(["true"], "tmuxctl-proj", "20G")
    assert f"MemoryHigh={robust.default_high_for('20G')}" in argv
    # 85% of 20G stays strictly below the hard MemoryMax wall.
    high_idx = argv.index("MemoryHigh=" + robust.default_high_for("20G"))
    assert robust.parse_size(argv[high_idx].split("=", 1)[1]) < robust.parse_size("20G")


def test_scope_wrap_accepts_explicit_high() -> None:
    argv = robust.scope_wrap(["true"], "tmuxctl-proj", "20G", high="12G")
    assert "MemoryHigh=12G" in argv


def test_scope_wrap_accepts_explicit_swap() -> None:
    argv = robust.scope_wrap(["true"], "tmuxctl-proj", "7G", swap="2G")
    assert "MemorySwapMax=2G" in argv


def test_scope_wrap_allows_zero_swap() -> None:
    argv = robust.scope_wrap(["true"], "tmuxctl-proj", "7G", swap="0")
    assert "MemorySwapMax=0" in argv


def test_scope_unit_name() -> None:
    assert robust.scope_unit_name("git-proj") == "tmuxctl-git-proj"


# ---------------------------------------------------------------------------
# server_bootstrap_argv — the shared server lives in its OWN unit, not a login
# session scope, so a logout/per-session-OOM can't take every session down.
# ---------------------------------------------------------------------------
def test_server_unit_name() -> None:
    assert robust.server_unit_name() == "tmuxctl-server"


def test_server_slice_name() -> None:
    assert robust.server_slice_name() == "tmuxctl-server.slice"


def test_server_bootstrap_argv_starts_server_in_own_uncapped_slice() -> None:
    argv = robust.server_bootstrap_argv()
    # A persistent forking service under its own server slice — NOT a --scope
    # tied to a login session, NOT inheriting the caller's session-*.scope, and
    # NOT inside the workload-capped robust.slice.
    assert argv[:5] == [
        "systemd-run",
        "--user",
        "--unit=tmuxctl-server",
        "-p",
        "Type=forking",
    ]
    assert "--scope" not in argv
    assert "Slice=tmuxctl-server.slice" in argv
    assert "Slice=robust.slice" not in argv
    # Strongly negative OOM score: if machine-wide OOM happens, avoid picking
    # the shared tmux server.
    assert "OOMScoreAdjust=-900" in argv
    # Default socket (tmuxctl's socket) — no -L override.
    assert "-L" not in argv
    # exit-empty off keeps the server alive with zero sessions after the hidden
    # bootstrap session is killed; ';' separates the three tmux commands.
    tail = argv[argv.index("--") + 1 :]
    assert tail[:4] == ["tmux", "new-session", "-d", "-s"]
    assert ["set", "-g", "exit-empty", "off"] == tail[tail.index("set") : tail.index("set") + 4]
    assert "kill-session" in tail
    assert tail.count(";") == 2


def test_server_bootstrap_argv_accepts_explicit_unit() -> None:
    argv = robust.server_bootstrap_argv("tmuxctl-server-test")
    assert "--unit=tmuxctl-server-test" in argv


# ---------------------------------------------------------------------------
# resolve_mem precedence
# ---------------------------------------------------------------------------
def test_resolve_mem_flag_wins(monkeypatch, tmp_path) -> None:
    # Even with env + project + config set, the flag wins.
    (tmp_path / "pyproject.toml").write_text(
        '[tool.tmuxctl]\nmem = "30G"\n', encoding="utf-8"
    )
    monkeypatch.setattr(robust, "read_user_config", lambda *a, **k: {"default_mem": "20G"})
    result = robust.resolve_mem(
        flag="48G",
        env={"ROBUST_TMUX_MEM": "16G"},
        cwd=tmp_path,
    )
    assert result == "48G"


def test_resolve_mem_env_beats_project_and_config(monkeypatch, tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.tmuxctl]\nmem = "30G"\n', encoding="utf-8"
    )
    monkeypatch.setattr(robust, "read_user_config", lambda *a, **k: {"default_mem": "20G"})
    result = robust.resolve_mem(
        flag=None,
        env={"ROBUST_TMUX_MEM": "16G"},
        cwd=tmp_path,
    )
    assert result == "16G"


def test_resolve_mem_project_beats_config(monkeypatch, tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.tmuxctl]\nmem = "30G"\n', encoding="utf-8"
    )
    monkeypatch.setattr(robust, "_git_root", lambda start: None)
    result = robust.resolve_mem(
        flag=None,
        env={},
        cwd=tmp_path,
        user_config={"default_mem": "20G"},
    )
    assert result == "30G"


def test_resolve_mem_config_beats_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(robust, "_git_root", lambda start: None)
    result = robust.resolve_mem(
        flag=None,
        env={},
        cwd=tmp_path,
        user_config={"default_mem": "20G"},
    )
    assert result == "20G"


def test_resolve_mem_falls_back_to_builtin_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(robust, "_git_root", lambda start: None)
    result = robust.resolve_mem(flag=None, env={}, cwd=tmp_path, user_config={})
    assert result == robust.DEFAULT_MEM == "12G"


def test_resolve_mem_reads_git_root_pyproject(monkeypatch, tmp_path) -> None:
    root = tmp_path / "repo"
    sub = root / "a" / "b"
    sub.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[tool.tmuxctl]\nmem = "42G"\n', encoding="utf-8"
    )
    monkeypatch.setattr(robust, "_git_root", lambda start: root)
    result = robust.resolve_mem(flag=None, env={}, cwd=sub, user_config={})
    assert result == "42G"


def test_resolve_mem_reads_pyproject_tool_section(monkeypatch, tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.tmuxctl]\nmem = \"36G\"\n", encoding="utf-8"
    )
    monkeypatch.setattr(robust, "_git_root", lambda start: None)
    result = robust.resolve_mem(flag=None, env={}, cwd=tmp_path, user_config={})
    assert result == "36G"


def test_resolve_mem_reads_project_cgroups_toml(monkeypatch, tmp_path) -> None:
    (tmp_path / "cgroups.toml").write_text('mem = "30G"\n', encoding="utf-8")
    monkeypatch.setattr(robust, "_git_root", lambda start: None)
    result = robust.resolve_mem(flag=None, env={}, cwd=tmp_path, user_config={})
    assert result == "30G"


def test_resolve_mem_project_cgroups_beats_pyproject(monkeypatch, tmp_path) -> None:
    (tmp_path / "cgroups.toml").write_text('mem = "30G"\n', encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.tmuxctl]\nmem = "36G"\n', encoding="utf-8"
    )
    monkeypatch.setattr(robust, "_git_root", lambda start: None)
    result = robust.resolve_mem(flag=None, env={}, cwd=tmp_path, user_config={})
    assert result == "30G"


def test_resolve_mem_pyproject_without_tool_section(monkeypatch, tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = \"x\"\n", encoding="utf-8"
    )
    monkeypatch.setattr(robust, "_git_root", lambda start: None)
    result = robust.resolve_mem(flag=None, env={}, cwd=tmp_path, user_config={})
    assert result == robust.DEFAULT_MEM


# ---------------------------------------------------------------------------
# resolve_swap precedence
# ---------------------------------------------------------------------------
def test_resolve_swap_env_beats_project_and_config(monkeypatch, tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.tmuxctl]\nswap = "3G"\n', encoding="utf-8"
    )
    monkeypatch.setattr(robust, "read_user_config", lambda *a, **k: {"default_swap": "4G"})
    result = robust.resolve_swap(
        env={"ROBUST_TMUX_SWAP": "2G"},
        cwd=tmp_path,
    )
    assert result == "2G"


def test_resolve_swap_reads_project_cgroups_toml(monkeypatch, tmp_path) -> None:
    (tmp_path / "cgroups.toml").write_text('swap = "3G"\n', encoding="utf-8")
    monkeypatch.setattr(robust, "_git_root", lambda start: None)
    result = robust.resolve_swap(env={}, cwd=tmp_path, user_config={})
    assert result == "3G"


def test_resolve_swap_reads_pyproject_tool_section(monkeypatch, tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.tmuxctl]\nswap = "5G"\n', encoding="utf-8"
    )
    monkeypatch.setattr(robust, "_git_root", lambda start: None)
    result = robust.resolve_swap(env={}, cwd=tmp_path, user_config={})
    assert result == "5G"


def test_resolve_swap_config_beats_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(robust, "_git_root", lambda start: None)
    result = robust.resolve_swap(
        env={},
        cwd=tmp_path,
        user_config={"default_swap": "4G"},
    )
    assert result == "4G"


def test_resolve_swap_falls_back_to_builtin_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(robust, "_git_root", lambda start: None)
    result = robust.resolve_swap(env={}, cwd=tmp_path, user_config={})
    assert result == robust.DEFAULT_SWAP == "8G"


def test_resolve_swap_allows_zero_override(tmp_path) -> None:
    result = robust.resolve_swap(flag="0", env={}, cwd=tmp_path, user_config={})
    assert result == "0"


# ---------------------------------------------------------------------------
# resolve_high precedence (MemoryHigh soft-throttle threshold)
# ---------------------------------------------------------------------------
def test_resolve_high_flag_wins(monkeypatch, tmp_path) -> None:
    (tmp_path / "cgroups.toml").write_text('high = "10G"\n', encoding="utf-8")
    monkeypatch.setattr(robust, "read_user_config", lambda *a, **k: {"default_high": "9G"})
    result = robust.resolve_high(
        "30G", flag="24G", env={"ROBUST_TMUX_HIGH": "16G"}, cwd=tmp_path
    )
    assert result == "24G"


def test_resolve_high_env_beats_project_and_config(monkeypatch, tmp_path) -> None:
    (tmp_path / "cgroups.toml").write_text('high = "10G"\n', encoding="utf-8")
    monkeypatch.setattr(robust, "_git_root", lambda start: None)
    result = robust.resolve_high(
        "30G", env={"ROBUST_TMUX_HIGH": "16G"}, cwd=tmp_path, user_config={"default_high": "9G"}
    )
    assert result == "16G"


def test_resolve_high_reads_project_cgroups_toml(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(robust, "_git_root", lambda start: None)
    (tmp_path / "cgroups.toml").write_text('high = "10G"\n', encoding="utf-8")
    result = robust.resolve_high("30G", env={}, cwd=tmp_path, user_config={})
    assert result == "10G"


def test_resolve_high_config_beats_default(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(robust, "_git_root", lambda start: None)
    result = robust.resolve_high("30G", env={}, cwd=tmp_path, user_config={"default_high": "9G"})
    assert result == "9G"


def test_resolve_high_falls_back_to_computed_fraction(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(robust, "_git_root", lambda start: None)
    result = robust.resolve_high("20G", env={}, cwd=tmp_path, user_config={})
    assert result == robust.default_high_for("20G")
    # The computed default is 85% of mem, strictly below MemoryMax.
    assert robust.parse_size(result) == robust.parse_size("20G") * 85 // 100
    assert robust.parse_size(result) < robust.parse_size("20G")


# ---------------------------------------------------------------------------
# resolve_slice_max
# ---------------------------------------------------------------------------
def test_resolve_slice_max_explicit_config() -> None:
    result = robust.resolve_slice_max(user_config={"slice_max": "50G"})
    assert result == "50G"


def test_resolve_slice_max_computes_ram_minus_reserve(tmp_path) -> None:
    meminfo = tmp_path / "meminfo"
    # 64 GiB total in kB.
    meminfo.write_text(f"MemTotal:       {64 * 1024 * 1024} kB\n", encoding="utf-8")
    result = robust.resolve_slice_max(
        user_config={"reserve": "8G"}, meminfo_path=str(meminfo)
    )
    # 64G - 8G = 56G
    assert robust.parse_size(result) == 56 * 1024**3


def test_resolve_slice_max_default_reserve(tmp_path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(f"MemTotal:       {32 * 1024 * 1024} kB\n", encoding="utf-8")
    result = robust.resolve_slice_max(user_config={}, meminfo_path=str(meminfo))
    # 32G - 8G default reserve = 24G
    assert robust.parse_size(result) == 24 * 1024**3


def test_resolve_slice_max_tiny_box_floor(tmp_path) -> None:
    meminfo = tmp_path / "meminfo"
    # 4 GiB total, reserve 8G -> negative -> floor to DEFAULT_MEM.
    meminfo.write_text(f"MemTotal:       {4 * 1024 * 1024} kB\n", encoding="utf-8")
    result = robust.resolve_slice_max(user_config={}, meminfo_path=str(meminfo))
    assert robust.parse_size(result) == robust.parse_size(robust.DEFAULT_MEM)


def test_resolve_slice_swap_max_explicit_config() -> None:
    result = robust.resolve_slice_swap_max(user_config={"slice_swap_max": "12G"})
    assert result == "12G"


def test_resolve_slice_swap_max_default() -> None:
    result = robust.resolve_slice_swap_max(user_config={})
    assert result == robust.DEFAULT_SWAP


# ---------------------------------------------------------------------------
# total_ram_bytes
# ---------------------------------------------------------------------------
def test_total_ram_bytes(tmp_path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:       16314084 kB\nMemFree:  100 kB\n", encoding="utf-8"
    )
    assert robust.total_ram_bytes(str(meminfo)) == 16314084 * 1024


def test_total_ram_bytes_missing_file() -> None:
    assert robust.total_ram_bytes("/nonexistent/meminfo") == 0


# ---------------------------------------------------------------------------
# read_user_config
# ---------------------------------------------------------------------------
def test_read_user_config(tmp_path) -> None:
    cfg = tmp_path / "cgroups.toml"
    cfg.write_text(
        (
            'default_mem = "10G"\n'
            'default_swap = "4G"\n'
            'slice_max = "48G"\n'
            'slice_swap_max = "12G"\n'
            'reserve = "6G"\n'
        ),
        encoding="utf-8",
    )
    result = robust.read_user_config(cfg)
    assert result == {
        "default_mem": "10G",
        "default_swap": "4G",
        "slice_max": "48G",
        "slice_swap_max": "12G",
        "reserve": "6G",
    }


def test_read_user_config_missing(tmp_path) -> None:
    assert robust.read_user_config(tmp_path / "nope.toml") == {}


# ---------------------------------------------------------------------------
# ensure_slice
# ---------------------------------------------------------------------------
def test_ensure_slice_writes_unit_and_reloads(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(robust, "systemd_available", lambda: True)
    reloads: list[list[str]] = []

    def fake_run(args, **kwargs):
        reloads.append(args)
        import subprocess

        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(robust.subprocess, "run", fake_run)

    unit_path = tmp_path / "robust.slice"
    changed = robust.ensure_slice("40G", swap_max="12G", unit_path=unit_path)

    assert changed is True
    content = unit_path.read_text(encoding="utf-8")
    assert "[Slice]" in content
    assert "MemoryMax=40G" in content
    assert "MemorySwapMax=12G" in content
    assert ["systemctl", "--user", "daemon-reload"] in reloads


def test_ensure_slice_noop_when_unchanged(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(robust, "systemd_available", lambda: True)
    runs: list[list[str]] = []
    monkeypatch.setattr(
        robust.subprocess, "run",
        lambda args, **k: runs.append(args),
    )
    unit_path = tmp_path / "robust.slice"
    unit_path.write_text(
        "[Slice]\nMemoryMax=40G\nMemorySwapMax=12G\n", encoding="utf-8"
    )
    changed = robust.ensure_slice("40G", swap_max="12G", unit_path=unit_path)
    assert changed is False
    assert runs == []  # no daemon-reload on no-op


def test_ensure_slice_noop_without_systemd(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(robust, "systemd_available", lambda: False)
    unit_path = tmp_path / "robust.slice"
    assert robust.ensure_slice("40G", unit_path=unit_path) is False
    assert not unit_path.exists()


# ---------------------------------------------------------------------------
# stop_scope / scope_property (mock systemd)
# ---------------------------------------------------------------------------
def test_stop_scope_invokes_systemctl(monkeypatch) -> None:
    monkeypatch.setattr(robust, "systemd_available", lambda: True)
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        import subprocess

        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(robust.subprocess, "run", fake_run)
    robust.stop_scope("tmuxctl-proj")
    assert calls == [["systemctl", "--user", "stop", "tmuxctl-proj.scope"]]


def test_stop_scope_noop_without_systemd(monkeypatch) -> None:
    monkeypatch.setattr(robust, "systemd_available", lambda: False)
    called = []
    monkeypatch.setattr(robust.subprocess, "run", lambda *a, **k: called.append(a))
    robust.stop_scope("tmuxctl-proj")
    assert called == []


def test_reset_scope_stops_and_clears_failed_state(monkeypatch) -> None:
    monkeypatch.setattr(robust, "systemd_available", lambda: True)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        robust.subprocess, "run",
        lambda args, **k: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )
    robust.reset_scope("tmuxctl-proj")
    # Stop first, then reset-failed, so the transient unit name frees up.
    assert calls == [
        ["systemctl", "--user", "stop", "tmuxctl-proj.scope"],
        ["systemctl", "--user", "reset-failed", "tmuxctl-proj.scope"],
    ]


def test_reset_server_unit_targets_the_service(monkeypatch) -> None:
    monkeypatch.setattr(robust, "systemd_available", lambda: True)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        robust.subprocess, "run",
        lambda args, **k: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )
    robust.reset_server_unit()
    assert calls == [
        ["systemctl", "--user", "stop", "tmuxctl-server.service"],
        ["systemctl", "--user", "reset-failed", "tmuxctl-server.service"],
    ]


def test_reset_scope_noop_without_systemd(monkeypatch) -> None:
    monkeypatch.setattr(robust, "systemd_available", lambda: False)
    called = []
    monkeypatch.setattr(robust.subprocess, "run", lambda *a, **k: called.append(a))
    robust.reset_scope("tmuxctl-proj")
    robust.reset_server_unit()
    assert called == []


def test_scope_property_reads_value(monkeypatch) -> None:
    monkeypatch.setattr(robust, "systemd_available", lambda: True)

    def fake_run(args, **kwargs):
        import subprocess

        assert args == [
            "systemctl", "--user", "show", "tmuxctl-proj.scope",
            "-p", "MemoryMax", "--value",
        ]
        return subprocess.CompletedProcess(args, 0, "5368709120\n", "")

    monkeypatch.setattr(robust.subprocess, "run", fake_run)
    assert robust.scope_property("tmuxctl-proj", "MemoryMax") == "5368709120"


# ---------------------------------------------------------------------------
# §0: per-session tmux servers
# ---------------------------------------------------------------------------
def test_socket_for_is_deterministic_and_uid_scoped(monkeypatch) -> None:
    monkeypatch.delenv("TMUX_TMPDIR", raising=False)
    assert robust.socket_for("git-foo", uid=1000) == "/tmp/tmux-1000/tmuxctl-git-foo"
    # Pure function: same input, same output, no side effects.
    assert robust.socket_for("git-foo", uid=1000) == robust.socket_for("git-foo", uid=1000)


def test_socket_for_honors_tmux_tmpdir(monkeypatch) -> None:
    monkeypatch.setenv("TMUX_TMPDIR", "/custom")
    assert robust.socket_for("proj", uid=1000) == "/custom/tmux-1000/tmuxctl-proj"


def test_socket_for_rejects_paths_too_long_for_af_unix(monkeypatch) -> None:
    monkeypatch.delenv("TMUX_TMPDIR", raising=False)
    with pytest.raises(ValueError, match="too long"):
        robust.socket_for("x" * 100, uid=1000)


def test_session_server_unit_name() -> None:
    assert robust.session_server_unit("proj") == "tmuxctl-server-proj"


def test_session_server_bootstrap_argv_shape(monkeypatch) -> None:
    monkeypatch.delenv("TMUX_TMPDIR", raising=False)
    tmux_argv = ["new-session", "-d", "-s", "proj"]
    argv = robust.session_server_bootstrap_argv("proj", tmux_argv)

    assert argv[:3] == ["systemd-run", "--user", "--unit=tmuxctl-server-proj"]
    assert "Type=forking" in argv
    assert f"Slice={robust.server_slice_name()}" in argv
    assert "OOMScoreAdjust=-900" in argv
    # Ends with: tmux -S <this session's socket> <the tmux_argv verbatim>.
    assert argv[-len(tmux_argv) - 3:] == ["tmux", "-S", robust.socket_for("proj"), *tmux_argv]


def test_reset_session_server_unit_targets_the_service(monkeypatch) -> None:
    monkeypatch.setattr(robust, "systemd_available", lambda: True)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        robust.subprocess, "run",
        lambda args, **k: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )
    robust.reset_session_server_unit("proj")
    assert calls == [
        ["systemctl", "--user", "stop", "tmuxctl-server-proj.service"],
        ["systemctl", "--user", "reset-failed", "tmuxctl-server-proj.service"],
    ]


def test_stop_session_server_invokes_systemctl(monkeypatch) -> None:
    monkeypatch.setattr(robust, "systemd_available", lambda: True)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        robust.subprocess, "run",
        lambda args, **k: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )
    robust.stop_session_server("proj")
    assert calls == [["systemctl", "--user", "stop", "tmuxctl-server-proj.service"]]


def test_stop_session_server_noop_without_systemd(monkeypatch) -> None:
    monkeypatch.setattr(robust, "systemd_available", lambda: False)
    called = []
    monkeypatch.setattr(robust.subprocess, "run", lambda *a, **k: called.append(a))
    robust.stop_session_server("proj")
    assert called == []


# ---------------------------------------------------------------------------
# §3: oversubscription guard
# ---------------------------------------------------------------------------
def test_total_swap_bytes(tmp_path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:  16314084 kB\nSwapTotal:  8388604 kB\n", encoding="utf-8")
    assert robust.total_swap_bytes(str(meminfo)) == 8388604 * 1024


def test_system_capacity_sums_ram_and_swap(tmp_path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:  1000 kB\nSwapTotal:  500 kB\n", encoding="utf-8")
    assert robust.system_capacity(str(meminfo)) == 1500 * 1024


def test_resolve_oversubscription_max_pct_env_wins() -> None:
    assert robust.resolve_oversubscription_max_pct(env={"ROBUST_TMUX_OVERSUBSCRIPTION_MAX_PCT": "50"}) == 50


def test_resolve_oversubscription_max_pct_config_beats_default() -> None:
    assert robust.resolve_oversubscription_max_pct(
        env={}, user_config={"oversubscription_max_pct": "0"}
    ) == 0


def test_resolve_oversubscription_max_pct_default() -> None:
    assert robust.resolve_oversubscription_max_pct(env={}, user_config={}) == 120


def test_total_reserved_mem_sums_scope_units(monkeypatch) -> None:
    monkeypatch.setattr(robust, "systemd_available", lambda: True)

    def fake_run(args, **kwargs):
        if args[:3] == ["systemctl", "--user", "list-units"]:
            return subprocess.CompletedProcess(
                args, 0,
                "tmuxctl-a.scope loaded active running Session a\n"
                "tmuxctl-b.scope loaded active running Session b\n",
                "",
            )
        if "MemoryMax" in args:
            scope = args[3]
            value = "5368709120" if scope == "tmuxctl-a.scope" else "infinity"
            return subprocess.CompletedProcess(args, 0, value, "")
        return subprocess.CompletedProcess(args, 1, "", "")

    monkeypatch.setattr(robust.subprocess, "run", fake_run)
    # Only the numeric scope counts; "infinity" (uncapped) contributes 0.
    assert robust.total_reserved_mem() == 5368709120


def test_total_reserved_mem_zero_without_systemd(monkeypatch) -> None:
    monkeypatch.setattr(robust, "systemd_available", lambda: False)
    assert robust.total_reserved_mem() == 0


# ---------------------------------------------------------------------------
# §1: pty-durable pane wrapping (dtach)
# ---------------------------------------------------------------------------
def test_resolve_dtach_wrap_env_wins() -> None:
    assert robust.resolve_dtach_wrap(env={"ROBUST_TMUX_DTACH_WRAP": "true"}) is True
    assert robust.resolve_dtach_wrap(env={"ROBUST_TMUX_DTACH_WRAP": "0"}) is False


def test_resolve_dtach_wrap_config_beats_default() -> None:
    assert robust.resolve_dtach_wrap(env={}, user_config={"dtach_wrap": "yes"}) is True


def test_resolve_dtach_wrap_defaults_off() -> None:
    assert robust.resolve_dtach_wrap(env={}, user_config={}) is False


def test_dtach_available_checks_path(monkeypatch) -> None:
    monkeypatch.setattr(robust.shutil, "which", lambda name: "/usr/bin/dtach" if name == "dtach" else None)
    assert robust.dtach_available() is True
    monkeypatch.setattr(robust.shutil, "which", lambda name: None)
    assert robust.dtach_available() is False


def test_dtach_socket_for_creates_directory(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    socket = robust.dtach_socket_for("proj")
    assert socket == tmp_path / "tmuxctl" / "dtach" / "proj-0.sock"
    assert socket.parent.is_dir()


def test_dtach_wrap_builds_expected_argv(tmp_path) -> None:
    socket = tmp_path / "proj-0.sock"
    argv = robust.dtach_wrap(["/bin/bash", "-l"], socket)
    assert argv == ["dtach", "-A", str(socket), "-E", "-z", "-r", "winch", "--", "/bin/bash", "-l"]


def test_dtach_attach_argv() -> None:
    socket = Path("/tmp/x.sock")
    assert robust.dtach_attach_argv(socket) == ["dtach", "-a", "/tmp/x.sock"]


def test_scope_occupied_only_by_dtach_true_when_all_pids_are_dtach(monkeypatch) -> None:
    monkeypatch.setattr(robust, "scope_cgroup_path", lambda unit: "/robust.slice/tmuxctl-proj.scope")
    monkeypatch.setattr(robust, "cgroup_proc_pids", lambda cgroup: [111, 222])
    monkeypatch.setattr(robust, "proc_comm", lambda pid: "dtach")
    assert robust.scope_occupied_only_by_dtach("tmuxctl-proj") is True


def test_scope_occupied_only_by_dtach_false_with_foreign_process(monkeypatch) -> None:
    monkeypatch.setattr(robust, "scope_cgroup_path", lambda unit: "/robust.slice/tmuxctl-proj.scope")
    monkeypatch.setattr(robust, "cgroup_proc_pids", lambda cgroup: [111, 222])
    monkeypatch.setattr(robust, "proc_comm", lambda pid: "dtach" if pid == 111 else "python")
    assert robust.scope_occupied_only_by_dtach("tmuxctl-proj") is False


def test_scope_occupied_only_by_dtach_false_when_empty(monkeypatch) -> None:
    monkeypatch.setattr(robust, "scope_cgroup_path", lambda unit: "/robust.slice/tmuxctl-proj.scope")
    monkeypatch.setattr(robust, "cgroup_proc_pids", lambda cgroup: [])
    assert robust.scope_occupied_only_by_dtach("tmuxctl-proj") is False


def test_scope_occupied_only_by_dtach_false_without_cgroup(monkeypatch) -> None:
    monkeypatch.setattr(robust, "scope_cgroup_path", lambda unit: None)
    assert robust.scope_occupied_only_by_dtach("tmuxctl-proj") is False


def test_cgroup_proc_pids_reads_procs_file(tmp_path, monkeypatch) -> None:
    cgroup_root = tmp_path / "sys_fs_cgroup"
    (cgroup_root / "robust.slice" / "tmuxctl-proj.scope").mkdir(parents=True)
    procs = cgroup_root / "robust.slice" / "tmuxctl-proj.scope" / "cgroup.procs"
    procs.write_text("111\n222\n", encoding="utf-8")

    monkeypatch.setattr(
        robust, "Path", lambda p: cgroup_root if p == "/sys/fs/cgroup" else Path(p)
    )
    assert robust.cgroup_proc_pids("/robust.slice/tmuxctl-proj.scope") == [111, 222]


def test_proc_comm_reads_comm_file(tmp_path, monkeypatch) -> None:
    comm_file = tmp_path / "comm"
    comm_file.write_text("dtach\n", encoding="utf-8")
    monkeypatch.setattr(
        robust, "Path", lambda p: comm_file if p == "/proc/999/comm" else Path(p)
    )
    assert robust.proc_comm(999) == "dtach"


def test_proc_comm_none_when_unreadable(monkeypatch) -> None:
    monkeypatch.setattr(
        robust, "Path", lambda p: Path("/nonexistent/does/not/exist")
    )
    assert robust.proc_comm(999) is None
