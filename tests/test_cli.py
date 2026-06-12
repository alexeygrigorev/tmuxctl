from __future__ import annotations

import sys
from pathlib import Path

from typer.testing import CliRunner

from tmuxctl import cli
from tmuxctl.cli import app
from tmuxctl.models import Job, SessionInfo


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
            "jobs",
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
        ["jobs", "add", "rk-codex", "--every", "30m", "--message-file", str(message_file)],
    )

    assert result.exit_code == 0
    assert captured["message"] == "hello from file"
    assert captured["message_file_path"] == str(message_file)


def test_add_accepts_current_session_alias(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr("tmuxctl.cli.tmux_api.current_session_name", lambda: "rk-codex")
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
        ["jobs", "add", ":current", "--every", "30m", "--message", "hello"],
    )

    assert result.exit_code == 0
    assert captured["session_name"] == "rk-codex"


def test_add_rejects_current_session_alias_outside_tmux(monkeypatch) -> None:
    monkeypatch.setattr(
        "tmuxctl.cli.tmux_api.current_session_name",
        lambda: (_ for _ in ()).throw(RuntimeError("session alias ':current' requires running inside tmux")),
    )

    result = runner.invoke(
        app,
        ["jobs", "add", ":current", "--every", "30m", "--message", "hello"],
    )

    assert result.exit_code == 1
    assert "session alias ':current' requires running inside tmux" in result.output


def test_edit_accepts_current_session_alias(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr("tmuxctl.cli.tmux_api.current_session_name", lambda: "rk-codex")
    monkeypatch.setattr("tmuxctl.cli.tmux_api.session_exists", lambda session_name: True)
    monkeypatch.setattr("tmuxctl.cli._conn", lambda: object())
    monkeypatch.setattr(
        "tmuxctl.cli.storage.get_job",
        lambda conn, job_id: Job(
            id=job_id,
            session_name="old-session",
            message="hello",
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
    )

    def fake_update_job(conn, job_id, **kwargs):
        captured["job_id"] = job_id
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("tmuxctl.cli.storage.update_job", fake_update_job)

    result = runner.invoke(app, ["jobs", "edit", "7", "--session", ":current"])

    assert result.exit_code == 0
    assert captured["job_id"] == 7
    assert captured["session_name"] == "rk-codex"


def test_edit_rejects_current_session_alias_outside_tmux(monkeypatch) -> None:
    monkeypatch.setattr("tmuxctl.cli._conn", lambda: object())
    monkeypatch.setattr(
        "tmuxctl.cli.storage.get_job",
        lambda conn, job_id: Job(
            id=job_id,
            session_name="old-session",
            message="hello",
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
    )
    monkeypatch.setattr(
        "tmuxctl.cli.tmux_api.current_session_name",
        lambda: (_ for _ in ()).throw(RuntimeError("session alias ':current' requires running inside tmux")),
    )

    result = runner.invoke(app, ["jobs", "edit", "7", "--session", ":current"])

    assert result.exit_code == 1
    assert "session alias ':current' requires running inside tmux" in result.output


def test_jobs_shows_inline_and_file_sources(monkeypatch) -> None:
    monkeypatch.setattr("tmuxctl.cli._conn", lambda: object())
    monkeypatch.setattr(
        "tmuxctl.cli.storage.list_jobs",
        lambda conn, session_name=None: [
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


def test_jobs_list_subcommand_shows_inline_and_file_sources(monkeypatch) -> None:
    monkeypatch.setattr("tmuxctl.cli._conn", lambda: object())
    monkeypatch.setattr(
        "tmuxctl.cli.storage.list_jobs",
        lambda conn, session_name=None: [
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
        ],
    )

    result = runner.invoke(app, ["jobs", "list"])

    assert result.exit_code == 0
    assert "SOURCE" in result.output
    assert "short inline prompt" in result.output


def test_j_alias_lists_jobs(monkeypatch) -> None:
    monkeypatch.setattr("tmuxctl.cli._conn", lambda: object())
    monkeypatch.setattr(
        "tmuxctl.cli.storage.list_jobs",
        lambda conn, session_name=None: [
            Job(
                id=7,
                session_name="rk-codex",
                message="check status",
                message_file_path=None,
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

    result = runner.invoke(app, ["j"])

    assert result.exit_code == 0
    assert "ID  ENABLED  SESSION" in result.output
    assert "7   yes      rk-codex" in result.output


def test_list_shows_sorted_session_table(monkeypatch) -> None:
    monkeypatch.setattr(
        "tmuxctl.cli.tmux_api.list_session_info",
        lambda: [
            SessionInfo(name="older", created_at=100, activity_at=300),
            SessionInfo(name="newer", created_at=200, activity_at=200),
        ],
    )

    result = runner.invoke(app, ["list"])

    assert result.exit_code == 0
    assert "IDX  SESSION               CREATED" in result.output
    assert "1    newer" in result.output
    assert "2    older" in result.output


def test_ls_alias_shows_sorted_session_table(monkeypatch) -> None:
    monkeypatch.setattr(
        "tmuxctl.cli.tmux_api.list_session_info",
        lambda: [
            SessionInfo(name="older", created_at=100, activity_at=300),
            SessionInfo(name="newer", created_at=200, activity_at=200),
        ],
    )

    result = runner.invoke(app, ["ls"])

    assert result.exit_code == 0
    assert "1    newer" in result.output
    assert "2    older" in result.output


def test_kill_by_session_name(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_kill_session(session_name: str) -> None:
        captured["session_name"] = session_name

    monkeypatch.setattr("tmuxctl.cli.tmux_api.kill_session", fake_kill_session)

    result = runner.invoke(app, ["kill", "rk-codex", "--yes"])

    assert result.exit_code == 0
    assert captured["session_name"] == "rk-codex"
    assert "Killed session rk-codex" in result.output


def test_kill_by_numeric_id(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "tmuxctl.cli.tmux_api.list_session_info",
        lambda: [
            SessionInfo(name="older", created_at=100, activity_at=300),
            SessionInfo(name="newer", created_at=200, activity_at=200),
        ],
    )

    def fake_kill_session(session_name: str) -> None:
        captured["session_name"] = session_name

    monkeypatch.setattr("tmuxctl.cli.tmux_api.kill_session", fake_kill_session)

    result = runner.invoke(app, ["kill", "2", "--yes"])

    assert result.exit_code == 0
    assert captured["session_name"] == "older"


def test_kill_prompts_for_confirmation(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_kill_session(session_name: str) -> None:
        captured["session_name"] = session_name

    monkeypatch.setattr("tmuxctl.cli.tmux_api.kill_session", fake_kill_session)

    result = runner.invoke(app, ["kill", "rk-codex"], input="y\n")

    assert result.exit_code == 0
    assert captured["session_name"] == "rk-codex"


def test_kill_aborts_without_confirmation(monkeypatch) -> None:
    called = {"kill": False}

    def fake_kill_session(session_name: str) -> None:
        called["kill"] = True

    monkeypatch.setattr("tmuxctl.cli.tmux_api.kill_session", fake_kill_session)

    result = runner.invoke(app, ["kill", "rk-codex"], input="n\n")

    assert result.exit_code == 1
    assert called["kill"] is False
    assert "Aborted." in result.output


def test_pause_current_pauses_jobs_for_current_session(monkeypatch) -> None:
    monkeypatch.setattr("tmuxctl.cli._conn", lambda: object())
    monkeypatch.setattr("tmuxctl.cli.tmux_api.current_session_name", lambda: "rk-codex")
    monkeypatch.setattr(
        "tmuxctl.cli.storage.list_jobs",
        lambda conn, session_name=None: [
            Job(
                id=1,
                session_name="rk-codex",
                message="hello",
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
        ],
    )
    monkeypatch.setattr("tmuxctl.cli.storage.set_session_jobs_enabled", lambda conn, *, session_name, enabled: 1)

    result = runner.invoke(app, ["jobs", "pause-current"])

    assert result.exit_code == 0
    assert "Paused 1 job(s) for session rk-codex" in result.output


def test_pause_current_rejects_when_session_has_no_jobs(monkeypatch) -> None:
    monkeypatch.setattr("tmuxctl.cli._conn", lambda: object())
    monkeypatch.setattr("tmuxctl.cli.tmux_api.current_session_name", lambda: "rk-codex")
    monkeypatch.setattr("tmuxctl.cli.storage.list_jobs", lambda conn, session_name=None: [])

    result = runner.invoke(app, ["jobs", "pause-current"])

    assert result.exit_code == 1
    assert "no jobs found for tmux session 'rk-codex'" in result.output


def test_resume_current_resumes_jobs_for_current_session(monkeypatch) -> None:
    monkeypatch.setattr("tmuxctl.cli._conn", lambda: object())
    monkeypatch.setattr("tmuxctl.cli.tmux_api.current_session_name", lambda: "rk-codex")
    monkeypatch.setattr(
        "tmuxctl.cli.storage.list_jobs",
        lambda conn, session_name=None: [
            Job(
                id=1,
                session_name="rk-codex",
                message="hello",
                message_file_path=None,
                interval_seconds=900,
                enabled=False,
                send_enter=True,
                enter_delay_ms=200,
                created_at="2026-04-03T00:00:00+00:00",
                updated_at="2026-04-03T00:00:00+00:00",
                last_run_at=None,
                next_run_at="2026-04-03T00:15:00+00:00",
            ),
        ],
    )
    monkeypatch.setattr("tmuxctl.cli.storage.set_session_jobs_enabled", lambda conn, *, session_name, enabled: 1)

    result = runner.invoke(app, ["jobs", "resume-current"])

    assert result.exit_code == 0
    assert "Resumed 1 job(s) for session rk-codex" in result.output


def test_pause_current_requires_running_inside_tmux(monkeypatch) -> None:
    monkeypatch.setattr("tmuxctl.cli._conn", lambda: object())
    monkeypatch.setattr(
        "tmuxctl.cli.tmux_api.current_session_name",
        lambda: (_ for _ in ()).throw(RuntimeError("session alias ':current' requires running inside tmux")),
    )

    result = runner.invoke(app, ["jobs", "pause-current"])

    assert result.exit_code == 1
    assert "session alias ':current' requires running inside tmux" in result.output


def test_rename_by_session_name_updates_jobs(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr("tmuxctl.cli._conn", lambda: object())

    def fake_rename_session(session_name: str, new_name: str) -> None:
        captured["session_name"] = session_name
        captured["new_name"] = new_name

    def fake_rename_session_jobs(conn, *, session_name: str, new_session_name: str) -> int:
        captured["job_session_name"] = session_name
        captured["job_new_session_name"] = new_session_name
        return 2

    monkeypatch.setattr("tmuxctl.cli.tmux_api.rename_session", fake_rename_session)
    monkeypatch.setattr("tmuxctl.cli.storage.rename_session_jobs", fake_rename_session_jobs)

    result = runner.invoke(app, ["rename", "rk-codex", "rk-main"])

    assert result.exit_code == 0
    assert captured["session_name"] == "rk-codex"
    assert captured["new_name"] == "rk-main"
    assert captured["job_session_name"] == "rk-codex"
    assert captured["job_new_session_name"] == "rk-main"
    assert "Renamed session rk-codex to rk-main (2 job(s) updated)" in result.output


def test_rename_by_numeric_id(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr("tmuxctl.cli._conn", lambda: object())
    monkeypatch.setattr(
        "tmuxctl.cli.tmux_api.list_session_info",
        lambda: [
            SessionInfo(name="older", created_at=100, activity_at=300),
            SessionInfo(name="newer", created_at=200, activity_at=200),
        ],
    )

    def fake_rename_session(session_name: str, new_name: str) -> None:
        captured["session_name"] = session_name
        captured["new_name"] = new_name

    monkeypatch.setattr("tmuxctl.cli.tmux_api.rename_session", fake_rename_session)
    monkeypatch.setattr("tmuxctl.cli.storage.rename_session_jobs", lambda *args, **kwargs: 0)

    result = runner.invoke(app, ["rename", "2", "archived", "--by", "created"])

    assert result.exit_code == 0
    assert captured["session_name"] == "older"
    assert captured["new_name"] == "archived"
    assert "Renamed session older to archived (0 job(s) updated)" in result.output


def test_complete_session_names_filters_matches(monkeypatch) -> None:
    monkeypatch.setattr("tmuxctl.cli.tmux_api.list_sessions", lambda: ["rk-codex", "rk-worker", "other"])

    assert cli._complete_session_names("rk-") == ["rk-codex", "rk-worker"]


def test_current_directory_session_name_under_home() -> None:
    assert cli._current_directory_session_name(
        cwd=Path("/home/alexey/git/workshops"),
        home=Path("/home/alexey"),
    ) == "git-workshops"


def test_current_directory_session_name_normalizes_tmux_separators() -> None:
    assert cli._current_directory_session_name(
        cwd=Path("/home/alexey/git/datatalksclub.github.io"),
        home=Path("/home/alexey"),
    ) == "git-datatalksclub_github_io"


def test_current_directory_session_name_for_home_directory() -> None:
    assert cli._current_directory_session_name(
        cwd=Path("/home/alexey"),
        home=Path("/home/alexey"),
    ) == "home-alexey"


def test_current_directory_session_name_with_suffix() -> None:
    assert cli._current_directory_session_name(
        cwd=Path("/home/alexey/git/workshops"),
        home=Path("/home/alexey"),
        suffix="asd",
    ) == "git-workshops-asd"


def test_current_directory_session_name_normalizes_suffix() -> None:
    assert cli._current_directory_session_name(
        cwd=Path("/home/alexey/git/workshops"),
        home=Path("/home/alexey"),
        suffix="feature/test",
    ) == "git-workshops-feature-test"


def test_current_directory_session_name_outside_home() -> None:
    assert cli._current_directory_session_name(
        cwd=Path("/var/tmp/demo space"),
        home=Path("/home/alexey"),
    ) == "var-tmp-demo-space"


def test_root_group_shell_complete_adds_sessions(monkeypatch) -> None:
    monkeypatch.setattr("tmuxctl.cli.tmux_api.list_sessions", lambda: ["rk-codex", "rk-worker", "other"])

    values = [item.value for item in cli._extend_root_completion([], "rk-")]
    assert "rk-codex" in values
    assert "rk-worker" in values


def test_root_group_shell_complete_adds_colon_sessions(monkeypatch) -> None:
    monkeypatch.setattr("tmuxctl.cli.tmux_api.list_sessions", lambda: ["rk-codex", "rk-worker", "other"])

    values = [item.value for item in cli._extend_root_completion([], ":rk-")]
    assert ":rk-codex" in values
    assert ":rk-worker" in values


def test_main_rewrites_colon_shortcut(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_app(*, args):
        captured["args"] = args

    monkeypatch.setattr(cli, "app", fake_app)
    monkeypatch.setattr(sys, "argv", ["tmuxctl", ":rk-codex"])

    cli.main()

    assert captured["args"] == ["create-or-attach", "rk-codex"]


def test_main_keeps_current_session_alias_shortcut(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_app(*, args):
        captured["args"] = args

    monkeypatch.setattr(cli, "app", fake_app)
    monkeypatch.setattr(sys, "argv", ["tmuxctl", ":current"])

    cli.main()

    assert captured["args"] == ["create-or-attach", ":current"]


def test_main_rewrites_dash_shortcut(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_app(*, args):
        captured["args"] = args

    monkeypatch.setattr(cli, "app", fake_app)
    monkeypatch.setattr(cli, "_current_directory_session_name", lambda: "git-workshops")
    monkeypatch.setattr(sys, "argv", ["t", "-"])

    cli.main()

    assert captured["args"] == ["create-or-attach", "git-workshops"]


def test_main_rewrites_dash_shortcut_with_create_command(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_app(*, args):
        captured["args"] = args

    monkeypatch.setattr(cli, "app", fake_app)
    monkeypatch.setattr(cli, "_current_directory_session_name", lambda: "git-workshops")
    monkeypatch.setattr(sys, "argv", ["t", "-", "cy"])

    cli.main()

    assert captured["args"] == ["create-or-attach", "git-workshops", "cy"]


def test_main_rewrites_dash_suffix_shortcut(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_app(*, args):
        captured["args"] = args

    monkeypatch.setattr(cli, "app", fake_app)
    monkeypatch.setattr(cli, "_current_directory_session_name", lambda suffix=None: f"git-workshops-{suffix}")
    monkeypatch.setattr(sys, "argv", ["t", "-asd"])

    cli.main()

    assert captured["args"] == ["create-or-attach", "git-workshops-asd"]


def test_create_or_attach_passes_create_command(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_create_or_attach_session(
        session_name: str,
        *,
        resize_window: bool = False,
        shell_command: list[str] | None = None,
        mem: str | None = None,
    ) -> None:
        captured["session_name"] = session_name
        captured["resize_window"] = resize_window
        captured["shell_command"] = shell_command

    monkeypatch.setattr("tmuxctl.cli.tmux_api.create_or_attach_session", fake_create_or_attach_session)

    result = runner.invoke(app, ["create-or-attach", "git-workshops", "cy"])

    assert result.exit_code == 0
    assert captured["session_name"] == "git-workshops"
    assert captured["resize_window"] is False
    assert captured["shell_command"] == ["cy"]


def test_create_or_attach_normalizes_tmux_separators(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_create_or_attach_session(
        session_name: str,
        *,
        resize_window: bool = False,
        shell_command: list[str] | None = None,
        mem: str | None = None,
    ) -> None:
        captured["session_name"] = session_name
        captured["resize_window"] = resize_window
        captured["shell_command"] = shell_command

    monkeypatch.setattr("tmuxctl.cli.tmux_api.create_or_attach_session", fake_create_or_attach_session)

    result = runner.invoke(app, ["create-or-attach", "git-datatalksclub.github.io", "cy"])

    assert result.exit_code == 0
    assert captured["session_name"] == "git-datatalksclub_github_io"
    assert captured["resize_window"] is False
    assert captured["shell_command"] == ["cy"]


def test_main_keeps_double_dash_option(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_app(*, args):
        captured["args"] = args

    monkeypatch.setattr(cli, "app", fake_app)
    monkeypatch.setattr(sys, "argv", ["tmuxctl", "--help"])

    cli.main()

    assert captured["args"] == ["--help"]


def test_main_rewrites_plain_session_shortcut(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_app(*, args):
        captured["args"] = args

    monkeypatch.setattr(cli, "app", fake_app)
    monkeypatch.setattr(sys, "argv", ["tmuxctl", "rk-codex"])

    cli.main()

    assert captured["args"] == ["attach", "rk-codex"]


def test_main_rewrites_plain_session_shortcut_with_resize_window(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_app(*, args):
        captured["args"] = args

    monkeypatch.setattr(cli, "app", fake_app)
    monkeypatch.setattr(sys, "argv", ["tmuxctl", "rk-codex", "--resize-window"])

    cli.main()

    assert captured["args"] == ["attach", "rk-codex", "--resize-window"]


def test_attach_passes_resize_window(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_attach_session(session_name: str, *, resize_window: bool = False) -> None:
        captured["session_name"] = session_name
        captured["resize_window"] = resize_window

    monkeypatch.setattr("tmuxctl.cli.tmux_api.attach_session", fake_attach_session)

    result = runner.invoke(app, ["attach", "git-llm-zoomcamp", "--resize-window"])

    assert result.exit_code == 0
    assert captured["session_name"] == "git-llm-zoomcamp"
    assert captured["resize_window"] is True


def test_attach_normalizes_tmux_separators(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_attach_session(session_name: str, *, resize_window: bool = False) -> None:
        captured["session_name"] = session_name
        captured["resize_window"] = resize_window

    monkeypatch.setattr("tmuxctl.cli.tmux_api.attach_session", fake_attach_session)

    result = runner.invoke(app, ["attach", "git-datatalksclub.github.io"])

    assert result.exit_code == 0
    assert captured["session_name"] == "git-datatalksclub_github_io"
    assert captured["resize_window"] is False


def test_main_does_not_rewrite_removed_job_root_command(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_app(*, args):
        captured["args"] = args

    monkeypatch.setattr(cli, "app", fake_app)
    monkeypatch.setattr(sys, "argv", ["tmuxctl", "add", "rk-codex"])

    cli.main()

    assert captured["args"] == ["add", "rk-codex"]


def test_main_does_not_rewrite_jobs_alias(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_app(*, args):
        captured["args"] = args

    monkeypatch.setattr(cli, "app", fake_app)
    monkeypatch.setattr(sys, "argv", ["tmuxctl", "j"])

    cli.main()

    assert captured["args"] == ["j"]


def test_main_rewrites_numeric_shortcut(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_app(*, args):
        captured["args"] = args

    monkeypatch.setattr(cli, "app", fake_app)
    monkeypatch.setattr(sys, "argv", ["tmuxctl", "12"])

    cli.main()

    assert captured["args"] == ["attach-recent", "12"]


def test_main_rewrites_numeric_shortcut_with_resize_window_short_flag(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_app(*, args):
        captured["args"] = args

    monkeypatch.setattr(cli, "app", fake_app)
    monkeypatch.setattr(sys, "argv", ["t", "1", "-r"])

    cli.main()

    assert captured["args"] == ["attach-recent", "1", "-r"]


def test_attach_recent_passes_resize_window_short_flag(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "tmuxctl.cli.tmux_api.list_session_info",
        lambda: [
            SessionInfo(name="git-llm-zoomcamp", created_at=3, activity_at=3),
        ],
    )

    def fake_attach_session(session_name: str, *, resize_window: bool = False) -> None:
        captured["session_name"] = session_name
        captured["resize_window"] = resize_window

    monkeypatch.setattr("tmuxctl.cli.tmux_api.attach_session", fake_attach_session)

    result = runner.invoke(app, ["attach-recent", "1", "-r"])

    assert result.exit_code == 0
    assert captured["session_name"] == "git-llm-zoomcamp"
    assert captured["resize_window"] is True


def test_app_shows_help_without_command() -> None:
    result = runner.invoke(app, ["--help"], terminal_width=120)

    assert result.exit_code == 0
    assert "Usage: " in result.output
    assert "COMMAND [ARGS]..." in result.output
    assert "Manage recurring jobs." in result.output
    assert "Rename a tmux session and retarget its scheduled jobs." in result.output
    assert "List tmux sessions sorted by creation time or activity." in result.output
    assert "Send a message to a tmux session." in result.output


def test_jobs_help_shows_nested_commands() -> None:
    result = runner.invoke(app, ["jobs", "--help"], terminal_width=120)

    assert result.exit_code == 0
    assert "Create a recurring message job for a tmux session." in result.output
    assert "Pause all scheduled jobs for the current tmux session." in result.output
    assert "Resume all scheduled jobs for the current tmux session." in result.output
    assert "Run the scheduler daemon or process due jobs once." in result.output


def test_app_shows_recent_sessions_without_command(monkeypatch) -> None:
    monkeypatch.setattr(cli, "PROGRAM_NAME", "tmuxctl")
    monkeypatch.setattr(
        "tmuxctl.cli.tmux_api.list_session_info",
        lambda: [
            SessionInfo(name="older", created_at=100, activity_at=300),
            SessionInfo(name="newer", created_at=200, activity_at=200),
        ],
    )

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert "IDX  SESSION               CREATED" in result.output
    assert "1    newer" in result.output
    assert "2    older" in result.output
    assert "Join a session: tmuxctl <id> or tmuxctl <session>" in result.output
    assert "Create a new one: tmuxctl :<session>" in result.output
    assert "Use current folder: tmuxctl - or tmuxctl -name" in result.output
    assert "Help: tmuxctl --help" in result.output


def test_app_shows_t_hints_without_command(monkeypatch) -> None:
    monkeypatch.setattr(cli, "PROGRAM_NAME", "t")
    monkeypatch.setattr("tmuxctl.cli.tmux_api.list_session_info", lambda: [])

    result = runner.invoke(app, [])

    assert result.exit_code == 0
    assert "Join a session: t <id> or t <session>" in result.output
    assert "Create a new one: t :<session>" in result.output
    assert "Use current folder: t - or t -name" in result.output
    assert "Help: t --help" in result.output


def test_main_runs_app_without_command(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_app(*, args):
        captured["args"] = args

    monkeypatch.setattr(cli, "app", fake_app)
    monkeypatch.setattr(sys, "argv", ["tmuxctl"])

    cli.main()

    assert captured["args"] == []


# ---------------------------------------------------------------------------
# doctor / strays / reap
# ---------------------------------------------------------------------------
def _socket_scan(monkeypatch, scans, dead=None, orphans=None):
    from tmuxctl import strays as strays_mod

    report = strays_mod.StrayReport(
        scans=scans,
        dead_sockets=dead or [],
        orphan_pids=orphans or [],
    )
    monkeypatch.setattr("tmuxctl.cli.strays_mod.scan_all", lambda *a, **k: report)
    return report


def _mk_scan(socket, sessions, *, reachable=True, pid=1, is_default=False):
    from tmuxctl import strays as strays_mod

    return strays_mod.SocketScan(
        socket=socket, reachable=reachable, is_default=is_default,
        pid=pid, sessions=sessions,
    )


def _mk_session(name, *, attached=False, idle_days=0.0, windows=1):
    from tmuxctl import strays as strays_mod
    import time

    activity = int(time.time() - idle_days * 86400)
    return strays_mod.StraySession(
        socket="s", name=name, activity_at=activity,
        attached=attached, windows=windows,
    )


def test_strays_lists_sessions_and_dead_sockets(monkeypatch) -> None:
    scan = _mk_scan(
        "/tmp/tmux-1000/default",
        [_mk_session("main", attached=True, idle_days=0), _mk_session("old", idle_days=20)],
        is_default=True,
    )
    _socket_scan(monkeypatch, [scan], dead=["/tmp/tmux-1000/deadsock"], orphans=[999])

    result = runner.invoke(app, ["strays"])
    assert result.exit_code == 0
    assert "main" in result.output
    assert "old" in result.output
    assert "/tmp/tmux-1000/deadsock" in result.output
    assert "999" in result.output


def test_strays_stale_filter(monkeypatch) -> None:
    scan = _mk_scan(
        "/tmp/tmux-1000/default",
        [_mk_session("fresh", idle_days=1), _mk_session("ancient", idle_days=30)],
    )
    _socket_scan(monkeypatch, [scan])

    result = runner.invoke(app, ["strays", "--stale", "14"])
    assert result.exit_code == 0
    assert "ancient" in result.output
    assert "fresh" not in result.output


def test_reap_dry_run_does_not_kill(monkeypatch) -> None:
    killed: list[list[str]] = []
    monkeypatch.setattr("tmuxctl.cli.subprocess.run", lambda args, **k: killed.append(args))

    scan = _mk_scan(
        "/tmp/tmux-1000/idlesock",
        [_mk_session("idle", attached=False, idle_days=30)],
    )
    _socket_scan(monkeypatch, [scan])

    result = runner.invoke(app, ["reap", "--stale", "14"])
    assert result.exit_code == 0
    assert "Would kill" in result.output
    assert "Dry-run" in result.output
    assert killed == []  # nothing actually killed


def test_reap_guards_attached_server(monkeypatch) -> None:
    killed: list[list[str]] = []
    monkeypatch.setattr("tmuxctl.cli.subprocess.run", lambda args, **k: killed.append(args))

    scan = _mk_scan(
        "/tmp/tmux-1000/livesock",
        [_mk_session("live", attached=True, idle_days=30)],
    )
    _socket_scan(monkeypatch, [scan])

    result = runner.invoke(app, ["reap", "--stale", "14", "--yes"])
    assert result.exit_code == 0
    assert "Nothing to reap" in result.output
    assert killed == []  # attached server is guarded


def test_reap_yes_kills_and_removes(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr("tmuxctl.cli.subprocess.run", lambda args, **k: calls.append(args))
    removed: list[str] = []
    monkeypatch.setattr("tmuxctl.cli.os.remove", lambda p: removed.append(p))

    scan = _mk_scan(
        "/tmp/tmux-1000/idlesock",
        [_mk_session("idle", attached=False, idle_days=30)],
    )
    _socket_scan(monkeypatch, [scan], dead=["/tmp/tmux-1000/deadsock"])

    result = runner.invoke(app, ["reap", "--stale", "14", "--yes"])
    assert result.exit_code == 0
    assert ["tmux", "-S", "/tmp/tmux-1000/idlesock", "kill-server"] in calls
    assert removed == ["/tmp/tmux-1000/deadsock"]


def test_doctor_runs(monkeypatch) -> None:
    _socket_scan(monkeypatch, [])
    monkeypatch.setattr("tmuxctl.cli._run_text", lambda args: "free output here")
    monkeypatch.setattr("tmuxctl.cli.robust.systemd_available", lambda: True)
    monkeypatch.setattr("tmuxctl.cli.tmux_api.list_sessions", lambda: ["main"])
    monkeypatch.setattr(
        "tmuxctl.cli.robust.scope_properties",
        lambda unit, props: {
            "MemoryMax": "5G",
            "MemorySwapMax": "8G",
            "MemoryPeak": "1G",
        },
    )

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "RAM" in result.output
    assert "main" in result.output
    assert "MemoryMax=5G" in result.output
    assert "MemorySwapMax=8G" in result.output


def test_limit_updates_live_session_limits(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_set_session_limits(
        session_name: str, *, mem: str | None, swap: str | None, high: str | None
    ) -> None:
        captured["session_name"] = session_name
        captured["mem"] = mem
        captured["swap"] = swap
        captured["high"] = high

    monkeypatch.setattr("tmuxctl.cli.tmux_api.set_session_limits", fake_set_session_limits)

    result = runner.invoke(
        app, ["limit", "main", "--mem", "30G", "--swap", "8G", "--high", "24G"]
    )

    assert result.exit_code == 0
    assert captured == {"session_name": "main", "mem": "30G", "swap": "8G", "high": "24G"}
    assert "Updated main: MemoryHigh=24G MemoryMax=30G MemorySwapMax=8G" in result.output


def test_limit_accepts_current_session_alias(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr("tmuxctl.cli.tmux_api.current_session_name", lambda: "main")
    monkeypatch.setattr(
        "tmuxctl.cli.tmux_api.set_session_limits",
        lambda session_name, *, mem=None, swap=None, high=None: captured.update(
            {"session_name": session_name, "mem": mem, "swap": swap, "high": high}
        ),
    )

    result = runner.invoke(app, ["limit", ":current", "--swap", "12G"])

    assert result.exit_code == 0
    assert captured == {"session_name": "main", "mem": None, "swap": "12G", "high": None}
    assert "Updated main: MemorySwapMax=12G" in result.output


def test_limit_requires_mem_or_swap(monkeypatch) -> None:
    monkeypatch.setattr(
        "tmuxctl.cli.tmux_api.set_session_limits",
        lambda session_name, *, mem=None, swap=None, high=None: (_ for _ in ()).throw(
            RuntimeError("provide --mem, --swap, --high, or a combination")
        ),
    )

    result = runner.invoke(app, ["limit", "main"])

    assert result.exit_code == 1
    assert "provide --mem, --swap, --high, or a combination" in result.output


def _mk_pane(window=0, pane=0, pid=111, command="bash", cwd="/home/a", active=True):
    from tmuxctl.models import PaneInfo

    return PaneInfo(
        window_index=window,
        pane_index=pane,
        pid=pid,
        command=command,
        cwd=cwd,
        active=active,
    )


def test_describe_capped_session(monkeypatch) -> None:
    monkeypatch.setattr("tmuxctl.cli.tmux_api.session_exists", lambda name: True)
    monkeypatch.setattr(
        "tmuxctl.cli.tmux_api.session_panes",
        lambda name: [_mk_pane(pid=111, command="nvim", cwd="/home/a/proj")],
    )
    monkeypatch.setattr("tmuxctl.cli.robust.systemd_available", lambda: True)
    monkeypatch.setattr(
        "tmuxctl.cli.robust.scope_properties",
        lambda unit, props: {
            "ActiveState": "active",
            "ControlGroup": "/user.slice/robust.slice/tmuxctl-proj.scope",
            "MemoryCurrent": str(3 * 1024**3),
            "MemoryPeak": str(5 * 1024**3),
            "MemoryMax": str(12 * 1024**3),
            "MemorySwapCurrent": "0",
            "MemorySwapMax": str(8 * 1024**3),
            "CPUUsageNSec": str(133 * 1_000_000_000),
            "TasksCurrent": "42",
        },
    )
    monkeypatch.setattr(
        "tmuxctl.cli._describe_memory_breakdown_lines",
        lambda cgroup: [
            "Memory by command (RSS):",
            "  python3              1.5G     6 process(es)",
            "Memory by process group (RSS):",
            "     1.2G     4 process(es)  PGID=1234     uv run make test",
        ],
    )

    result = runner.invoke(app, ["describe", "proj"])
    assert result.exit_code == 0
    assert "nvim" in result.output
    assert "/home/a/proj" in result.output
    assert "robust.slice/tmuxctl-proj.scope" in result.output
    assert "3.0G / 12.0G" in result.output
    assert "peak 5.0G" in result.output
    assert "swap 0B / 8.0G" in result.output
    assert "Memory by command (RSS):" in result.output
    assert "python3" in result.output
    assert "PGID=1234" in result.output
    assert "2m13s" in result.output
    assert "Tasks:    42" in result.output


def test_describe_uncapped_session_shows_real_cgroup(monkeypatch) -> None:
    monkeypatch.setattr("tmuxctl.cli.tmux_api.session_exists", lambda name: True)
    monkeypatch.setattr(
        "tmuxctl.cli.tmux_api.session_panes",
        lambda name: [_mk_pane(pid=999, command="claude", cwd="/home/a")],
    )
    monkeypatch.setattr("tmuxctl.cli.robust.systemd_available", lambda: True)
    monkeypatch.setattr(
        "tmuxctl.cli.robust.scope_properties", lambda unit, props: {"ActiveState": "inactive"}
    )
    monkeypatch.setattr(
        "tmuxctl.cli.tmux_api.process_cgroup",
        lambda pid: "/user.slice/user@1000.service/session-7.scope",
    )

    result = runner.invoke(app, ["describe", "plain"])
    assert result.exit_code == 0
    assert "uncapped" in result.output
    assert "session-7.scope" in result.output
    assert "--mem 24G" in result.output


def test_describe_missing_session(monkeypatch) -> None:
    monkeypatch.setattr("tmuxctl.cli.tmux_api.session_exists", lambda name: False)
    result = runner.invoke(app, ["describe", "ghost"])
    assert result.exit_code == 1
    assert "was not found" in result.output
