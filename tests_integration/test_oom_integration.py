"""Integration test: real cgroup OOM isolation (no mocks).

This is the regression net for the crash that motivated the feature -- a
machine-wide OOM-killer taking down the shared tmux server and every session
on it. With memory-capped systemd --user scopes, a runaway is OOM-killed in
ITS OWN cgroup and nothing else is touched.

It exercises the real ``robust.scope_wrap`` argv against a real
``systemd-run --user --scope`` + cgroup v2, so it only runs where user scopes
work. Where there is no user systemd bus it is skipped (not failed) -- that is
the correct behaviour; the assertion requires a live cgroup.

This test SPAWNS A PROCESS THAT ALLOCATES MEMORY UNTIL IT IS OOM-KILLED, so it
lives in ``tests_integration/`` (outside the default ``testpaths``) and never
runs during ``make test`` / a plain ``pytest``. It runs on CI only, via
``make test-integration`` (the ``integration`` job in .github/workflows/test.yml).
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from tmuxctl import robust


def _user_scopes_work() -> bool:
    if not robust.systemd_available():
        return False
    try:
        probe = subprocess.run(
            robust.scope_wrap(["true"], f"tmuxctl-oomprobe-{os.getpid()}", "64M"),
            capture_output=True,
            timeout=15,
        )
        return probe.returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _user_scopes_work(),
    reason="systemd --user memory-capped scopes unavailable "
    "(e.g. CI without a user systemd bus / cgroup v2)",
)


def test_capped_runaway_is_oom_killed_in_isolation() -> None:
    """A process that blows past its cap is OOM-killed in its own scope.

    The test runner itself surviving to make this assertion IS the isolation
    guarantee: a machine-wide OOM would have taken pytest (and the tmux
    server) down too.
    """
    hog = "b = bytearray()\nwhile True:\n    b += bytearray(20_000_000)\n"
    victim = subprocess.run(
        robust.scope_wrap(
            [sys.executable, "-c", hog],
            f"tmuxctl-oomvictim-{os.getpid()}",
            "128M",
        ),
        capture_output=True,
        timeout=60,
    )
    assert victim.returncode != 0, "a capped runaway must be killed, not complete"
    # 137 = 128 + SIGKILL(9) from systemd-run; -9 if surfaced as a raw signal.
    assert victim.returncode in (137, -9, 9), (
        f"expected an OOM/SIGKILL exit, got {victim.returncode}"
    )


def test_sibling_scope_completes_normally() -> None:
    """A well-behaved capped command in a separate scope is unaffected."""
    survivor = subprocess.run(
        robust.scope_wrap(
            [sys.executable, "-c", "print('survivor-ok')"],
            f"tmuxctl-oomsurvivor-{os.getpid()}",
            "128M",
        ),
        capture_output=True,
        timeout=30,
        text=True,
    )
    assert survivor.returncode == 0
    assert "survivor-ok" in survivor.stdout
