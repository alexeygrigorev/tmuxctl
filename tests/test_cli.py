from __future__ import annotations

import sys
from pathlib import Path

from typer.testing import CliRunner

from tmuxctl import cli
from tmuxctl.cli import app
from tmuxctl.models import Job


runner = CliRunner()


def test_send_reads_message_from_file(monkeypatch, tmp_path: Path) -> None:
    message_file = tmp_path / "message.txt"
    message_file.write_text("line 1\nline 2\n", encoding="utf-8")
    captured: dict[str, object] = {}

    monkeypatch.setattr("tmuxctl.cli._conn", lambda: object())
    monkeypatch.setattr("tmuxctl.cli.tmux_api.session_exists", lambda session_name: True)

    def fake_send_keys(session_name: str, message: str, press_enter: bool, enter_delay_ms: int) -> None:
        captured["session_name"] = session_name
        captured["message"] = message
        captured["press_enter"] = press_enter
        captured["enter_delay_ms"] = enter_delay_ms

    monkeypatch.setattr("tmuxctl.cli.tmux_api.send_keys", fake_send_keys)
    monkeypatch.setattr("tmuxctl.cli.storage.insert_log", lambda *args, **kwargs: None)

    result = runner.invoke(app, ["send", "rk-codex", "--message-file", str(message_file)])

    assert result.exit_code == 0
    assert captured["session_name"] == "rk-codex"
    assert captured["message"] == "line 1\nline 2"
    assert captured["press_enter"] is True
    assert captured["enter_delay_ms"] == 200


def test_add_rejects_both_message_and_message_file(monkeypatch, tmp_path: Path) -> None:
    message_file = tmp_path / "message.txt"
    message_file.write_text("hello", encoding="utf-8")

    monkeypatch.setattr("tmuxctl.cli.tmux_api.session_exists", lambda session_name: True)

    result = runner.invoke(
        app,
        [
            "add",
            "rk-codex",
            "--every",
            "30m",
            "--message",
            "hello",
            "--message-file",
            str(message_file),
        ],
    )

    assert result.exit_code == 1
    assert "choose either --message or --message-file, not both" in result.output


def test_add_stores_message_file_path(monkeypatch, tmp_path: Path) -> None:
    message_file = tmp_path / "message.txt"
    message_file.write_text("hello from file\n", encoding="utf-8")
    captured: dict[str, object] = {}

    monkeypatch.setattr("tmuxctl.cli.tmux_api.session_exists", lambda session_name: True)
    monkeypatch.setattr("tmuxctl.cli._conn", lambda: object())
    monkeypatch.setattr("tmuxctl.cli.parse_interval", lambda value: 1800)

    class DummyJob:
        id = 7
        session_name = "rk-codex"
        interval_seconds = 1800

    def fake_create_job(conn, **kwargs):
        captured.update(kwargs)
        return DummyJob()

    monkeypatch.setattr("tmuxctl.cli.storage.create_job", fake_create_job)

    result = runner.invoke(
        app,
        ["add", "rk-codex", "--every", "30m", "--message-file", str(message_file)],
    )

    assert result.exit_code == 0
    assert captured["message"] == "hello from file"
    assert captured["message_file_path"] == str(message_file)


def test_jobs_shows_inline_and_file_sources(monkeypatch) -> None:
    monkeypatch.setattr("tmuxctl.cli._conn", lambda: object())
    monkeypatch.setattr(
        "tmuxctl.cli.storage.list_jobs",
        lambda conn: [
            Job(
                id=1,
                session_name="inline",
                message="short inline prompt",
                message_file_path=None,
                interval_seconds=900,
                enabled=True,
                send_enter=True,
                enter_delay_ms=200,
                created_at="2026-04-03T00:00:00+00:00",
                updated_at="2026-04-03T00:00:00+00:00",
                last_run_at=None,
                next_run_at="2026-04-03T00:15:00+00:00",
            ),
            Job(
                id=2,
                session_name="linked",
                message="stored snapshot",
                message_file_path="prompts/rk-codex-progress.txt",
                interval_seconds=1800,
                enabled=True,
                send_enter=True,
                enter_delay_ms=200,
                created_at="2026-04-03T00:00:00+00:00",
                updated_at="2026-04-03T00:00:00+00:00",
                last_run_at=None,
                next_run_at="2026-04-03T00:30:00+00:00",
            ),
        ],
    )

    result = runner.invoke(app, ["jobs"])

    assert result.exit_code == 0
    assert "SOURCE" in result.output
    assert "inline" in result.output
    assert "file" in result.output
    assert "short inline prompt" in result.output
    assert "prompts/rk-codex-progress.txt" in result.output


def test_complete_session_names_filters_matches(monkeypatch) -> None:
    monkeypatch.setattr("tmuxctl.cli.tmux_api.list_sessions", lambda: ["rk-codex", "rk-worker", "other"])

    assert cli._complete_session_names("rk-") == ["rk-codex", "rk-worker"]


def test_main_rewrites_colon_shortcut(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_app(*, args):
        captured["args"] = args

    monkeypatch.setattr(cli, "app", fake_app)
    monkeypatch.setattr(sys, "argv", ["tmuxctl", ":rk-codex"])

    cli.main()

    assert captured["args"] == ["create-or-attach", "rk-codex"]
