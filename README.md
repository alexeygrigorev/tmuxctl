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

Install from PyPI with `uv`:

```bash
uv tool install tmuxctl
tmuxctl --help
```

Or with `pip`:

```bash
pip install tmuxctl
tmuxctl --help
```

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
uv sync --dev
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
tmuxctl create-or-attach codex
tmuxctl :codex
tmuxctl attach-last
tmuxctl attach-last --by activity
tmuxctl attach-recent 2
tmuxctl attach-recent 3 --by activity
```

`tmuxctl :codex` is shorthand for `tmuxctl create-or-attach codex`.

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

Send one message from a file:

```bash
tmuxctl send rk-codex --message-file prompts/rk-codex-progress.txt
```

By default, `tmuxctl send` waits `200ms` before pressing Return. Override that with `--enter-delay-ms` or disable Return with `--no-enter`.

Create a recurring job:

```bash
tmuxctl add codex --every 15m --message "check status and fix if something is broken or stuck"
```

Recurring jobs also store an Enter delay. By default that is `200ms`, and you can override it with `--enter-delay-ms`.

Example: send an automated follow-up to `rk-codex` every 30 minutes:

```bash
tmuxctl add rk-codex --every 30m --message-file prompts/rk-codex-progress.txt
```

You can load message text from a file with `--message-file` for `tmuxctl send`, `tmuxctl add`, and `tmuxctl edit`.

For `tmuxctl add` and `tmuxctl edit`, the file path is stored with the job. Scheduled runs read the file at send time, so updating the prompt file changes future runs without recreating the job.

To switch an existing job to the shared prompt file:

```bash
tmuxctl jobs
tmuxctl edit <job_id> --message-file prompts/rk-codex-progress.txt
```

Example: check a worker session every 30 minutes and unblock stalled progress:

```bash
tmuxctl add lnewly-57 --every 30m --message "Status check for litehive: report current progress, current task, and the last meaningful change. Check whether progress is stalled, not whether you personally feel stuck. Identify blockers, lack of movement, repeated retries, failing commands, broken states, or missing dependencies. If progress is stalled, choose the best next concrete action to unblock litehive and execute it. Fix any problems you can fix now, then continue the work and summarize what changed."
```

Edit an existing job:

```bash
tmuxctl edit 2 --every 45m
tmuxctl edit 2 --message "check status and continue"
tmuxctl edit 3 --message-file prompts/rk-codex-progress.txt
```

Remove a job:

```bash
tmuxctl remove 3
```

List jobs and logs:

```bash
tmuxctl jobs
tmuxctl logs --limit 20
```

`tmuxctl jobs` shows whether a job uses inline text or a linked file prompt.

Logs include the target session, whether the send was manual or scheduled, whether Return was sent, the Enter delay used, and any recorded error text.

If a scheduled job fails 3 runs in a row, `tmuxctl daemon` removes it automatically.

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

## Bash Completion

`tmuxctl` includes shell completion through Typer.

Install completion for your current Bash setup:

```bash
tmuxctl --install-completion
```

Preview or manually wire the Bash completion script:

```bash
tmuxctl --show-completion bash
```

Session-taking commands also complete existing tmux session names in Bash.
