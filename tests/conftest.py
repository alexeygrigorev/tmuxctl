from __future__ import annotations

import pytest

from tmuxctl import storage


@pytest.fixture(autouse=True)
def _isolated_session_event_db(monkeypatch, tmp_path):
    """tmux_api._record_event() opens tmuxctl's default sqlite db as a
    best-effort side channel on every session create/kill. Point it at a
    throwaway path for every test so a test run never writes into the
    user's real ~/.config/tmuxctl/tmuxctl.db."""
    monkeypatch.setattr(storage, "DEFAULT_DB_PATH", tmp_path / "tmuxctl-test.db")
