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

For local development with `uv`:

```bash
uv sync
uv run tmuxctl --help
```

For test and build tooling:

```bash
uv sync --group dev
uv run pytest
uv build
```

## Usage

List sessions:

```bash
uv run tmuxctl list
uv run tmuxctl recent --limit 10
uv run tmuxctl recent --limit 10 --by activity
```

Attach to a session directly or jump to the newest one:

```bash
uv run tmuxctl attach codex
uv run tmuxctl attach-last
uv run tmuxctl attach-last --by activity
uv run tmuxctl attach-recent 2
uv run tmuxctl attach-recent 3 --by activity
```

There are also short hidden aliases for the most recent sessions:

```bash
uv run tmuxctl a1
uv run tmuxctl a2
uv run tmuxctl a3
```

Send one message now:

```bash
uv run tmuxctl send codex "check status and fix if something is broken or stuck"
```

By default, `tmuxctl send` waits `200ms` before pressing Return. Override that with `--enter-delay-ms` or disable Return with `--no-enter`.

Create a recurring job:

```bash
uv run tmuxctl add codex --every 15m --message "check status and fix if something is broken or stuck"
```

Recurring jobs also store an Enter delay. By default that is `200ms`, and you can override it with `--enter-delay-ms`.

List jobs and logs:

```bash
uv run tmuxctl jobs
uv run tmuxctl logs --limit 20
```

Run the scheduler:

```bash
uv run tmuxctl daemon
```

## Shortcuts

Useful shortcuts for hopping between recent sessions:

- `uv run tmuxctl attach-last`
- `uv run tmuxctl attach-recent 2`
- `uv run tmuxctl attach-recent 3`
- `uv run tmuxctl a1`
- `uv run tmuxctl a2`
- `uv run tmuxctl a3`

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
