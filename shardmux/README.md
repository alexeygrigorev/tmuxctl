# shardmux

`shardmux` is a Linux-first terminal session manager built around one rule:

> A session is a failure domain, not a row inside one global server.

Every session has its own Rust server process, Unix socket, PTY, child process
tree, and (by default) transient systemd user service/cgroup. There is no global
multiplexer daemon. Killing or OOM-killing one session server therefore cannot
remove the sockets, PTYs, or servers of unrelated sessions.

This directory is an intentionally small first version living beside the
existing Python `tmuxctl` implementation. It proves the isolation architecture
before adding windows, splits, a richer terminal model, scheduling, or migration
tools.

## What works in v0.1

- detached session creation
- interactive attach and detach (`Ctrl-b d`)
- raw PTY input/output with terminal resize propagation
- one active interactive client per session
- bounded raw-byte scrollback for reattach
- list, status, send, kill, rename, prune, and doctor commands
- immutable UUID identity, so rename does not rename sockets or systemd units
- one transient systemd user service and cgroup per session
- default `MemoryMax=12G` and `MemorySwapMax=8G`
- an integration test that SIGKILLs one session server and verifies another
  session remains live and accepts input
- PyPI-compatible binary wheels through Maturin

## Install from this checkout

```bash
uv tool install ./shardmux
```

For development:

```bash
cd shardmux
cargo test
cargo run -- --help
```

After the package is published:

```bash
uv tool install shardmux
```

The wheel contains the Rust `shardmux` executable; Python is only the package
distribution transport.

## Basic workflow

Create or attach:

```bash
shardmux new codex -c ~/git/project
```

Create detached:

```bash
shardmux create worker -c ~/git/project -- python -m worker
```

List and inspect:

```bash
shardmux list
shardmux status worker
shardmux status worker --json
```

Attach later:

```bash
shardmux attach worker
```

Detach with `Ctrl-b d`. Only the client disconnects; the PTY server and command
continue.

Send text and Enter:

```bash
shardmux send worker --message "check status and continue"
```

Stop one session:

```bash
shardmux kill worker
```

Rename without changing its immutable process/socket identity:

```bash
shardmux rename worker worker-main
```

Clean records left behind by SIGKILL, OOM, or a machine crash:

```bash
shardmux prune
```

## OOM isolation

The default launch resembles:

```bash
systemd-run --user --collect \
  --unit=shardmux-<immutable-uuid>.service \
  --service-type=exec \
  --property=KillMode=control-group \
  --property=MemoryOOMGroup=yes \
  --property=OOMPolicy=kill \
  --property=MemoryMax=12G \
  --property=MemorySwapMax=8G \
  -- shardmux serve --id <immutable-uuid>
```

The server then creates exactly one PTY and command tree. If that cgroup reaches
its hard memory ceiling, systemd and the kernel kill that session's cgroup as a
unit. Other sessions are different processes in different cgroups with different
sockets; they do not share a server that can become a correlated failure point.

Override the defaults per invocation:

```bash
shardmux create build --memory-max 24G --memory-swap-max 8G
```

Or with environment variables:

```bash
export SHARDMUX_MEMORY_MAX=24G
export SHARDMUX_MEMORY_SWAP_MAX=8G
```

Create a systemd-managed session without a hard memory ceiling:

```bash
shardmux create uncapped --no-limit
```

That keeps process/socket isolation, but it is not marked OOM-contained because
box-wide memory pressure can still select processes outside a bounded cgroup.

For tests or development on a machine without a systemd user manager, direct
launch is explicit and deliberately noisy in the interface:

```bash
shardmux create demo --direct --no-limit
```

`shardmux` never silently falls back from protected systemd launch to direct
launch. A command claiming a memory limit either gets the limit or fails.

## Persistence and paths

Records live under:

```text
$XDG_STATE_HOME/shardmux
# or ~/.local/state/shardmux
```

Sockets live under:

```text
$XDG_RUNTIME_DIR/shardmux
# or /tmp/shardmux-<uid>
```

Overrides, useful for tests:

```bash
SHARDMUX_STATE_DIR=/path/to/state
SHARDMUX_RUNTIME_DIR=/short/runtime/path
```

State is one JSON record per immutable UUID plus a tiny name-to-UUID reservation
file. The CLI is stateless; it discovers sessions from these records and probes
each independent socket.

## Current boundaries

This is an architecture-first MVP, not yet a tmux replacement feature-for-
feature:

- one PTY/pane per session
- one active interactive attachment per session; a new attachment takes over
- raw-byte scrollback, not a parsed terminal screen/grid
- Linux/systemd is the protected production path
- the session command does not survive that session's own server/cgroup being
  killed; the guarantee is that unrelated sessions survive
- no windows, splits, copy mode, scheduler, tmux import, or shell completion yet

The next architectural step should be multiple independent pane brokers under a
session controller. That lets a controller restart without owning every pane
PTY, while preserving the same no-global-daemon rule.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the process model, invariants, protocol,
and extension path.
