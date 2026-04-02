# tmuxctl

Small tmux session controller with recurring sends.

`tmuxctl` lets you:

- list tmux sessions by name
- show the most recent sessions by creation time or activity
- attach to a named session or jump to the most recent ones quickly
- send a message to a session's active pane
- store recurring jobs in SQLite
- run a lightweight daemon loop that executes due jobs

## Install

Install from GitHub with `uv`:

```bash
uv tool install git+https://github.com/alexeygrigorev/tmuxctl.git
tmuxctl --help
```

Install from a local checkout in editable mode:

```bash
git clone https://github.com/alexeygrigorev/tmuxctl.git
cd tmuxctl
uv tool install -e .
tmuxctl --help
```

If you update the local checkout later, reinstall with:

```bash
uv tool install -e . --force
```

For development, tests, and builds:

```bash
uv sync --group dev
uv run pytest
uv build
```

## Usage

List sessions:

```bash
tmuxctl list
tmuxctl recent --limit 10
tmuxctl recent --limit 10 --by activity
```

Attach to a session directly or jump to the newest one:

```bash
tmuxctl attach codex
tmuxctl attach-last
tmuxctl attach-last --by activity
tmuxctl attach-recent 2
tmuxctl attach-recent 3 --by activity
```

There are also short hidden aliases for the most recent sessions:

```bash
tmuxctl a1
tmuxctl a2
tmuxctl a3
```

Send one message now:

```bash
tmuxctl send codex "check status and fix if something is broken or stuck"
```

By default, `tmuxctl send` waits `200ms` before pressing Return. Override that with `--enter-delay-ms` or disable Return with `--no-enter`.

Create a recurring job:

```bash
tmuxctl add codex --every 15m --message "check status and fix if something is broken or stuck"
```

Recurring jobs also store an Enter delay. By default that is `200ms`, and you can override it with `--enter-delay-ms`.

List jobs and logs:

```bash
tmuxctl jobs
tmuxctl logs --limit 20
```

Run the scheduler:

```bash
tmuxctl daemon
```

## Shortcuts

Useful shortcuts for hopping between recent sessions:

- `tmuxctl attach-last`
- `tmuxctl attach-recent 2`
- `tmuxctl attach-recent 3`
- `tmuxctl a1`
- `tmuxctl a2`
- `tmuxctl a3`

## How Scheduling Works

`tmuxctl` does not create cron entries and it does not require editing `crontab`.

Recurring jobs are stored in SQLite at:

```text
~/.config/tmuxctl/tmuxctl.db
```

The commands work like this:

- `tmuxctl add ...` inserts a recurring job into the database
- `tmuxctl edit`, `pause`, `resume`, and `remove` update that stored job
- `tmuxctl daemon` polls the database for due jobs and runs them

That means recurring sends only happen while the daemon is running.

If you want jobs to keep running after logout or reboot, use an external process manager to keep the daemon alive, for example:

- `systemd --user` on Linux
- `launchd` on macOS
- `cron @reboot` as a fallback

Even in those setups, cron or systemd only starts `tmuxctl daemon`. The recurring schedule itself still lives in the `tmuxctl` database.
