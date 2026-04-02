# tmux controller — implementation plan

## Goal

Build a small Python CLI tool that lets you:

- target a tmux session by **name**
- send a message to that session's active pane
- define recurring messages, such as every 15 minutes
- edit and manage recurring jobs simply
- run a lightweight scheduler loop that performs those sends

This is intentionally **session-name based**, not pane-ID based, for version 1.

---

## Product shape

Keep version 1 very small.

### User experience

Examples of the desired UX:

```bash
tmuxctl list
tmuxctl send codex "check status and fix if something is broken or stuck"
tmuxctl add codex --every 15m --message "check status and fix if something is broken or stuck"
tmuxctl jobs
tmuxctl edit 1
tmuxctl pause 1
tmuxctl resume 1
tmuxctl remove 1
tmuxctl daemon
```

### Scope for version 1

Include:

- list tmux sessions
- send one message now to a named session
- store recurring jobs
- run a daemon loop that sends due jobs
- pause/resume/remove jobs
- simple editing flow
- logs

Do not include yet:

- panes/windows targeting
- UI/TUI
- advanced busy detection
- remote tmux support
- system service integration

---

## Core design decision

### Target format

Only support:

- `session_name`

Meaning:

```bash
tmux send-keys -t codex "..." Enter
```

This sends to the **active pane of the active window** in that session.

This keeps the tool dead simple.

### Assumption

Each important Codex worker runs in its own tmux session with a meaningful name, like:

- `codex`
- `codex-fixer`
- `ops-agent`

That means session name is enough for control.

---

## Architecture

Use a small Python package with 5 main parts:

```text
tmuxctl/
  __init__.py
  __main__.py
  cli.py
  tmux_api.py
  storage.py
  scheduler.py
  models.py
```

### Responsibilities

#### `cli.py`
Defines CLI commands.

Suggested library: **Typer**

Why:
- simple
- good help text
- easy argument parsing
- nice developer experience

#### `tmux_api.py`
Wraps all direct tmux interactions.

Functions:
- list sessions
- verify a session exists
- send text to session
- optionally capture recent pane output later

#### `storage.py`
Handles persistence.

Use **SQLite** from the standard library via `sqlite3`.

Why:
- no extra dependency required
- reliable
- easy querying for due jobs
- better than JSON once editing/logs are involved

#### `models.py`
Lightweight dataclasses or typed helpers for jobs/logs.

#### `scheduler.py`
Implements the recurring execution loop:
- load due jobs
- send messages
- record log entries
- compute next run time

---

## Data model

Use SQLite with two tables to start.

### `jobs`

Fields:

- `id` INTEGER PRIMARY KEY
- `session_name` TEXT NOT NULL
- `message` TEXT NOT NULL
- `interval_seconds` INTEGER NOT NULL
- `enabled` INTEGER NOT NULL DEFAULT 1
- `send_enter` INTEGER NOT NULL DEFAULT 1
- `created_at` TEXT NOT NULL
- `updated_at` TEXT NOT NULL
- `last_run_at` TEXT NULL
- `next_run_at` TEXT NOT NULL

### `logs`

Fields:

- `id` INTEGER PRIMARY KEY
- `job_id` INTEGER NULL
- `session_name` TEXT NOT NULL
- `message` TEXT NOT NULL
- `status` TEXT NOT NULL
- `error_text` TEXT NULL
- `created_at` TEXT NOT NULL

### Why no `sessions` table

Because tmux sessions are dynamic. Always read live from tmux.

---

## CLI command plan

### 1. `list`

Purpose:
- show currently available tmux sessions

Example:

```bash
tmuxctl list
```

Output example:

```text
SESSION
codex
codex-fixer
ops-agent
```

Implementation:
- call `tmux list-sessions -F "#{session_name}"`

### 2. `send`

Purpose:
- immediately send a message to a tmux session

Example:

```bash
tmuxctl send codex "check status and fix if something is broken or stuck"
```

Implementation:
- confirm session exists
- run:
  ```bash
  tmux send-keys -t codex "<message>" Enter
  ```
- record log row with `job_id = NULL`

Optional flags:
- `--no-enter`
- `--dry-run`

### 3. `add`

Purpose:
- create a recurring job

Example:

```bash
tmuxctl add codex --every 15m --message "check status and fix if something is broken or stuck"
```

Behavior:
- validate session exists now
- parse interval into seconds
- create row in `jobs`
- set `next_run_at = now + interval`

Optional flags:
- `--no-enter`
- `--start-now` to set `next_run_at = now`

### 4. `jobs`

Purpose:
- list all recurring jobs

Example:

```bash
tmuxctl jobs
```

Output example:

```text
ID  ENABLED  SESSION  EVERY  NEXT RUN              MESSAGE
1   yes      codex    15m    2026-04-02 14:30:00   check status and fix...
2   no       ops      60m    2026-04-02 15:00:00   summarize backlog
```

### 5. `pause`

Purpose:
- disable a job without deleting it

Example:

```bash
tmuxctl pause 1
```

Behavior:
- set `enabled = 0`

### 6. `resume`

Purpose:
- re-enable a paused job

Example:

```bash
tmuxctl resume 1
```

Behavior:
- set `enabled = 1`
- set `next_run_at = now + interval`

### 7. `remove`

Purpose:
- delete a job

Example:

```bash
tmuxctl remove 1
```

Behavior:
- delete from `jobs`

### 8. `edit`

Purpose:
- edit an existing job simply

Keep first version minimal.

Supported edits:
- message
- interval
- session name
- enable/disable

Examples:

```bash
tmuxctl edit 1 --message "new text"
tmuxctl edit 1 --every 30m
tmuxctl edit 1 --session codex-fixer
```

Do not implement editor-based editing in v1 unless it is quick.

### 9. `logs`

Purpose:
- show recent send attempts

Example:

```bash
tmuxctl logs
tmuxctl logs --limit 20
```

Output example:

```text
TIME                 JOB  SESSION  STATUS   ERROR
2026-04-02 14:15:00  1    codex    success
2026-04-02 14:00:00  1    codex    success
2026-04-02 13:45:00  1    codex    failed   session not found
```

### 10. `daemon`

Purpose:
- continuously execute due jobs

Example:

```bash
tmuxctl daemon
```

Behavior:
- loop forever
- every few seconds:
  - query due enabled jobs
  - for each due job:
    - verify session exists
    - send message
    - record success/failure
    - update `last_run_at`
    - set new `next_run_at = now + interval`

Recommended polling interval:
- 2 to 5 seconds

---

## tmux integration details

### List sessions

Command:

```bash
tmux list-sessions -F "#{session_name}"
```

### Check session existence

Command:

```bash
tmux has-session -t <session_name>
```

Return code:
- 0 => exists
- non-zero => does not exist

### Send message

Command:

```bash
tmux send-keys -t <session_name> "<message>" Enter
```

If `--no-enter`:
- omit `Enter`

### Error handling

Wrap subprocess calls carefully.

Potential failures:
- tmux is not installed
- user is not inside an environment with tmux server available
- session does not exist
- target is invalid

User-facing behavior:
- friendly error message
- log failures when running from daemon

---

## Interval parsing

Support these formats:

- `15m`
- `30s`
- `2h`
- `1d`

A tiny parser is enough.

Rules:
- integer + suffix
- suffix one of `s`, `m`, `h`, `d`

Examples:
- `15m` => 900
- `2h` => 7200

Implement helper:

```python
parse_interval("15m") -> 900
format_interval(900) -> "15m"
```

Keep it simple.

---

## Scheduler behavior

### Basic loop

Pseudo-flow:

1. connect to database
2. query enabled jobs where `next_run_at <= now`
3. for each job:
   - confirm session still exists
   - send message
   - insert log row
   - update job timestamps
4. sleep a short interval
5. repeat

### Important implementation note

When a job is due:
- use the current send time as the base for the next run

Meaning:

```python
next_run_at = now + interval
```

This avoids burst catch-up behavior if daemon was paused or system slept.

### Failure policy

If a send fails because session is missing:
- log failure
- still schedule next run normally

Why:
- the session may come back later

Do not auto-disable on first failure in version 1.

---

## Logging plan

Log every send attempt.

Status values:
- `success`
- `failed`

For failed sends, record:
- session missing
- tmux command error
- unexpected exception

This will make debugging much easier.

---

## Storage location

Suggested default database path:

```text
~/.tmuxctl/tmuxctl.db
```

Also create directory if needed:

```text
~/.tmuxctl/
```

Later you can add:
- config file
- daemon PID file
- rotating log file

But not required now.

---

## Suggested package/dependency choices

### Required
- Python 3.10+
- `typer`

### Standard library
- `sqlite3`
- `subprocess`
- `datetime`
- `time`
- `pathlib`
- `dataclasses`
- `typing`

### Optional later
- `rich` for prettier tables
- `textual` for a TUI

For version 1, `typer` + stdlib is enough.

---

## Implementation order

### Phase 1 — tmux wrapper
Implement `tmux_api.py`

Functions:
- `list_sessions() -> list[str]`
- `session_exists(name: str) -> bool`
- `send_keys(session_name: str, message: str, press_enter: bool = True) -> None`

Test these manually first.

### Phase 2 — database
Implement `storage.py`

Functions:
- `init_db()`
- `create_job(...)`
- `list_jobs()`
- `get_job(job_id)`
- `update_job(...)`
- `delete_job(job_id)`
- `get_due_jobs(now)`
- `insert_log(...)`
- `list_logs(limit)`

### Phase 3 — CLI
Implement:
- `list`
- `send`
- `add`
- `jobs`
- `pause`
- `resume`
- `remove`
- `logs`

### Phase 4 — daemon
Implement `tmuxctl daemon`

Run and verify:
- due jobs execute
- logs are written
- next run updates correctly

### Phase 5 — editing
Implement `edit` with flags:
- `--message`
- `--every`
- `--session`

That is enough for version 1.

---

## Example file/module sketch

### `tmux_api.py`

Core idea:

- use `subprocess.run(..., capture_output=True, text=True)`
- check return codes
- raise custom exceptions for clarity

Suggested custom exceptions:
- `TmuxNotFoundError`
- `TmuxSessionNotFoundError`
- `TmuxCommandError`

### `storage.py`

On startup:
- ensure `~/.tmuxctl/` exists
- connect to SQLite DB
- create tables if missing

You can use simple SQL directly.

### `scheduler.py`

Functions:
- `run_daemon(poll_interval: int = 3)`
- `run_job(job)`

### `cli.py`

Use Typer app with commands:
- `list`
- `send`
- `add`
- `jobs`
- `pause`
- `resume`
- `remove`
- `edit`
- `logs`
- `daemon`

---

## Testing plan

### Manual tests

#### Test 1 — list sessions
- create tmux sessions
- ensure `tmuxctl list` shows them

#### Test 2 — one-off send
- start a test session:
  ```bash
  tmux new -d -s test-session
  ```
- send a message:
  ```bash
  tmuxctl send test-session "hello"
  ```
- attach and verify text arrived

#### Test 3 — recurring job
- add a short interval job:
  ```bash
  tmuxctl add test-session --every 30s --message "ping"
  ```
- run daemon
- verify repeated sends

#### Test 4 — missing session
- create job for session
- kill session
- verify daemon logs failures but continues running

#### Test 5 — pause/resume
- pause a job
- verify sends stop
- resume job
- verify sends continue

---

## Nice-to-have improvements after version 1

Do these only after the basic tool works.

### 1. Busy guard
Before sending:
- capture the current pane content
- skip sending if it looks busy

Potential future command:

```bash
tmuxctl add codex --every 15m --message "..." --skip-if-busy
```

### 2. Session auto-complete
CLI shell completion for session names.

### 3. Better edit UX
Open job definition in `$EDITOR`.

### 4. Rich output
Pretty tables with colors.

### 5. Optional support for `session:window`
For users who later want more precision.

### 6. Config file
For defaults like:
- poll interval
- database path
- log retention

---

## Risks and edge cases

### Active pane ambiguity
Because you are targeting only session name, tmux sends to the active pane in that session.

This is expected, but note:
- if you switch windows/panes in that session, the target changes

For version 1, accept this tradeoff.

### Session disappears
Scheduler should not crash.
Just log failure and continue.

### Duplicate sends
If two daemon processes run at once, a job may fire twice.

For version 1:
- document that only one daemon should run

Possible later fix:
- lock file

### Special characters in messages
Use subprocess argument lists, not shell=True.
That avoids quoting issues.

---

## Recommended first milestone

The smallest useful milestone is:

- `tmuxctl list`
- `tmuxctl send codex "hello"`
- `tmuxctl add codex --every 15m --message "..."`
- `tmuxctl daemon`

Once those work, the tool already solves the real need.

---

## Suggested initial project checklist

- [ ] create project structure
- [ ] add Typer entrypoint
- [ ] implement tmux session listing
- [ ] implement tmux session existence check
- [ ] implement send command
- [ ] implement SQLite initialization
- [ ] implement add job
- [ ] implement list jobs
- [ ] implement daemon loop
- [ ] implement logging
- [ ] implement pause/resume/remove
- [ ] implement edit
- [ ] manually test against a real named Codex tmux session

---

## Example of the final usage flow

Start a named session:

```bash
tmux new -s codex
```

From another shell:

```bash
tmuxctl add codex --every 15m --message "check status and fix if something is broken or stuck"
tmuxctl daemon
```

Then inspect:

```bash
tmuxctl jobs
tmuxctl logs
```

That is the whole product.

---

## Final recommendation

Keep version 1 narrow and reliable:

- session-name only
- CLI only
- SQLite only
- one daemon process
- no TUI
- no pane complexity

That will get you to a working tool quickly, and you can extend it later only if needed.
