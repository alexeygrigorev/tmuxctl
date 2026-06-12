# tmuxctl cgroups

This document explains how tmuxctl uses systemd and Linux cgroups to stop one
runaway tmux session from taking down every other tmux session.

## The problem

tmux has one server process that owns all tmux sessions. Panes, windows, and
sessions are separate from the user's point of view, but they share that server.

If one pane starts a runaway build, test VM, emulator, or agent process and the
machine runs out of memory, the kernel can kill the shared tmux server. When
that happens, every tmux session disappears, including unrelated work.

tmuxctl's memory isolation exists to keep the tmux server out of the blast
radius of a single busy session.

## systemd and cgroups

Linux cgroups are the kernel feature for grouping processes and applying
resource accounting and limits, such as memory limits.

systemd is the service manager on most Linux systems. It also manages cgroups.
tmuxctl talks to the per-user systemd manager with:

```bash
systemd-run --user --scope
```

That asks systemd to create a temporary cgroup-backed unit for a command.

## Slice vs scope

A systemd slice is a parent group. It does not run your shell. It groups related
units and can set a shared resource budget.

A systemd scope is a live process group. It contains running processes and can
have its own resource limits.

tmuxctl uses this shape:

```text
robust.slice
├── tmuxctl-project-a.scope
│   └── shell + commands from that tmux session
├── tmuxctl-project-b.scope
│   └── shell + commands from that tmux session
└── tmuxctl-tests.scope
    └── shell + commands from that tmux session
```

Short version:

- `robust.slice`: parent bucket for all tmuxctl-created sessions.
- `tmuxctl-*.scope`: one running tmux session's process tree.

## How session launch works

When tmuxctl creates a capped session, the command is shaped like this:

```bash
tmux new-session -d -s my-session -c /repo \
  systemd-run --user --scope \
    --unit=tmuxctl-my-session \
    -p MemoryMax=30G \
    -p MemorySwapMax=8G \
    -p Slice=robust.slice \
    --quiet -- \
    /bin/bash -l
```

tmux starts the pane command. The pane command is `systemd-run`. `systemd-run`
asks the user systemd manager to create a transient scope named:

```text
tmuxctl-my-session.scope
```

systemd starts the session's login shell inside that scope:

```text
tmuxctl-my-session.scope
└── /bin/bash -l
    └── commands launched from the tmux panes
```

Processes launched from the shell inherit the same cgroup unless they
deliberately move themselves elsewhere. That means compilers, test runners,
emulators, agents, and their child processes count against the session's
`MemoryMax` and `MemorySwapMax`.

The tmux server itself stays outside the per-session scope.

## How limits apply

tmuxctl uses two layers:

```text
robust.slice
  MemoryMax=56G
  MemorySwapMax=16G

tmuxctl-my-session.scope
  MemoryMax=30G
  MemorySwapMax=8G
```

The scope limits apply to one session. If that session exceeds its hard memory
budget, systemd/kernel OOM handling kills processes inside that scope.

The slice limits apply to all tmuxctl-created sessions together. They keep the
collection of managed sessions from consuming the whole user memory budget.

## Memory and swap

`MemoryMax` is the hard memory ceiling for the cgroup.

`MemorySwapMax` is the swap allowance for the cgroup. A nonzero swap allowance
lets a transient spike spill cold pages to swap and continue. If the workload
keeps growing beyond the memory and swap budget, it still gets killed inside
that session's scope.

`MemorySwapMax=0` is still valid when you want hard no-swap behavior.

## Inspecting a session

Use:

```bash
t describe my-session
```

The memory line shows current memory, memory cap, peak memory, and swap usage:

```text
Memory:   13.0G / 30.0G  (peak 13.0G, swap 0B / 8.0G)
```

That means:

- `13.0G / 30.0G`: current RAM usage / `MemoryMax`.
- `peak 13.0G`: highest RAM usage seen by systemd.
- `swap 0B / 8.0G`: current swap usage / `MemorySwapMax`.

## Changing limits

Config defaults apply when new sessions are created. For example:

```toml
default_mem = "24G"
default_swap = "8G"
slice_max = "56G"
slice_swap_max = "16G"
```

Create a new session with a one-off memory cap:

```bash
t :my-session --mem 30G
```

Change an already-running tmuxctl-created session:

```bash
t limit my-session --mem 30G --swap 8G
```

That command updates the live systemd scope. It is equivalent to:

```bash
systemctl --user set-property tmuxctl-my-session.scope MemoryMax=30G MemorySwapMax=8G
```

Live changes are not written back to config. Use config when you want future
sessions to start with those limits.
