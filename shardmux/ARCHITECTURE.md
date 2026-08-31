# shardmux architecture

## Goal

Eliminate the shared-server failure mode structurally. The design must make it
impossible for the death of one session server to directly tear down unrelated
sessions.

## Process model

There is no global long-running shardmux process.

```text
shardmux CLI (short-lived)
│
├── session UUID A: shardmux-A.service / cgroup A
│   └── shardmux serve --id A
│       ├── Unix socket A
│       ├── PTY master A
│       └── shell / command tree A
│
├── session UUID B: shardmux-B.service / cgroup B
│   └── shardmux serve --id B
│       ├── Unix socket B
│       ├── PTY master B
│       └── shell / command tree B
│
└── session UUID C: shardmux-C.service / cgroup C
    └── ...
```

The short-lived CLI reads the durable registry and connects directly to the
selected session socket. Listing sessions probes each socket independently.
There is no coordinator whose death invalidates every session.

## Core invariants

1. **One server process per session.** A server owns only one session PTY.
2. **One socket per immutable UUID.** Names are aliases; rename cannot move live
   process identity.
3. **One systemd service/cgroup per session by default.** Memory accounting and
   OOM action do not cross session boundaries.
4. **No silent safety downgrade.** Requested memory limits require systemd. The
   unprotected direct launcher requires `--direct --no-limit`.
5. **The registry is not a daemon.** JSON files can be stale after SIGKILL/OOM;
   liveness is always established by probing the socket.
6. **The name index is disposable.** A dead session's name can be reclaimed
   without deleting its historical UUID record.
7. **A socket path is short and private.** Runtime/state directories are mode
   `0700`; sockets and records are mode `0600`.

## OOM behavior

The protected launcher creates a transient user service with:

- `MemoryMax=` — hard memory ceiling
- `MemorySwapMax=` — hard swap ceiling
- `MemoryOOMGroup=yes` — treat the cgroup as the OOM unit
- `OOMPolicy=kill` — fail the service as a unit after an OOM kill
- `KillMode=control-group` — cleanup descendants when the server stops

The server and its command tree intentionally share that **session's** cgroup.
If the workload exhausts the budget, losing that one session is acceptable. The
important property is that session B's PTY/server are not objects inside session
A's process, socket, or cgroup.

This differs from protecting one shared multiplexer server with a favorable OOM
score. OOM score tuning is policy and can be overridden by severe pressure;
separate process ownership is an architectural boundary.

## Identity and rename

A session receives a UUID before launch. These are derived from the UUID and
never change:

- Unix socket path
- JSON record path
- systemd unit name
- direct-launch log path

The human name is a small `name -> UUID` reservation file. Rename reserves the
new alias, updates the record atomically, and releases the old alias. No live
socket, service, PTY, or process must move.

## Registry consistency

The registry uses:

- a global advisory file lock for writes
- temporary-file + rename for record replacement
- `create_new` for name reservation
- one record per UUID to avoid a single database corruption/failure bottleneck

A clean server exit updates its record and releases its name. SIGKILL and OOM do
not permit cleanup, so commands verify liveness through a `PING/PONG` socket
exchange. `new` reclaims a stale alias; `prune` removes dead records and sockets.

## Wire protocol

The protocol is deliberately small and version-local for v0.1:

```text
1 byte type | 4 byte big-endian payload length | payload
```

Client frames:

- `ATTACH`
- `INPUT`
- `RESIZE`
- `DETACH`
- `PING`
- `INJECT`
- `KILL`

Server frames:

- `OUTPUT`
- `ATTACHED`
- `ACK`
- `ERROR`
- `PONG`
- `EXIT`

Frames are capped at 16 MiB. Interactive client writes are serialized so terminal
input and resize messages cannot interleave on the stream.

## PTY and attachment behavior

The server uses `portable-pty` to create the PTY, spawn either `$SHELL -l` or an
explicit command, forward input, and apply terminal sizes. A bounded `VecDeque`
retains recent raw output bytes. Reattach receives the retained bytes followed by
live output.

There is one active interactive client. A new `ATTACH` closes the previous
attachment, but non-interactive `send`, `status`, and `kill` connections remain
independent.

## Why raw scrollback first

A correct terminal emulator/grid is substantial work. Raw byte retention is
enough to validate:

- independent PTY ownership
- reconnect semantics
- client/server framing
- resize and input flow
- OOM and SIGKILL isolation

It is not enough for robust copy mode, exact screen restoration, or multi-client
rendering. Those belong after the process model is proven.

## Test that proves the primary invariant

The Linux integration test starts two direct test sessions, records session A's
server PID, sends `SIGKILL` to A, then verifies session B still answers `PING` and
accepts an `INJECT` frame. The direct launcher is used so the test needs no
systemd user manager in CI; the process/socket isolation under test is the same.

A future systemd integration test should add a small `MemoryMax`, deliberately
allocate past it in A, and verify B remains live while A's unit records an OOM.

## Extension path

### v0.2: operability

- shell completions
- project/user TOML configuration
- recent-index shortcuts and directory-derived names
- systemd memory/cpu metrics in `status`
- explicit OOM detection from unit/cgroup state
- event log and stronger crash reconciliation

### v0.3: multiple panes without reintroducing a global blast radius

Use one lightweight controller per session and one PTY broker per pane:

```text
session service/cgroup
├── session controller + socket
├── pane broker 1 + PTY 1
├── pane broker 2 + PTY 2
└── pane broker 3 + PTY 3
```

The controller stores layout and routes clients, but pane brokers own PTYs. A
controller restart can reconnect to pane brokers instead of destroying foreground
processes. A session-level OOM can still kill that session, while no process owns
panes from another session.

### Later: compatibility and automation

- tmuxctl command aliases/migration
- window/split/layout model
- scheduler as a separate optional service that communicates through session
  sockets, never owns PTYs
- richer terminal parsing/copy mode
- remote/client APIs with explicit authentication

## Non-goals for this first version

- preserving a command through its own cgroup OOM
- sharing one server for efficiency
- implementing the tmux command language
- pretending an uncapped/direct launch is protected
- using Python as a runtime dependency
