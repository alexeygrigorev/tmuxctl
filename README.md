# tmuxctl

`tmuxctl` is a small tmux workflow helper. You use it to find the session you
want and jump to it quickly. It also keeps each session isolated so one crash
can't take down the rest, and it sends recurring follow-ups to long-running
agent or worker sessions.

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

## Find the session you want

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

## Jump into a session

Attach by name:

```bash
t codex
```

That's equivalent to:

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

If you're already inside tmux, attach still works across sessions. Each
session now has its own tmux server, so `t other-session` detaches your
client and attaches it to that server instead of running `switch-client` on
a shared one.

## Create a session if it does not exist

Use a leading colon when you want create-or-attach behavior:

```bash
t :codex
```

That resolves to:

```bash
t create-or-attach codex
```

The two forms mean different things:

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

## Create without attaching

`t create-detached` brings a memory-capped session into existence and returns
immediately, without occupying your terminal. Tools that attach over their own
transport (for example tmux `-CC` control mode) need this, because they would
otherwise build raw, uncapped `new-session` commands.

```bash
t create-detached myproj -c ~/git/myproj
```

It's a no-op if the session already exists. It resolves the memory cap the
same way as the other verbs (`--mem` flag, then project `cgroups.toml` /
`pyproject [tool.tmuxctl]`, then the default) and prints the session name on
success.

## Isolation: one server per session

tmux used to have one server process that owned every session. If that process
died, every pane disappeared at the same time, including unrelated work.

tmuxctl no longer shares a server. Each new session gets its own tmux server,
its own socket, and its own systemd unit. A crash in one session has no path
to the others.

```text
tmuxctl-server.slice                          (uncapped, protected)
├── tmuxctl-server-project-a.service
│   └── tmux -S /tmp/tmux-<uid>/tmuxctl-project-a
└── tmuxctl-server-project-b.service
    └── tmux -S /tmp/tmux-<uid>/tmuxctl-project-b

robust.slice                                  (capped parent for session work)
├── tmuxctl-project-a.scope  MemoryHigh=10.2G MemoryMax=12G MemorySwapMax=8G
│   └── login shell (+ optional dtach) + commands from that session
└── tmuxctl-project-b.scope  MemoryHigh=20.4G MemoryMax=24G MemorySwapMax=8G
    └── login shell (+ optional dtach) + commands from that session
```

We keep two units per session on purpose:

- `tmuxctl-server-<name>.service` holds only the multiplexer. It lives in
  `tmuxctl-server.slice`, has `OOMScoreAdjust=-900`, and has no memory cap.
- `tmuxctl-<name>.scope` holds the login shell and everything you launch
  from a pane, and it lives in `robust.slice` with `MemoryHigh`,
  `MemoryMax`, and `MemorySwapMax`.

Commands launched from a pane inherit the cgroup of that session's shell, so
memory accounting covers the whole process tree. When a scope crosses its
soft `MemoryHigh` threshold, the kernel throttles it and reclaims pages
(spilling cold ones to swap). Heavy work then slows down and waits under
transient pressure. Only if it still climbs to the hard `MemoryMax` does
systemd or the kernel kill processes inside that scope.

Sessions created before this change stay on the old shared socket until you
kill and recreate them. `t doctor` labels those as `LEGACY`. Recreate with
`t kill <name>` then `t :<name>`, or with `t salvage --recreate` after a
crash.

The actual create command is shaped like this:

```bash
systemd-run --user --unit=tmuxctl-server-my-session \
  -p Type=forking \
  -p Slice=tmuxctl-server.slice \
  -p OOMScoreAdjust=-900 \
  --quiet -- \
  tmux -S /tmp/tmux-<uid>/tmuxctl-my-session \
    new-session -d -s my-session -c /repo \
      systemd-run --user --scope \
        --unit=tmuxctl-my-session \
        -p MemoryHigh=25.5G \
        -p MemoryMax=30G \
        -p MemorySwapMax=8G \
        -p Slice=robust.slice \
        --quiet -- \
        /bin/bash -l
```

See [docs/cgroups.md](docs/cgroups.md) for slices, scopes, and how the
memory limits apply.

## Memory, throttle, and swap limits

By default, new sessions get `MemoryMax=12G`, `MemorySwapMax=8G`, and
`MemoryHigh` at 85% of `MemoryMax`. The memory cap contains a runaway
workload inside that session. `MemoryHigh` makes it throttle and reclaim
before the hard wall. The swap allowance lets a transient spike spill cold
pages so it survives instead of going straight to an OOM kill.

Set per-user defaults in `~/.config/tmuxctl/cgroups.toml`:

```toml
default_mem = "24G"
default_swap = "8G"
default_high = "20G"   # optional; omit to use 85% of mem
slice_max = "56G"
slice_swap_max = "16G"
oversubscription_max_pct = 120
dtach_wrap = false
auto_salvage = false
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

If the new cap would push the sum of every live scope's `MemoryMax` past
`oversubscription_max_pct` of RAM+swap (default 120%), create still
succeeds, but tmuxctl prints a warning. `t doctor` shows the same total.
Set `oversubscription_max_pct = 0` to disable the check.

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

Live changes aren't written back to config. Use `~/.config/tmuxctl/cgroups.toml`
or project config when you want future sessions to start with those limits.

## Survive this session's own server dying

A per-session server stops one session from killing another. It doesn't
stop this session's own server from dying if its workload blows the cap.
When that happens, tmux owns the pty, so whatever is running in the pane
(an unattended agent, a long test run) dies with it.

Opt in to wrapping the initial pane behind `dtach`, so that process keeps
running even if this session's tmux server exits:

```toml
# ~/.config/tmuxctl/cgroups.toml
dtach_wrap = true
```

This requires `dtach` on `PATH` (`apt install dtach`), and if the flag is on
and `dtach` is missing, the session starts as a normal shell and `t doctor`
warns. Only the session's first pane is wrapped, and panes you create later
inside tmux aren't.

## Send a one-off message

Send text directly:

```bash
t send codex --message "check status and continue"
```

Or send from a file:

```bash
t send rk-codex --message-file prompts/rk-codex-progress.txt
```

By default, `send` waits `200ms` before pressing Enter.

You can change the delay or skip Enter:

```bash
t send codex --message "status?" --enter-delay-ms 500
t send codex --message "status?" --no-enter
```

## Recurring jobs

Inline message:

```bash
t jobs add codex --every 15m --message "check status and continue"
```

If you're already inside tmux, use `:current` to target the active session
without typing its name:

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

When a job uses `--message-file`, `tmuxctl` stores the file path and reads
the file at send time. Updating the file updates future scheduled runs.

## Run the scheduler

Start the daemon with:

```bash
t jobs daemon
```

Recurring jobs only run while the daemon is running.

The same daemon also checks session health every 60 seconds
(`--health-interval`). It logs a `health_check` event only when the
unhealthy set changes (a new problem, or one that resolved), not on every
tick while something stays broken. It does the same for the
oversubscription warning.

By default it only detects and logs. Recreating an unhealthy session is a
bigger blast radius, so it's off until you opt in.

Pass `--auto-salvage`:

```bash
t jobs daemon --auto-salvage
```

Or set `auto_salvage = true` in `~/.config/tmuxctl/cgroups.toml`, but even
then sessions classified as `needs-manual-reclaim` are never recreated
automatically, so use `t salvage` for those.

Look at and edit jobs with:

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

## Kill and rename

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

A name starting with a dash is a suffix shorthand. `-cli` always means
`<session's directory>-cli`, so you don't have to retype the project part.

```bash
t rename 2 -cli          # git-dataops-sop  ->  git-dataops-cli
```

The prefix comes from the session's own working directory, using the same
rule that named it when it was created. The suffix replaces whatever
followed that prefix. A session with no suffix yet gains one
(`git-dtc-website` becomes `git-dtc-website-design`). The current name is
never consulted, so a name you picked by hand is normalized back onto its
directory.

After a rename, the per-session socket file moves with the new name so
lookups keep working. The systemd scope and server unit names stay tied to
the original name.

## `t describe`

`describe` shows what's actually running inside a session. For each pane it
prints the process, the working directory, and the cgroup. For sessions
started by `t` with a memory cap, it also prints live RAM and CPU usage
read straight from that cgroup.

Target it by name, by the numeric ID from `t list`, or `:current`:

```bash
t describe codex            # by name
t describe 2                # by the numeric ID from `t list`
t describe :current         # the session you are in
```

For a capped session it reads memory and CPU from the session's
`tmuxctl-<name>.scope` cgroup, so the numbers cover the whole process tree,
not just the shell:

```text
Session:  git-myproj  (1 window(s), 1 pane(s))

WIN.PANE  PID      COMMAND          DIRECTORY
0.0*      3564734  claude           /home/you/git/myproj

Scope:    tmuxctl-git-myproj.scope  (active)
Cgroup:   /user.slice/.../robust.slice/tmuxctl-git-myproj.scope
Memory:   3.4G / 12.0G  (peak 5.2G, swap 0B)
CPU time: 7m25s
Tasks:    222
```

A session you didn't start through `t` has no memory cap. `describe` says
so and prints the real cgroup it found, for example a plain
`session-NN.scope`. That tells you which sessions are protected and which
can still take the machine down under memory pressure.

```text
Scope:    none — session is uncapped (not started by tmuxctl)
Cgroup:   /user.slice/.../session-7.scope
          No per-session RAM/CPU cap; the box-wide OOM-killer can
          take the whole tmux server. Start a capped one with:
          t :git-myproj --mem 24G
```

## `t doctor`

Run `t doctor` to see RAM and cgroup OOM kills. It also shows live memory
limits, oversubscription, and `dtach` wrapping, plus whether each session
is on its own server or still on the legacy shared socket.

```bash
t doctor
```

## Session event log

These events go into a durable log:

- create
- kill
- rename
- limit
- health-check
- capacity-warning

That log survives the process and cgroup dying, so after a crash we can still
see which sessions existed and how they were created.

```bash
t sessions-log
t sessions-log --session git-myproj
t sessions-log --since 7 --limit 50
```

## Salvage after a crash

`t salvage` answers, for every tmuxctl session, whether there's something
live to reattach to, or something that needs recreating. It reads the event
log instead of guessing from the session name.

```bash
t salvage
```

Typical statuses:

```text
SESSION                    STATUS                DETAIL
git-myproj                 healthy               own server is up
git-other                  reattachable-dtach    dtach master survived
git-gone                   gone                  recreate from log
git-stale                  stale-work            leftover process holds the scope
git-foreign                needs-manual-reclaim  foreign occupant; do not auto-recreate
```

Recreate every `gone` / `stale-work` session (never `needs-manual-reclaim`):

```bash
t salvage --recreate
```

Kill leftover processes whose working directory no longer exists on disk.
Dry-run unless you pass `--yes`.

```bash
t salvage --kill-dead-cwd
t salvage --kill-dead-cwd --yes
```

## Strays and reap

Scan every tmux socket for leftover sessions, dead socket files, orphan
servers, and stranded `tmux -CC` control-mode clients:

```bash
t strays
t strays --stale 14
```

Kill detached idle servers and remove dead socket files. The command
dry-runs by default, and it never touches a server that still has an
attached session.

```bash
t reap
t reap --stale 14 --yes
```

Detach duplicate orphan control-mode clients (the PocketShell `-CC` crash
case). This never kills a session, a server, or an interactive client.

```bash
t reap-clients
t reap-clients --yes
```

## Bash completion

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

## Local checkout helper

If you're working from this repository and want its virtualenv binaries on your `PATH`, run:

```bash
./install.sh
```

This appends the repo's `.venv/bin` and `alias tl='t l'` to `~/.bashrc`,
skipping any line that's already present.

## Scheduling

Recurring jobs are stored in:

```text
~/.config/tmuxctl/tmuxctl.db
```

The same database holds the session event log (`session_events`).

The scheduler is database-driven:

- `jobs add` creates jobs
- `jobs edit`, `jobs pause`, `jobs resume`, and `jobs remove` modify jobs
- `jobs daemon` polls for due jobs, runs them, and on a coarser cadence
  (`--health-interval`, default 60s) scans session health

If you want recurring jobs to survive logout or reboot, keep `t jobs daemon` running with something like:

- `systemd --user`
- `launchd`
- `cron @reboot`

## Running as a systemd user service (Linux)

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

Adjust `ExecStart` to wherever `tmuxctl` is installed, and for a local
editable checkout point it at `.venv/bin/tmuxctl`.

Then enable and start it:

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

## An occupied scope can block a capped recreate of the same name

`t kill` tears a session's memory-capped scope down with
`systemctl --user stop tmuxctl-<name>.scope`, so the unit name is free for next time.

Two things have to go wrong together to leave the name occupied:

1. the tmux server dies uncleanly (a crash or machine-wide OOM), so the
   normal kill path, and its scope teardown, never runs
2. a disowned background process (for example an `Xvfb`, a dev server,
   anything `nohup`/`&`-launched) is still running inside that session's
   scope

The dead session's shell is gone, but the stray process keeps the
`tmuxctl-<name>.scope` cgroup alive.

tmuxctl no longer fails silently when you create that name again:

- a dead or failed leftover unit is reset automatically and the name is reused
- a surviving dtach master (if `dtach_wrap` is on) is treated as the
  session, and recreate reattaches into it
- any other live process holding the scope: create still succeeds, but
  the new session starts without a memory cap, and tmuxctl prints how to
  reclaim the name

Diagnose with:

```bash
t salvage
t doctor
systemctl --user status tmuxctl-<name>.scope
```

Stop the orphan scope (this also kills the stray process inside it), then
create the session again:

```bash
systemctl --user stop tmuxctl-<name>.scope
t :my-session
```

`t salvage --recreate` recreates `gone` and `stale-work` sessions, and it
won't touch `needs-manual-reclaim`.

## Sessions created before per-session servers

Older sessions still live on the shared default socket and they keep working.
They still share one server, so if that process dies they all die.
`t doctor` marks them `LEGACY`, so kill and recreate each one to move it onto
its own server.

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
