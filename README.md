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
tl
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

Ask tmux to resize the window after attaching:

```bash
t git-llm-zoomcamp --resize-window
t 1 -r
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

Pass a command after `t -` to run it only when a new session is created:

```bash
t - cy
```

If you want another session for the same folder, add any suffix:

```bash
cd ~/git/workshops
t -asd
```

That resolves to:

```bash
t create-or-attach git-workshops-asd
```

#### Create without attaching

`t create-detached` brings a memory-capped session into existence and returns
immediately, without occupying your terminal. It is for tools that attach over
their own transport (e.g. tmux `-CC` control mode) and would otherwise build raw,
uncapped `new-session` commands:

```bash
t create-detached myproj -c ~/git/myproj
```

It is idempotent (a no-op if the session already exists), resolves the memory cap
the same way as the other verbs (`--mem` flag → project `cgroups.toml` /
`pyproject [tool.tmuxctl]` → default), and prints the session name on success.

#### Memory, throttle, and swap limits

tmux has one server process that owns all sessions. If one pane starts a
runaway build, test VM, emulator, or agent process and the machine runs out of
RAM, the kernel can kill that shared tmux server. When that happens, every tmux
session disappears, including unrelated work.

tmuxctl avoids that by starting each new session's login shell through
`systemd-run --user --scope`:

```text
tmux server
└── robust.slice
    ├── tmuxctl-project-a.scope  MemoryHigh=10.2G MemoryMax=12G MemorySwapMax=8G
    └── tmuxctl-project-b.scope  MemoryHigh=20.4G MemoryMax=24G MemorySwapMax=8G
```

The tmux server stays outside those per-session scopes. Commands launched from
a pane inherit the cgroup of that session's shell, so memory accounting covers
the whole process tree for that session. As a scope crosses its soft
`MemoryHigh` threshold the kernel throttles it and reclaims pages (spilling cold
ones to swap), so heavy work **slows down and waits** under transient pressure.
Only if it still climbs to the hard `MemoryMax` does systemd/kernel OOM handling
kill processes inside that scope — and even then the pressure stays contained
instead of spilling into the shared tmux server and unrelated sessions.

The actual create command is shaped like this:

```bash
tmux new-session -d -s my-session -c /repo \
  systemd-run --user --scope \
    --unit=tmuxctl-my-session \
    -p MemoryHigh=25.5G \
    -p MemoryMax=30G \
    -p MemorySwapMax=8G \
    -p Slice=robust.slice \
    --quiet -- \
    /bin/bash -l
```

See [docs/cgroups.md](docs/cgroups.md) for a more detailed explanation of
systemd, slices, scopes, the exact launch command, and how limits apply.

By default, new sessions get `MemoryMax=12G`, `MemorySwapMax=8G`, and
`MemoryHigh` at 85% of `MemoryMax`. The memory cap protects the tmux server from
runaway work; `MemoryHigh` makes a session throttle/reclaim before that hard
wall; and the swap allowance gives it room to spill cold pages so a transient
spike survives instead of going straight to an OOM kill.

Set per-user defaults in `~/.config/tmuxctl/cgroups.toml`:

```toml
default_mem = "24G"
default_swap = "8G"
default_high = "20G"   # optional; omit to use 85% of mem
slice_max = "56G"
slice_swap_max = "16G"
```

Set per-project defaults in either `cgroups.toml`:

```toml
mem = "24G"
swap = "8G"
high = "20G"   # optional; defaults to 85% of mem
```

or `pyproject.toml`:

```toml
[tool.tmuxctl]
mem = "24G"
swap = "8G"
high = "20G"
```

`swap = "0"` is still valid when you want hard no-swap scope behavior.
When `high` is omitted, it tracks `mem` automatically at 85%.

`--mem` and config defaults apply when a session is created:

```bash
t :my-session --mem 30G
t create-detached my-session --mem 30G
```

For an existing tmuxctl-created session, change the live systemd scope with
`limit`:

```bash
t limit my-session --mem 30G
t limit my-session --mem 30G --swap 8G --high 24G
t limit :current --swap 12G
```

Under the hood, this updates the session's systemd scope:

```bash
systemctl --user set-property tmuxctl-my-session.scope MemoryHigh=24G MemoryMax=30G MemorySwapMax=8G
```

Live changes are not written back to config. Use `~/.config/tmuxctl/cgroups.toml`
or project config when you want future sessions to start with those limits.

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
t jobs add codex --every 15m --message "check status and continue"
```

If you are already inside tmux, use `:current` to target the active session without typing its name:

```bash
t jobs add :current --every 15m --message \
  "Check project status and continue. Help any blocked agents, review CI, and \
  keep the pipeline moving. If nothing in the current batch needs attention, \
  pick the next two ready issues per _docs/PROCESS.md and run the full workflow."
```

Shared prompt file:

```bash
t jobs add rk-codex --every 30m --message-file prompts/rk-codex-progress.txt
```

When a job uses `--message-file`, `tmuxctl` stores the file path and reads the file at send time. Updating the file updates future scheduled runs.

### 2. Run the scheduler

```bash
t jobs daemon
```

Recurring jobs only run while the daemon is running.

### 3. Inspect and edit jobs

```bash
t jobs
t jobs list
t jobs show 2
t jobs logs --limit 20
t jobs edit 2 --every 45m
t jobs edit 2 --message "check status and continue"
t jobs edit 2 --session :current
t jobs edit 3 --message-file prompts/rk-codex-progress.txt
```

Useful job controls:

```bash
t jobs pause 3
t jobs pause-current
t jobs resume 3
t jobs resume-current
t jobs remove 3
```

If a scheduled job fails 3 runs in a row, `tmuxctl jobs daemon` removes it automatically.

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

## Inspect a Session

`describe` shows what is actually running inside a session — the process in each
pane, its working directory, the cgroup the session lives in, and (for sessions
started by `t` with a memory cap) live RAM and CPU usage read straight from that
cgroup. Target it by name, by the numeric ID from `t list`, or `:current`:

```bash
t describe codex            # by name
t describe 2                # by the numeric ID from `t list`
t describe :current         # the session you are in
```

For a capped session it reads memory and CPU from the session's
`tmuxctl-<name>.scope` cgroup, so the numbers cover the whole process tree, not
just the shell:

```
Session:  git-myproj  (1 window(s), 1 pane(s))

WIN.PANE  PID      COMMAND          DIRECTORY
0.0*      3564734  claude           /home/you/git/myproj

Scope:    tmuxctl-git-myproj.scope  (active)
Cgroup:   /user.slice/.../robust.slice/tmuxctl-git-myproj.scope
Memory:   3.4G / 12.0G  (peak 5.2G, swap 0B)
CPU time: 7m25s
Tasks:    222
```

A session you did **not** start through `t` has no memory cap. `describe` says so
and prints the real cgroup it found (e.g. a plain `session-NN.scope`), so you can
tell at a glance which sessions are protected and which can still take the whole
tmux server down under memory pressure:

```
Scope:    none — session is uncapped (not started by tmuxctl)
Cgroup:   /user.slice/.../session-7.scope
          No per-session RAM/CPU cap; the box-wide OOM-killer can
          take the whole tmux server. Start a capped one with:
          t :git-myproj --mem 24G
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

That appends this repo's `.venv/bin` and `alias tl='t l'` to `~/.bashrc`, skipping any line that is already present.

## How Scheduling Works

Recurring jobs are stored in:

```text
~/.config/tmuxctl/tmuxctl.db
```

The scheduler is database-driven:

- `jobs add` creates jobs
- `jobs edit`, `jobs pause`, `jobs resume`, and `jobs remove` modify jobs
- `jobs daemon` polls for due jobs and runs them

If you want recurring jobs to survive logout or reboot, keep `t jobs daemon` running with something like:

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
ExecStart=%h/.local/bin/tmuxctl jobs daemon
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

## Known Problems

### An orphaned scope can block recreating a session of the same name

Normally `t kill` tears a session's memory-capped scope down (`systemctl --user
stop tmuxctl-<name>.scope`), so the unit name is free for next time. Two things
have to go wrong together to defeat that:

1. the tmux **server dies uncleanly** (a crash or machine-wide OOM), so the
   normal kill path — and its scope teardown — never runs, **and**
2. a **disowned background process** (e.g. an `Xvfb`, a dev server, anything
   `nohup`/`&`-launched) is still running inside that session's scope.

The dead session's shell is gone, but the stray process keeps the
`tmuxctl-<name>.scope` cgroup alive. The next time you try to create a session
with the **same derived name** (e.g. `t -` from the same folder), tmuxctl asks
`systemd-run` for that unit name and it fails with *"Unit
tmuxctl-<name>.scope was already loaded"*. The new tmux pane's command dies on
launch, and instead of a clear error you usually see terminal escape codes
(a Device-Attributes reply such as `^[[?61;...c`) leak onto your prompt as the
aborted tmux client exits.

**Diagnose** — look for a scope whose tmux session no longer exists:

```bash
systemctl --user list-units --type=scope --all 'tmuxctl-*'
systemctl --user status tmuxctl-<name>.scope   # shows the stray process holding it open
```

**Fix** — stop the orphan scope (this also kills the stray process inside it),
then create the session again:

```bash
systemctl --user stop tmuxctl-<name>.scope
```

This is rare (it needs an unclean crash *and* a backgrounded process), so it is
documented rather than worked around. A future version may auto-recover by
clearing a stale scope whose session is gone before creating a new one.

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
