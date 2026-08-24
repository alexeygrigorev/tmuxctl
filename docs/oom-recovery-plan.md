# Plan: eliminate the shared-server single point of failure, make sessions
# survive unattended, and make recovery a one-command operation

## Background (what happened on 2026-08-24)

The cgroup OOM killer fired in `user-1000.slice` (`memory.events` showed
`oom_kill = 84`) and took out the shared tmux server around 12:07, killing
every session's pane shell **at once** — ~9-14 unrelated sessions went down
together because they all lived inside one process. The `robust.slice` /
`tmuxctl-server.slice` split (added in `d4556a1`, `09f9f67`) tries to protect
that one process with `OOMScoreAdjust=-900` and cgroup separation from the
capped workload scopes — and it *was* correctly placed — but this only
biases which process the kernel picks; it doesn't grant immunity, and
crucially **it does not change the fact that one process's death is a
correlated failure for every session**. That's the core problem, independent
of whether the OOM killer specifically was what pulled the trigger this time.

**The fix is not "protect the shared server harder." It's "stop sharing a
server."** Session isolation should be structural (different processes,
different cgroups — cannot be reached by each other's memory pressure by
construction), not probabilistic (a priority hint the kernel is free to
override under enough pressure). Confirmation this already works in
practice: two sessions survived this exact crash completely untouched
(`ps2159c`/`ps2159d`, pocketshell's own integration-test fixtures) — because,
unlike every real session, they *already* ran on their own dedicated tmux
server + socket instead of the shared one. That's the pattern to make the
default, not the exception.

Separately: even a correctly-isolated per-session server can still die (that
session's own workload can still legitimately blow its own cap). When that
happens today the interactive shell dies with it — including anything
running in its foreground, like an unattended Claude Code session mid-task.
`tmux`/the shell owns the pty; the pty dies with the server. Fixing isolation
doesn't fix that; it needs a separate, pty-durable layer underneath tmux.

And recovering after the crash required manually reverse-engineering, from
scratch, which sessions had existed and what directory each belonged to —
scraping `systemctl --user list-units 'tmuxctl-*.scope'`, reading
`cgroup.procs`, resolving `/proc/<pid>/cwd`, and guessing at the
name→directory convention in `_current_directory_session_name`
(`cli.py:97-131`). There was no durable record of "what sessions exist,
where, with what config" independent of live process state.

Five pieces. **§0 is the priority — it's the thing that actually stops this
from recurring** (§2 is what today's failure mode illustrates but is
secondary once §0 lands, since sessions genuinely can't take each other down
after §0). The rest layer on top:

0. **Per-session tmux servers** (structural isolation — one session's crash
   cannot touch another's)
1. **Pty-durable pane wrapping** (`dtach`) — survive *that session's own*
   server dying, unattended, without losing the foreground process
2. Session event log (durable recovery record)
3. Oversubscription guard (defense in depth — bounds aggregate risk even
   though no single session can cascade to others anymore)
4. Self-healing daemon check
5. `tmuxctl salvage` command (one-command inspect + reattach/recreate) — much
   simpler once §0+§1 land, since there's almost always something live to
   reattach to directly instead of an orphaned, pty-less child process

---

## 0. Per-session tmux servers (structural isolation)

**Current model:** one shared tmux server (`tmuxctl-server.service`, fixed
unit name, default socket) hosts every named session. Each session's *pane
shell* is scope-wrapped into `robust.slice/tmuxctl-<name>.scope`
(`_new_session_command`, `tmux_api.py:350`) for its own memory cap, but the
server process that owns all the ptys is one shared thing, outside any
session's blast radius by cgroup accounting but not by process identity — if
*it* dies, for any reason (OOM, `systemctl stop`, `kill`, a bug), every
session dies with it, capped or not.

**New model:** no shared server. Each session gets its own tmux server on
its own socket, launched the first time that session is created and reused
after. Concretely:

- Drop the fixed `_SERVER_UNIT`/`_SERVER_BOOTSTRAP_SESSION`/`ensure_server()`
  machinery in favor of a per-session equivalent:
  `tmuxctl-server-<name>.scope`, launched via a variant of
  `server_bootstrap_argv` (`robust.py:538`) parameterized by session name and
  a per-session socket path (e.g. `$XDG_RUNTIME_DIR/tmuxctl/<name>.sock`,
  matching the pattern already used ad hoc for the pocketshell test fixtures'
  `/tmp/ps-*.sock`).
- Keep the existing `robust.slice` capped-scope wrapping for the pane
  *shell*, but now nested under that session's own server rather than a
  shared one. Since the server and its one session's pane tree are now both
  scoped to that session and nothing else, consider **merging them into a
  single scope** (`tmuxctl-<name>.scope` contains both the tmux server and
  its pane children) — this is simpler than maintaining two cgroups per
  session and is exactly the shape `tmuxctl-git-pocketshell.scope` had by
  accident today (server + test child in the same scope, and it survived).
  `OOMScoreAdjust=-900` on the server process still makes sense *within* that
  scope, so if that session's own workload pressures its own cap, the kernel
  prefers killing the workload over the multiplexer — same idea as before,
  just scoped down to one session so the failure is contained.
- `tmux_api.py` functions (`create_or_attach_session`, `create_detached_session`,
  `attach_session`, `session_exists`, `send_keys`, ...) all currently assume
  the default/shared socket implicitly via `_run_tmux`. These need a
  `socket_for(session_name)` resolution threaded through (or a `-S <path>`
  argument added to every `tmux` invocation) instead of relying on
  `TMUX_TMPDIR`/default. `strays.py`'s `list_socket_paths` already scans all
  sockets under `/tmp/tmux-<uid>/*`, so `tmuxctl list`/`recent` (currently
  backed by the single default socket) need to become a scan across all
  `tmuxctl`-owned sockets — reuse that scanning logic rather than
  reimplementing it.
- `doctor`'s "tmux server placement" check (`cli.py:956` area) becomes
  per-session: report placement for every known session's server, not one
  line for "the" server.

**Payoff:** a `kill -9` on any one session's server, OOM or otherwise,
structurally cannot reach any other session's process tree — different
cgroup, different socket, no shared state. This is what actually satisfies
"if one session dies from OOM the others shouldn't be affected" — not as a
policy the kernel is asked to respect, but as something the kernel has no
path to violate.

---

## 1. Pty-durable pane wrapping (`dtach`)

**Why:** §0 contains the blast radius to one session, but that session's own
server can still die (its own workload can still hit its own cap, or
anything else can kill that one process). Today that kills the pty and
everything attached to it — including an unattended Claude Code session
mid-task, which is the concrete case motivating this: **the foreground
process must keep running through the multiplexer dying, and be resumable
exactly where it left off, without the user having been present.**

**Mechanism:** stop having tmux own the pty directly for the pane's command.
Instead, the pane's command becomes a tiny, near-zero-memory process (`dtach`,
not installed yet — `apt install dtach`; ~50KB binary, single purpose, does
nothing but hold a pty and proxy bytes to whoever attaches, no scrollback
buffering of its own to bloat its footprint) that itself execs the real
command (a shell, or directly `claude` for an agent session):

```
dtach -A "$XDG_RUNTIME_DIR/tmuxctl/dtach/<name>-<window>.sock" -- <real command>
```

- `dtach` forks, creates the socket, and the *child* (the shell / `claude`
  process) keeps running attached to that pty regardless of whether anything
  is currently attached to the socket. If the outer tmux server dies, this
  process tree is **not a descendant of tmux** — it's a sibling, so tmux
  dying does not signal it at all. It just keeps running, unattended, exactly
  as if someone were still watching.
- Reattach, from a fresh tmux server/session (or directly, no tmux at all):
  `dtach -a <socket>`. A recreated tmux pane whose command is that same
  `dtach -a` line looks and behaves like a normal pane, just re-wrapping a
  process that's been running the whole time — this is the literal
  "continue from the moment we stopped" the user asked for, not a simulation
  of it.
- Put the `dtach`-wrapped command inside the same memory-capped scope as
  before (§0's merged per-session scope) — `dtach` itself is irrelevant
  memory-wise; the real command inside it is still what needs capping.
- `_login_shell()` (`robust.py`, used by `scope_wrap`) is the natural place
  to change: wrap its return value in the `dtach -A ...` argv instead of
  returning the bare shell. Needs a per-session (and, if panes/windows are
  used, per-window) socket path helper alongside `scope_unit_name`.
- `doctor`/`salvage` (§5) can now check "is the dtach socket alive" as an
  even-more-fundamental durability signal than "is the tmux server alive" —
  the dtach socket outliving its tmux server is exactly the case this whole
  feature exists for.

**Scope note:** this is the piece with the most user-facing behavior change
(raw pty proxying through an extra hop) — worth prototyping against a single
session first (e.g. re-purpose the existing `ps2159`/winch-app test fixtures,
which already probe nested-pty/SIGWINCH-forwarding behavior) before rolling
out as the default for every session.

---

## 2. Session event log

**Why this order:** everything else (`salvage`, `doctor`, post-mortems) is only as
good as the source of truth it reads from. Right now that source is transient
kernel/cgroup state. A durable log survives the crash that destroys that
state.

**Where:** extend the existing `tmuxctl.db` (already used for `jobs`/`logs` in
`storage.py`) rather than a new file — one DB, one connection helper
(`storage.get_connection`).

**Schema** — add to `init_db()` in `storage.py` (follow the existing
`CREATE TABLE IF NOT EXISTS` pattern at lines 33/48):

```sql
CREATE TABLE IF NOT EXISTS session_events (
    id INTEGER PRIMARY KEY,
    session_name TEXT NOT NULL,
    event TEXT NOT NULL,       -- 'created' | 'attached' | 'scope_wrapped' | 'killed' | 'renamed'
    start_dir TEXT NULL,       -- cwd the session was created/reattached with
    mem TEXT NULL,             -- resolved MemoryMax at creation time
    swap TEXT NULL,
    high TEXT NULL,
    scope_unit TEXT NULL,      -- tmuxctl-<name>.scope
    server_pid INTEGER NULL,   -- tmux server pid at time of event, if known
    detail TEXT NULL,          -- free-form (e.g. rename old->new name)
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_events_name ON session_events(session_name, created_at);
```

**Writers** — add `storage.record_session_event(conn, session_name, event, **fields)`
and call it from the existing lifecycle functions so this is append-only and
automatic, not something callers have to remember:

- `tmux_api.create_or_attach_session` and `create_detached_session`
  (`tmux_api.py:300`, `325`): after a successful `new-session`, record
  `created` with `start_dir`, resolved `mem`/`swap`/`high`, and
  `robust.scope_unit_name(session_name)`.
- `_new_session_command` (`tmux_api.py:350`): this is where `mem`/`swap`/`high`
  are actually resolved — thread them back up instead of re-resolving in the
  caller.
- `cli.py` `rename` command: record `renamed` with `detail=f"{old}->{new}"`.
- `cli.py` `kill` command: record `killed`.
- `robust.ensure_server()` / `server_bootstrap_argv` success path
  (`tmux_api.py:256`): record a `server_bootstrap` event (session_name can be
  a sentinel like `__server__`) with the server pid — this is what lets a
  future crash post-mortem answer "when did the server we just lost actually
  start, and was it ever properly placed."

**Reader** — `tmuxctl sessions-log [--session NAME] [--since DAYS] [--limit N]`
in `cli.py`, modeled on the existing `jobs logs` command (`cli.py:907`,
`storage.list_logs`). This is the "replay the log to see status" the plan
should deliver directly: `tmuxctl sessions-log --session git-dtc-website`
shows exactly where that session was created, with what cap, and when it was
last touched — even if the process and cgroup are long gone.

**This is what `salvage` (§5) should consult first**, before falling back to
guessing directory from session name via `_current_directory_session_name`.

---

## 3. Oversubscription guard

**Why:** `git-ai-shipping-labs` was capped at 50G alone; summed across ~9
sessions the caps this session actually run at, from `doctor`'s own output
today, add up to roughly 165G against 62G RAM + 31G swap (~93G ceiling) — the
system is set up to allow ~1.8x oversubscription even before you add
uncapped-fallback sessions (see §5's note on the silent-uncapped-fallback bug
found during recovery). Per-scope `MemoryMax` only bounds one session; nothing
bounds the sum.

**Where:** `robust.py`, near `resolve_slice_max()`/`resolve_mem()` (used by
`ensure_slice` and `_resolve_session_mem`).

**Add** `total_reserved_mem() -> int`: sum `MemoryMax` (bytes) across every
live `tmuxctl-*.scope` unit, via the same `systemctl --user list-units
'tmuxctl-*.scope' --all --no-legend --plain` + `scope_property(unit,
"MemoryMax")` pattern already used by `doctor`'s "robust session scopes"
section (`cli.py` around 1202-1230, `_uncapped_scope_lines`/
`_describe_scope_lines` — reuse rather than reimplement).

**Add** `system_capacity() -> int`: physical RAM + swap, from `/proc/meminfo`
(`MemTotal` + `SwapTotal`), matching what `doctor`'s `free -h` panel already
surfaces informally — make it a real function instead of a shelled-out `free`
call.

**Enforcement point:** `_resolve_session_mem` (`tmux_api.py`, called from
`_new_session_command`) is where a new session's cap gets decided — this is
the single choke point for both `create_or_attach_session` and
`create_detached_session`. Before returning the resolved `mem`, check whether
`total_reserved_mem() + new_mem` exceeds a threshold fraction (config value,
default suggestion: 120% of `system_capacity()` — some oversubscription is
fine since sessions rarely peak simultaneously, 165G/93G is not fine).

- **Default: warn, don't block.** Print the same style of stderr note
  `_new_session_command` already uses for the scope-squatting case
  (`tmux_api.py` "scope ... still has live processes" message) —
  `tmuxctl: total session memory caps (Xg) now exceed Y% of system capacity
  (Zg); consider lowering --mem or running 'tmuxctl doctor'`. Don't silently
  degrade capability the way the systemd-unavailable path does — this is a
  capacity problem, not an environment problem, and the user should decide.
- **Config:** add `oversubscription_max_pct` to the existing `cgroups.toml` /
  `pyproject.toml [tool.tmuxctl]` config precedence chain (`robust.py` already
  has this machinery for `DEFAULT_MEM`/`DEFAULT_SWAP` — follow the same
  resolution order). A value of `0` disables the check for anyone who wants
  the old behavior.
- **`doctor` addition:** add an `== oversubscription ==` section to
  `cli.py:doctor()` showing `total_reserved_mem()` vs `system_capacity()` as a
  percentage, unconditionally (not just at creation time) — this is what
  would have surfaced the 165G/93G situation *before* today's crash instead of
  only after.

---

## 4. Self-healing daemon check

**Why:** `ensure_server()` (`tmux_api.py:256`) already does the right thing —
it's reactive, only called on the next `create_or_attach`/`create_detached`.
If nobody creates a session for a while after the server dies, nothing
notices or fixes it. The scheduler daemon (`scheduler.py:73`, `run_daemon`)
already runs a persistent loop (`tmuxctl.service`, confirmed alive 58+ days in
this session) — piggyback on it instead of adding a second daemon.

**Where:** `scheduler.py:run_daemon`.

```python
def run_daemon(*, poll_interval: int = 3, db_path: Path | None = None,
                health_interval: int = 60) -> None:
    conn = storage.get_connection(db_path)
    last_health_check = 0.0
    while True:
        for job in storage.get_due_jobs(conn):
            run_job(conn, job)
        now = time.monotonic()
        if now - last_health_check >= health_interval:
            _check_server_health(conn)
            last_health_check = now
        time.sleep(poll_interval)
```

`_check_server_health(conn)`: call `tmux_api.ensure_server()` (idempotent,
no-op if already running per its own docstring) and, separately, call the
oversubscription check from §3 and `storage.record_session_event(conn,
"__server__", "health_check", detail=...)` so the recovery log (§2) captures
*when* the daemon last confirmed the server was alive/protected — this is the
piece that turns "doctor catches it after a human runs it" into "the daemon
catches it within `health_interval` seconds and both fixes and logs it."

Keep `health_interval` generous (60s default is plenty — this is a cheap
`tmux display-message` check per `_server_running()`, not the expensive
`doctor` scan) and don't run the full oversubscription scan every 3s poll
tick, only on the health-check cadence.

---

## 5. `tmuxctl salvage` command

**Why:** this formalizes exactly what recovery required doing by hand this
session: for each `tmuxctl-*.scope`, is there something to attach to, or
something to recreate?

**New file:** `tmuxctl/salvage.py`, structured like `strays.py` (dataclasses +
pure scan functions, no CLI concerns) — `cli.py` gets a thin `salvage`
command that calls it and prints, matching the `strays`/`_print_stray_report`
split already in the codebase.

**Scan logic** (`salvage.scan() -> SalvageReport`):

1. Enumerate `tmuxctl-*.scope` units: `systemctl --user list-units
   'tmuxctl-*.scope' --all --no-legend --plain`.
2. For each, read live PIDs from
   `/sys/fs/cgroup/user.slice/user-<uid>.slice/user@<uid>.service/robust.slice/<unit>/cgroup.procs`
   (don't hardcode the path — resolve it via `systemctl --user show <unit> -p
   ControlGroup` the way `scope_properties` already does elsewhere, since the
   literal path depends on slice nesting and shouldn't be assumed).
3. Classify each live PID by cmdline (`/proc/<pid>/cmdline`):
   - Matches a tmux server invocation (`argv[0] == "tmux"` and `-S`/`-L`
     present) → **reattachable**: record the socket path and session name (via
     `tmux -S <socket> list-sessions` on that socket) and the exact `tmux -S
     <socket> attach -t <session>` command.
   - Anything else → **orphaned work**: record cmdline + `/proc/<pid>/cwd`
     (resolve with `os.readlink`, and check `os.path.exists` on the resolved
     path to flag the deleted-worktree case hit today with `dtc-website`'s and
     `ai-shipping-labs`'s `runserver`s).
4. For scope units that no longer exist at all (fully reaped, as happened to
   `home-alexey` and `git-telegram-writing-assistant` today): look up the last
   `created`/`server_bootstrap` row for that session in `session_events`
   (§1) to recover `start_dir`/`mem` instead of falling back to guessing from
   the name via `_current_directory_session_name`.

**CLI surface** (`cli.py`, next to `doctor`/`strays`/`reap`):

```
tmuxctl salvage                 # report only, like `strays`
tmuxctl salvage --recreate      # for every non-reattachable scope, run
                                 # create_detached_session(name, start_dir=...)
                                 # using the log (or live orphan cwd) — this is
                                 # the "one command" from today's manual recovery
tmuxctl salvage --kill-dead-cwd # kill orphans whose cwd no longer exists on disk
```

Report format, one line per session, mirroring `_print_stray_report`'s style:

```
SESSION                    STATUS       DETAIL
git-pocketshell             orphaned     tmux -S /tmp/ps-807f-....sock attach -t lab
git-dtc-website              stale-work   runserver pid 2422250, cwd DELETED (.tmp/website-main-deploy)
git-dataops                  stale-work   tsx dev-server pid 3662000, cwd OK (~/git/dataops)
home-alexey                  gone         last seen 2026-08-23 (session_events) — will recreate at ~
```

**Note on the scope-squatting bug hit during today's recovery:** `--recreate`
must not repeat the mistake found today — `create_detached_session` silently
falls back to an *uncapped* session when the old scope name is still occupied
(`tmux_api.py`'s `_new_session_command`, the `ActiveState == "active"`
branch), and it only warns to stderr, easy to miss when running 9 of these
back-to-back. `salvage --recreate` should treat that fallback as a hard stop
for that session (report it as `needs-manual-reclaim`, print the
`systemctl --user stop` line) rather than silently leaving a new session
running with no memory cap — that's the exact condition that makes the next
OOM event worse, not better.

---

## Suggested implementation order for the subagent

1. **§0 (per-session servers)** — this is the actual fix for "one session's
   OOM shouldn't touch the others." Land it first; everything downstream
   (salvage, doctor, the daemon check) is simpler and more meaningful once
   there's no shared server to reason about. This is also the most invasive
   change (touches every `tmux_api.py` call site that assumes a socket), so
   it needs the most runway and the most test coverage — reuse the
   `ps2159`-style fixtures (independent socket per test session) as the
   pattern, since that's already proven to work in this codebase.
2. **§1 (dtach wrapping)** — layer on top of §0 once per-session servers are
   solid. This is what actually delivers "an unattended Claude session
   survives and resumes." Prototype against one session before making it the
   default `_login_shell()` behavior for all of them.
3. **§2 (session event log)** — no behavior change, pure addition, low risk,
   independent of §0/§1, can be built in parallel. Update its `scope_unit`/
   `server_pid` fields to match whatever naming §0 lands on.
4. **§5 (`salvage`)** using the log from §2 — becomes mostly "is this
   session's server/dtach socket alive → print/run the exact reattach
   command" once §0+§1 exist, versus today's messier
   orphan/child-process-classification logic that this plan drafted against
   the *old* shared-server world (see §5 above — reread it against the new
   architecture before implementing, some of its cgroup-path assumptions are
   shared-server-era and need updating).
5. **§3 (oversubscription guard)** — independent, still worth having even
   after §0, since aggregate swap thrashing can still degrade every session's
   performance even when it can no longer kill them via a shared process.
   Reference real numbers from this session's `doctor` output (165G summed
   caps / 93G capacity) as the test case.
6. **§4 (daemon health check)** — smallest change, wire in last once §3's
   check function exists to call, and update it to check per-session server
   liveness (§0) rather than a single shared server.

Test against the actual failure signature from today, adapted to the new
architecture: pick one session, kill *only its own* server/scope
(`systemctl --user stop tmuxctl-<name>.scope`), and confirm every other
session is completely unaffected (still attached, still serving, no log
entries about them at all) — that's the property this whole plan exists to
guarantee. Then confirm `salvage` finds the killed one, `doctor` flags
oversubscription if applicable, and the daemon's health check rebootstraps
that one session's server within `health_interval` without any manual
`tmuxctl` invocation — and, if §1 landed, that the foreground process inside
it (e.g. a `claude` run) never stopped running and is exactly where it left
off after reattaching.
