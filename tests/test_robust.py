from __future__ import annotations

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
    assert argv == [
        "systemd-run",
        "--user",
        "--scope",
        "--unit=tmuxctl-proj",
        "-p",
        "MemoryMax=7G",
        "-p",
        "MemorySwapMax=0",
        "-p",
        "Slice=robust.slice",
        "--quiet",
        "--",
        "/bin/bash",
        "-l",
    ]


def test_scope_unit_name() -> None:
    assert robust.scope_unit_name("git-proj") == "tmuxctl-git-proj"


# ---------------------------------------------------------------------------
# resolve_mem precedence
# ---------------------------------------------------------------------------
def test_resolve_mem_flag_wins(monkeypatch, tmp_path) -> None:
    # Even with env + project + config set, the flag wins.
    (tmp_path / ".robust-tmux").write_text("mem = 30G\n", encoding="utf-8")
    monkeypatch.setattr(robust, "read_user_config", lambda *a, **k: {"default_mem": "20G"})
    result = robust.resolve_mem(
        flag="48G",
        env={"ROBUST_TMUX_MEM": "16G"},
        cwd=tmp_path,
    )
    assert result == "48G"


def test_resolve_mem_env_beats_project_and_config(monkeypatch, tmp_path) -> None:
    (tmp_path / ".robust-tmux").write_text("mem = 30G\n", encoding="utf-8")
    monkeypatch.setattr(robust, "read_user_config", lambda *a, **k: {"default_mem": "20G"})
    result = robust.resolve_mem(
        flag=None,
        env={"ROBUST_TMUX_MEM": "16G"},
        cwd=tmp_path,
    )
    assert result == "16G"


def test_resolve_mem_project_beats_config(monkeypatch, tmp_path) -> None:
    (tmp_path / ".robust-tmux").write_text('mem = "30G"\n', encoding="utf-8")
    monkeypatch.setattr(robust, "_git_root", lambda start: None)
    result = robust.resolve_mem(
        flag=None,
        env={},
        cwd=tmp_path,
        user_config={"default_mem": "20G"},
    )
    assert result == "30G"


def test_resolve_mem_project_bare_size_line(monkeypatch, tmp_path) -> None:
    (tmp_path / ".robust-tmux").write_text("24G\n", encoding="utf-8")
    monkeypatch.setattr(robust, "_git_root", lambda start: None)
    result = robust.resolve_mem(flag=None, env={}, cwd=tmp_path, user_config={})
    assert result == "24G"


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


def test_resolve_mem_reads_git_root_project_file(monkeypatch, tmp_path) -> None:
    root = tmp_path / "repo"
    sub = root / "a" / "b"
    sub.mkdir(parents=True)
    (root / ".robust-tmux").write_text("mem = 42G\n", encoding="utf-8")
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


def test_resolve_mem_dedicated_file_beats_pyproject(monkeypatch, tmp_path) -> None:
    (tmp_path / ".robust-tmux").write_text("mem = 30G\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[tool.tmuxctl]\nmem = \"36G\"\n", encoding="utf-8"
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
        'default_mem = "10G"\nslice_max = "48G"\nreserve = "6G"\n',
        encoding="utf-8",
    )
    result = robust.read_user_config(cfg)
    assert result == {"default_mem": "10G", "slice_max": "48G", "reserve": "6G"}


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
    changed = robust.ensure_slice("40G", unit_path=unit_path)

    assert changed is True
    content = unit_path.read_text(encoding="utf-8")
    assert "[Slice]" in content
    assert "MemoryMax=40G" in content
    assert "MemorySwapMax=0" in content
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
        "[Slice]\nMemoryMax=40G\nMemorySwapMax=0\n", encoding="utf-8"
    )
    changed = robust.ensure_slice("40G", unit_path=unit_path)
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
