from __future__ import annotations

import subprocess

from tmuxctl import tmux_api


def test_attach_session_can_resize_window(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(tmux_api, "session_exists", lambda session_name: True)

    def fake_run_tmux(args, *, check=True, timeout=10):
        captured["args"] = args
        captured["check"] = check
        captured["timeout"] = timeout
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tmux_api, "_run_tmux", fake_run_tmux)

    tmux_api.attach_session("git-llm-zoomcamp", resize_window=True)

    assert captured["args"] == [
        "attach-session",
        "-t",
        "git-llm-zoomcamp",
        ";",
        "resize-window",
        "-A",
    ]
    assert captured["check"] is False


def test_attach_session_can_resize_window_inside_tmux(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setenv("TMUX", "/tmp/tmux")
    monkeypatch.setattr(tmux_api, "session_exists", lambda session_name: True)

    def fake_run_tmux(args, *, check=True, timeout=10):
        captured["args"] = args
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tmux_api, "_run_tmux", fake_run_tmux)

    tmux_api.attach_session("git-llm-zoomcamp", resize_window=True)

    assert captured["args"] == [
        "switch-client",
        "-t",
        "git-llm-zoomcamp",
        ";",
        "resize-window",
        "-A",
    ]


def test_create_or_attach_runs_command_only_when_creating(monkeypatch) -> None:
    captured: list[list[str]] = []

    monkeypatch.delenv("TMUX", raising=False)
    session_exists_results = iter([False, True, True])
    monkeypatch.setattr(tmux_api, "session_exists", lambda session_name: next(session_exists_results))

    def fake_run_tmux(args, *, check=True, timeout=10):
        captured.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tmux_api, "_run_tmux", fake_run_tmux)

    tmux_api.create_or_attach_session("git-workshops", shell_command=["cy"])

    assert captured == [
        ["new-session", "-d", "-s", "git-workshops"],
        ["send-keys", "-t", "git-workshops", "cy"],
        ["send-keys", "-t", "git-workshops", "Enter"],
        ["attach-session", "-t", "git-workshops"],
    ]


def test_create_or_attach_preserves_create_command_quoting(monkeypatch) -> None:
    captured: list[list[str]] = []

    monkeypatch.delenv("TMUX", raising=False)
    session_exists_results = iter([False, True, True])
    monkeypatch.setattr(tmux_api, "session_exists", lambda session_name: next(session_exists_results))

    def fake_run_tmux(args, *, check=True, timeout=10):
        captured.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tmux_api, "_run_tmux", fake_run_tmux)

    tmux_api.create_or_attach_session(
        "git-workshops",
        shell_command=["python", "-c", "print('hello world')"],
    )

    assert captured[1] == [
        "send-keys",
        "-t",
        "git-workshops",
        "python -c 'print('\"'\"'hello world'\"'\"')'",
    ]


def test_create_or_attach_ignores_command_when_attaching(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(tmux_api, "session_exists", lambda session_name: True)

    def fake_attach_session(session_name: str, *, resize_window: bool = False) -> None:
        captured["session_name"] = session_name
        captured["resize_window"] = resize_window

    monkeypatch.setattr(tmux_api, "attach_session", fake_attach_session)

    tmux_api.create_or_attach_session("git-workshops", shell_command=["cy"])

    assert captured == {"session_name": "git-workshops", "resize_window": False}
