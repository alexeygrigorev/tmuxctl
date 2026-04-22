# tmuxctl

`tmuxctl` is a small tmux workflow helper for three things:

- finding the session you want
- jumping to it quickly
- sending recurring follow-ups to long-running agent or worker sessions

It installs two executables:

- `tmuxctl`
- `t`

`t` is just the shorter alias for the same CLI.

## Install

Primary install:

```bash
uv tool install tmuxctl
```

Then use either:

```bash
tmuxctl --help
t --help
```

Run without arguments to see the 10 most recent sessions plus shortcut hints:

```bash
tmuxctl
t
```

## Core Workflow

### 1. Find the session you want

Show all sessions, sorted by recency, with numeric IDs:

```bash
t list
```

Short form:

```bash
t l
```

Typical output:

```text
IDX  SESSION               CREATED
1    codex                 2026-04-03 15:56:59
2    backend-worker        2026-04-03 15:22:10
3    docs                  2026-04-03 14:10:31
```

If you just want the recent view:

```bash
t
t r
t recent --limit 10
```

### 2. Jump into a session

Attach by name:

```bash
t codex
```

That is equivalent to:

```bash
t attach codex
```

Attach by recency index:

```bash
t 1
t 2
t 10
```

Those resolve to `attach-recent N`.

Attach to the newest session directly:

```bash
t attach-last
```

### 3. Create a session if it does not exist

Use a leading colon when you want create-or-attach behavior:

```bash
t :codex
```

That resolves to:

```bash
t create-or-attach codex
```

Rule of thumb:

- `t codex` means attach only
- `t :codex` means create or attach

Use `t -` to derive the session name from the current directory and create-or-attach it:

```bash
cd ~/git/workshops
t -
```

That resolves to:

```bash
t create-or-attach git-workshops
```

### 4. Send a one-off message

Send text directly:

```bash
t send codex --message "check status and continue"
```

Or send from a file:

```bash
t send rk-codex --message-file prompts/rk-codex-progress.txt
```

By default, `send` waits `200ms` before pressing Enter. You can change that:

```bash
t send codex --message "status?" --enter-delay-ms 500
t send codex --message "status?" --no-enter
```

## Automation Workflow

### 1. Add a recurring job

Inline message:

```bash
t add codex --every 15m --message "check status and continue"
```

If you are already inside tmux, use `:current` to target the active session without typing its name:

```bash
t add :current --every 15m --message \
  "Check project status and continue. Help any blocked agents, review CI, and \
  keep the pipeline moving. If nothing in the current batch needs attention, \
  pick the next two ready issues per _docs/PROCESS.md and run the full workflow."
```

Shared prompt file:

```bash
t add rk-codex --every 30m --message-file prompts/rk-codex-progress.txt
```

When a job uses `--message-file`, `tmuxctl` stores the file path and reads the file at send time. Updating the file updates future scheduled runs.

### 2. Run the scheduler

```bash
t daemon
```

Recurring jobs only run while the daemon is running.

### 3. Inspect and edit jobs

```bash
t jobs
t logs --limit 20
t edit 2 --every 45m
t edit 2 --message "check status and continue"
t edit 2 --session :current
t edit 3 --message-file prompts/rk-codex-progress.txt
```

Useful job controls:

```bash
t pause 3
t resume 3
t remove 3
```

If a scheduled job fails 3 runs in a row, `tmuxctl daemon` removes it automatically.

## Session Cleanup

Kill a session by name:

```bash
t kill codex
```

Kill a session by the numeric ID shown in `t list`:

```bash
t kill 2
```

Skip confirmation:

```bash
t k 2 --yes
```

Rename a session and retarget any scheduled jobs bound to it:

```bash
t rename codex codex-main
t rename 2 archived-worker
```

## Shell Setup

### Bash completion

Install completion:

```bash
t --install-completion
```

Preview the script:

```bash
t --show-completion bash
```

Completion works for:

- commands
- plain session names
- `:session` shortcuts

### Local checkout helper

If you are working from this repository and want its virtualenv binaries on your `PATH`, run:

```bash
./install.sh
```

That appends this repo's `.venv/bin` to `~/.bashrc` and does nothing if the line is already present.

## How Scheduling Works

Recurring jobs are stored in:

```text
~/.config/tmuxctl/tmuxctl.db
```

The scheduler is database-driven:

- `add` creates jobs
- `edit`, `pause`, `resume`, and `remove` modify jobs
- `daemon` polls for due jobs and runs them

If you want recurring jobs to survive logout or reboot, keep `t daemon` running with something like:

- `systemd --user`
- `launchd`
- `cron @reboot`

### Running as a systemd user service (Linux)

Create `~/.config/systemd/user/tmuxctl.service`:

```ini
[Unit]
Description=tmuxctl scheduler daemon
After=default.target

[Service]
Type=simple
ExecStart=%h/.local/bin/tmuxctl daemon
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Adjust `ExecStart` to wherever `tmuxctl` is installed (for a local editable checkout, point at `.venv/bin/tmuxctl`). Then enable and start it:

```bash
systemctl --user daemon-reload
systemctl --user enable --now tmuxctl.service
systemctl --user status tmuxctl.service
```

To keep the daemon running after you log out, enable lingering for your user (needs sudo, one-time):

```bash
sudo loginctl enable-linger "$USER"
```

Logs are available via `journalctl --user -u tmuxctl -f`.

## Alternatives

Install with `pip`:

```bash
pip install tmuxctl
```

Install directly from GitHub:

```bash
uv tool install git+https://github.com/alexeygrigorev/tmuxctl.git
```

Install from a local checkout in editable mode:

```bash
git clone https://github.com/alexeygrigorev/tmuxctl.git
cd tmuxctl
uv tool install -e .
```

If you use the local checkout install, also run:

```bash
./install.sh
```

Reinstall the local checkout after updates:

```bash
uv tool install -e . --force
```

For development:

```bash
uv sync --dev
uv run pytest
uv build
```
