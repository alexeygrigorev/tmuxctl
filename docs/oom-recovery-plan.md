# Plan: eliminate the shared-server single point of failure, make sessions
# survive unattended, and make recovery a one-command operation

> **Revision note (2026-08-24):** this plan was reviewed against the actual
> codebase before implementation. Corrections from that review are folded in
> directly (marked inline where they change prior reasoning); the biggest
> changes are: dropping the "merge server+workload into one scope" idea (it
> would silently break §1), fixing the dtach-recreate path so it doesn't
> collide with §5's own squatting-guard, adding an explicit migration story
> for sessions that already exist on the shared server, and reordering
> implementation to build the log + guard (§2/§3) before the risky rewrite
> (§0/§1) instead of after.

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
default, not the exception. (Note: those specific fixtures belong to a
different project and are not present in this repo — see "Test strategy"
below for the in-repo equivalent to build against.)

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
(`cli.py:100-136`). There was no durable record of "what sessions exist,
where, with what config" independent of live process state.

Six pieces:

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

**Build order is §2 → §3 → §0 → §1 → §5 → §4** — see "Suggested
implementation order" at the end for why this differs from the numbering
(numbering reflects conceptual dependency/payoff, not build sequence: §0 is
still the change that actually fixes the incident, it's just not the
*first* thing to build).

---

## 0. Per-session tmux servers (structural isolation)

**Current model:** one shared tmux server (`tmuxctl-server.service`, fixed
unit name, default socket) hosts every named session. Each session's *pane
shell* is scope-wrapped into `robust.slice/tmuxctl-<name>.scope`
(`_new_session_command`, `tmux_api.py:359`) for its own memory cap, but the
server process that owns all the ptys is one shared thing, outside any
session's blast radius by cgroup accounting but not by process identity — if
*it* dies, for any reason (OOM, `systemctl stop`, `kill`, a bug), every
session dies with it, capped or not.

**New model:** no shared server. Each session gets its own tmux server on
its own socket, launched the first time that session is created and reused
after. Concretely:

- Drop the fixed `_SERVER_UNIT`/`_SERVER_BOOTSTRAP_SESSION`/`ensure_server()`
  machinery (and the hidden `__tmuxctl_server__` bootstrap-session dance,
  which existed only to keep a *shared* server alive with `exit-empty off` —
  per-session, you actually want the opposite: `exit-empty` on its default,
  so a session's dedicated server self-terminates when that session ends).
  Replace with a per-session equivalent: `tmuxctl-server-<name>.service`
  (`Type=forking`, `OOMScoreAdjust=-900`, **no memory cap** — this unit holds
  only the tmux server process, not the workload), launched via a variant of
  `server_bootstrap_argv` (`robust.py:538`) parameterized by session name.
  Server bootstrap and session creation become one command:
  `systemd-run --user --unit=tmuxctl-server-<name> -p Type=forking
  -p OOMScoreAdjust=-900 -- tmux -S <sock> new-session -d -s <name> ...`.

- **Socket location: use `tmux -L tmuxctl-<name>` rather than a bespoke path
  under `$XDG_RUNTIME_DIR`.** (Correction: an earlier draft of this plan
  suggested a custom path *and* claimed the existing socket scanner would
  pick it up — those two claims contradicted each other. `strays.py`'s
  `list_socket_paths` globs `$TMUX_TMPDIR|/tmp/tmux-<uid>/*` only, which is
  exactly where `tmux -L <name>` lands. Using `-L` means `list`/`recent`/
  `strays`/`reap`/doctor's socket scan all keep working with no changes to
  their glob, and `socket_for(name)` becomes a pure, deterministic function
  — `/tmp/tmux-<uid>/tmuxctl-<name>` — with no directory-creation or XDG
  fallback logic needed.) Sanity-check `sun_path` stays under the 108-byte
  unix socket limit for realistic session names (assert it; don't silently
  truncate).

- **Do not merge the server unit and the workload scope into one cgroup.**
  (Correction: an earlier draft suggested this as a simplification. It's
  wrong for two independent reasons: (1) systemd's default
  `KillMode=control-group` means stopping/failing that one unit reaps
  *everything* in its cgroup, including the dtach master §1 depends on
  staying alive independently of the tmux server's lifecycle — a merged
  scope makes "the foreground process survives the server dying" false
  whenever the death is systemd-mediated; (2) `OOMScoreAdjust=` applies to
  every process the unit spawns and `oom_score_adj` is inherited across
  fork/exec, so in a merged unit the workload inherits the server's -900 too
  and the knob that's supposed to make the kernel prefer killing the
  workload over the multiplexer cancels itself out.) Keep two units per
  session, as today: `tmuxctl-server-<name>.service` (tiny, protected, no
  cap) for the tmux server process, and `robust.slice/tmuxctl-<name>.scope`
  (capped) for the pane/dtach tree — just both now scoped to one session
  instead of the scope being the only per-session part.

- **Cross-server attach.** `attach_session` (`tmux_api.py:212-223`) uses
  `switch-client` when `$TMUX` is set — `switch-client` cannot cross
  servers, and tmux refuses to nest a second server inside a client that
  already has `$TMUX` set. Every "attach to a different session" path
  (`t <name>`, `t <index>`, `attach-last`, `attach-recent`) needs this
  fixed: from inside tmux, replace `switch-client` with
  `tmux detach-client -E 'tmux -S <target-socket> attach -t <name>'` (hands
  the client off to a fresh attach against the other server); from outside
  tmux, a plain `tmux -S <socket> attach -t <name>` as before.

- `tmux_api.py` functions (`create_or_attach_session`, `create_detached_session`,
  `attach_session`, `session_exists`, `send_keys`, ...) all currently assume
  the default/shared socket implicitly via `_run_tmux`. These need
  `socket_for(session_name)` threaded through as a `-S <path>` argument on
  every `tmux` invocation.

- **Rename.** `rename_session` today renames inside tmux only, and
  `kill_session` already has a latent bug where it stops
  `scope_unit_name(new_name)` after a rename, which never existed (leaking
  the old scope). Per-session sockets/units make this worse — everything is
  keyed by name. Fix by keying the server unit, socket, and scope by an
  **immutable session id** recorded in the §2 event log (name→id lookup for
  every name-based lookup), so rename only ever updates the tmux-internal
  name and one DB row, never any process/socket identity.

- `kill_session` must additionally stop the per-session server unit
  (`tmuxctl-server-<name>.service`) and remove its socket file — not just
  the workload scope as today.

- `doctor`'s "tmux server placement" check (`cli.py:956` area) becomes
  per-session: report placement for every known session's server, not one
  line for "the" server. Also add a line flagging any session still found on
  the legacy shared/default socket (see "Migration" below).

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
dtach -A "$XDG_RUNTIME_DIR/tmuxctl/dtach/<name>-<pane-id>.sock" -E -z -r winch -- <real command>
```

Flags matter: `-E` disables dtach's own detach hotkey (otherwise it steals
`^\`), `-z` disables its suspend key, `-r winch` forces a repaint on attach
so the pane isn't blank until the app redraws on its own (apps that handle
SIGWINCH — `claude`, vim, most TUIs — redraw immediately; a bare shell
prompt will still look blank until Enter is pressed, which is expected and
should be documented as such). SIGWINCH forwarding through the extra hop
does work: the attaching client forwards its size to the dtach master, which
`TIOCSWINSZ`s the inner pty, and the kernel delivers SIGWINCH to the app
normally.

Gate this feature on `shutil.which("dtach")` (mirroring the existing
`systemd_available()` pattern) and degrade to the bare shell with a one-line
warning if it's missing, plus a `doctor` check for it. Ship it behind a
config flag (`dtach_wrap` in `cgroups.toml`), **default off**, and flip it on
per-session before making it the global default — this is the piece with the
most user-facing behavior change (an extra hop in the pty path).

- `dtach` forks, creates the socket, and the *child* (the shell / `claude`
  process) keeps running attached to that pty regardless of whether anything
  is currently attached to the socket. If the outer tmux server dies, this
  process tree is **not a descendant of tmux** — it's a sibling, so tmux
  dying does not signal it at all. It just keeps running, unattended, exactly
  as if someone were still watching.
- Reattach, from a fresh tmux server/session (or directly, no tmux at all):
  `dtach -a <socket>`. A recreated tmux pane whose command is that same
  `dtach -a` line looks and behaves like a normal pane, just re-wrapping a
  process that's been running the whole time.
- **Recreate-after-crash must not collide with §5's uncapped-fallback
  guard.** After a server death, the dtach master (living in
  `robust.slice/tmuxctl-<name>.scope`, independent of the server) is still
  running by design — that's the entire point. `_new_session_command`'s
  existing `ActiveState == "active"` check treats *any* occupied scope as
  squatting and silently degrades to an uncapped session. Post-§1, "the
  scope is occupied by this session's own dtach master" is the expected,
  desired recreate case, not squatting. The recreate path must distinguish
  the two: if the active scope's `cgroup.procs` contains this session's own
  dtach master (identified via the §2 log's recorded dtach socket path, or
  by matching the running dtach master's `-A`/`-a` socket argument), attach
  a plain `dtach -a <socket>` pane into the *existing* scope without
  re-wrapping it in a new one. Only a foreign/unexpected occupant is actually
  squatting and should hit the `needs-manual-reclaim` hard-stop.
- Put the `dtach`-wrapped command inside the same memory-capped
  `robust.slice/tmuxctl-<name>.scope` as before — `dtach` itself is
  irrelevant memory-wise; the real command inside it is still what needs
  capping.
- `_login_shell()` (`tmux_api.py:226`, whose return value `_new_session_command`
  passes into `robust.scope_wrap`) is the place to change: wrap its return
  value in the `dtach -A ...` argv instead of returning the bare shell.
- **Windows/panes created interactively** (`C-b c`, splits) are not covered
  by wrapping just the initial `_login_shell()` call — they run the
  server's `default-command`. Set the per-session server's `default-command`
  to a small `tmuxctl _dtach-exec` helper that mints a fresh dtach socket
  per pane. Key sockets by a stable per-pane identifier, not `<name>-<window
  index>` (window indices renumber when windows close) — record the
  pane↔socket mapping in the §2 log or a per-session runtime-state file so
  salvage can reconstruct which sockets belong to which pane after a crash.
- **Downstream observability changes.** With dtach in the pane, tmux's
  `#{pane_current_command}` / `#{pane_current_path}` report the dtach
  *client* process, not the real workload — `describe`'s COMMAND/DIRECTORY
  columns need to resolve through the scope's `cgroup.procs` to find the
  actual child instead of trusting tmux's view. Also: killing the tmux
  server (or session) no longer kills the workload — `robust.stop_scope`
  (already invoked by `kill_session`) is now what actually terminates work,
  and `reap`'s server-level `kill-server` needs to additionally stop the
  corresponding scope, or a "reaped" idle session leaves its shell running
  invisibly forever.
- `doctor`/`salvage` (§5) can now check "is the dtach socket alive" as an
  even-more-fundamental durability signal than "is the tmux server alive" —
  the dtach socket outliving its tmux server is exactly the case this whole
  feature exists for.

---

## 2. Session event log

**Why this order:** everything else (`salvage`, `doctor`, post-mortems) is only as
good as the source of truth it reads from. Right now that source is transient
kernel/cgroup state. A durable log survives the crash that destroys that
state. **Build this before §0/§1** (see reordered "Suggested implementation
order" below) — it's the lowest-risk piece and gives every subsequent,
riskier change something durable to log against and debug with from day one.

**Where:** extend the existing `tmuxctl.db` (already used for `jobs`/`logs` in
`storage.py`) rather than a new file — one DB, one connection helper
(`storage.get_connection`). Writes must be best-effort (`try/except` around
the insert) so a locked or corrupt DB can never block session creation —
matching the "never block on config" convention `_resolve_session_mem`
already follows.

**Schema** — add to `init_db()` in `storage.py` (follow the existing
`CREATE TABLE IF NOT EXISTS` pattern at lines 33/48):

```sql
CREATE TABLE IF NOT EXISTS session_events (
    id INTEGER PRIMARY KEY,
    session_name TEXT NOT NULL,
    event TEXT NOT NULL,       -- 'created' | 'attached' | 'scope_wrapped' |
                                -- 'killed' | 'renamed' | 'server_bootstrap' |
                                -- 'health_check' | 'limit_changed'
    start_dir TEXT NULL,       -- cwd the session was created/reattached with
    mem TEXT NULL,             -- resolved MemoryMax at creation time
    swap TEXT NULL,
    high TEXT NULL,
    scope_unit TEXT NULL,      -- tmuxctl-<name>.scope
    socket_path TEXT NULL,     -- this session's tmux socket (and, in `detail`
                                -- as needed, any dtach socket paths) — the
                                -- thing salvage actually needs to print a
                                -- reattach command
    server_pid INTEGER NULL,   -- tmux server pid at time of event, if known
    detail TEXT NULL,          -- free-form (e.g. rename old->new name)
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_events_name ON session_events(session_name, created_at);
```

**Growth control:** don't log every §4 health-check tick unconditionally —
that's ~1,440 rows/day/forever for information that's almost always "still
fine." Log `health_check` rows only on a *transition* (was-down→now-up,
threshold crossed), not on every poll; add pruning of routine rows older than
N days if unconditional logging is used anywhere.

**Writers** — add `storage.record_session_event(conn, session_name, event, **fields)`
and call it from the existing lifecycle functions so this is append-only and
automatic, not something callers have to remember:

- `tmux_api.create_or_attach_session` and `create_detached_session`
  (`tmux_api.py:300`, `325`): after a successful `new-session`, record
  `created` with `start_dir`, resolved `mem`/`swap`/`high`, `socket_path`,
  and `robust.scope_unit_name(session_name)`.
- `_new_session_command` (`tmux_api.py:359`): this is where `mem`/`swap`/`high`
  are actually resolved — thread them back up instead of re-resolving in the
  caller.
- `cli.py` `rename` command: record `renamed` with `detail=f"{old}->{new}"`.
- `cli.py` `kill` command: record `killed`.
- `cli.py` `limit` command (changes a live session's cap): record
  `limit_changed` — without this the log silently lies about a session's
  effective cap after the first `limit` call.
- Per-session server bootstrap success path (§0's replacement for
  `tmux_api.ensure_server()`): record a `server_bootstrap` event with the
  server pid and socket path — this is what lets a future crash post-mortem
  answer "when did the server we just lost actually start, and was it ever
  properly placed."

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
bounds the sum. **Build this alongside §2, before §0/§1** — it's independent
of the server-isolation rewrite and gives visibility into the *other* thing
that made this incident bad (not just correlated failure, but genuine
oversubscription) while the riskier work is still in progress.

**Where:** `robust.py`, near `resolve_slice_max()`/`resolve_mem()` (used by
`ensure_slice` and `_resolve_session_mem`).

**Add** `total_reserved_mem() -> int`: sum `MemoryMax` (bytes) across every
live `tmuxctl-*.scope` unit via `systemctl --user list-units
'tmuxctl-*.scope' --all --no-legend --plain` + `scope_property(unit,
"MemoryMax")`. (Correction: an earlier draft claimed `doctor` already has
this exact enumeration to reuse — it doesn't. Doctor's current "robust
session scopes" section, `cli.py:985-1007`, enumerates *live tmux sessions*
via `tmux_api.list_sessions()`, which would miss the scopes of sessions
whose tmux process is already dead — precisely the case an oversubscription
total must still count. This function needs to be written against
`list-units` directly, scanning scope units rather than live sessions.) Skip
`infinity`/`[not set]` values using the same parsing convention as
`_scope_bytes` (`cli.py:1091`). Post-§0, exclude the new per-session
*server* units from the sum (they're uncapped/tiny by design — only the
workload scopes count).

**Add** `system_capacity() -> int`: physical RAM + swap. Extend the existing
`robust.total_ram_bytes()` (`robust.py:131`) with a sibling `SwapTotal`
reader from `/proc/meminfo`, rather than writing a new parallel parser.

**Enforcement point:** `_resolve_session_mem` (`tmux_api.py`, called from
`_new_session_command`) is where a new session's cap gets decided — this is
the single choke point for both `create_or_attach_session` and
`create_detached_session`. Before returning the resolved `mem`, check whether
`total_reserved_mem() + new_mem` exceeds a threshold fraction (config value,
default suggestion: 120% of `system_capacity()` — some oversubscription is
fine since sessions rarely peak simultaneously, 165G/93G is not fine).

- **Default: warn, don't block.** Print the same style of stderr note
  `_new_session_command` already uses for the scope-squatting case —
  `tmuxctl: total session memory caps (Xg) now exceed Y% of system capacity
  (Zg); consider lowering --mem or running 'tmuxctl doctor'`. Warn once per
  session-creation call, not repeatedly across a batch (e.g. a `salvage
  --recreate` run recreating 9 sessions shouldn't print 9 near-identical
  warnings). **This creation-time line is a courtesy, not the primary
  defense** — the plan's own postmortem noted the identical stderr channel
  was "easy to miss when running 9 of these back-to-back." The primary
  defense is the daemon (§4) logging a `capacity_warning` event on threshold
  *crossing*, and the unconditional `doctor` section below.
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

**Why:** a purely reactive per-session bootstrap (only triggered on the next
`create_or_attach`/`create_detached` for that session) means if nobody
touches a session for a while after its server dies, nothing notices or
fixes it. The scheduler daemon (`scheduler.py:73`, `run_daemon`) already runs
a persistent loop (`tmuxctl.service`, confirmed alive 58+ days in this
session) — piggyback on it instead of adding a second daemon.

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

`_check_server_health(conn)`: post-§0, "restart a dead session's server" is
no longer the idempotent no-op `tmux_api.ensure_server()` used to be (that
function is deleted by §0) — it means *recreating that session and
re-binding its dtach sockets*, i.e. running the same logic as `salvage
--recreate` for one session. That's a materially bigger blast radius than
the old no-op restart, so: **by default the health check only detects, logs,
and warns** (record a `session_events` row, print/log a warning) rather than
auto-recreating. Auto-recreate (actually invoking salvage logic
automatically) should be a separate, explicit config opt-in
(`auto_salvage = true`), off by default, for anyone who wants fully
unattended self-healing once they trust the recreate path. Separately, run
the §3 oversubscription check on this same cadence and log a
`capacity_warning` event on threshold crossing.

Keep `health_interval` generous (60s default is plenty — this is a cheap
per-session `tmux -S <sock> display-message` check, not the expensive
`doctor` scan) and don't run the full oversubscription scan every 3s poll
tick, only on the health-check cadence. `jobs daemon` in `cli.py:916` needs a
passthrough for `health_interval`/`auto_salvage` if either should be
configurable from the CLI rather than only `cgroups.toml`.

---

## 5. `tmuxctl salvage` command

**Why:** this formalizes exactly what recovery required doing by hand this
session: for each session, is there something to attach to, or something to
recreate? **Reread this section against §0/§1's actual shape before
implementing** — it was originally drafted against the old shared-server
world; the corrections below update its cgroup-path and classification
assumptions, but the mindset shift (from "guess from orphaned children" to
"read the log, then confirm against live state") is what to carry forward,
not the literal original mechanics.

**New file:** `tmuxctl/salvage.py`, structured like `strays.py` (dataclasses +
pure scan functions, no CLI concerns) — `cli.py` gets a thin `salvage`
command that calls it and prints, matching the `strays`/`_print_stray_report`
split already in the codebase.

**Scan logic** (`salvage.scan() -> SalvageReport`):

1. Enumerate `tmuxctl-*.scope` units: `systemctl --user list-units
   'tmuxctl-*.scope' --all --no-legend --plain`.
2. For each, read live PIDs via the existing `_cgroup_proc_pids` helper
   (`cli.py:1102`) rather than reading `cgroup.procs` directly — it already
   resolves the unit's control-group path correctly (via `systemctl --user
   show <unit> -p ControlGroup`), including through slice nesting.
3. Classify each live PID by cmdline (`/proc/<pid>/cmdline`):
   - Comm is `tmux` (classify by process name, not by presence of `-S`/`-L`
     — during the migration window some sessions may still be on the legacy
     default socket with no explicit `-S` flag) → **reattachable**: record
     the socket path and session name (via `tmux -S <socket> list-sessions`
     on that socket, or the default socket if no `-S`) and the exact
     `tmux -S <socket> attach -t <session>` command.
   - Comm is `dtach` → **reattachable via dtach**: record the dtach socket
     path and the `dtach -a <socket>` command — this is the case §1 exists
     for, and is a *good* outcome, not squatting (see §1's note on not
     confusing this with the uncapped-fallback guard).
   - Anything else → **orphaned work**: record cmdline + `/proc/<pid>/cwd`
     (resolve with `os.readlink`, and check `os.path.exists` on the resolved
     path to flag the deleted-worktree case hit today with `dtc-website`'s and
     `ai-shipping-labs`'s `runserver`s).
4. For scope units that no longer exist at all (fully reaped, as happened to
   `home-alexey` and `git-telegram-writing-assistant` today): look up the last
   `created`/`server_bootstrap` row for that session in `session_events`
   (§2) to recover `start_dir`/`mem`/`socket_path` instead of falling back to
   guessing from the name via `_current_directory_session_name`.

**CLI surface** (`cli.py`, next to `doctor`/`strays`/`reap`):

```
tmuxctl salvage                 # report only, like `strays`
tmuxctl salvage --recreate      # for every non-reattachable scope, run
                                 # create_detached_session(name, start_dir=...)
                                 # using the log (or live orphan cwd) — this is
                                 # the "one command" from today's manual recovery
tmuxctl salvage --kill-dead-cwd # dry-run by default (list what would be
                                 # killed); pass --yes to actually kill
                                 # orphans whose cwd no longer exists on disk,
                                 # matching the `reap`/`reap-clients` house
                                 # style of confirming destructive action
```

Concurrent runs: `--recreate` relies on `create_detached_session`'s
check-then-act idempotence, which two simultaneous `salvage --recreate`
invocations can race. Take a trivial advisory `flock` on a file next to the
DB for the duration of `--recreate`.

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
OOM event worse, not better. **Except** when the occupant is this session's
own dtach master (§1's post-crash recreate case) — that's the expected
outcome once §1 lands, not squatting, and should attach unwrapped into the
existing scope instead of hard-stopping.

---

## Migration: sessions that exist today on the shared server

This is not optional and has to land alongside §0, not after it. On
upgrade, every currently-running session is attached to the shared
default-socket server. Once `socket_for(name)` resolution is live, naive
name lookup will not find them there: `attach` fails, `session_exists`
returns `False`, and `create_or_attach_session` will happily create a
**duplicate** session for that name on a fresh per-session socket while the
original keeps running unnoticed on the old shared server. Worse: the
scheduler's `send_keys` jobs targeting those sessions would start silently
failing against the new (empty) per-session sockets, and
`MAX_CONSECUTIVE_FAILURES = 3` (`scheduler.py:52-54`) **deletes jobs** after
three failures — an irreversible loss of scheduled work, self-inflicted by
the upgrade, for the exact sessions this whole plan exists to protect.

**Required behavior:**

- Name resolution order becomes: (1) per-session socket
  (`/tmp/tmux-<uid>/tmuxctl-<name>`) if it exists and answers, (2) fall back
  to scanning the legacy default socket for an exact session-name match, (3)
  only create fresh (always on a per-session socket) if neither exists.
- `doctor` gets a line flagging any session still found on the legacy shared
  socket: `git-foo — shared-server (pre-migration); run 'tmuxctl salvage
  --recreate' or restart it to move to a dedicated server`.
- Do not stop/decommission `tmuxctl-server.service`/`tmuxctl-server.slice`
  until the default socket's session list is empty — check this explicitly
  before removing the old bootstrap machinery, don't just delete it as part
  of the §0 code change.
- Confirm `loginctl show-user $USER -p Linger` is enabled (it evidently is
  on this box, since `tmuxctl.service` survives logout today) — per-session
  server units depend on the same lingering behavior to survive across
  logout/reboot as the old shared one did.
- Add an explicit acceptance test: reboot (or simulate via stopping all
  `tmuxctl-*` units + clearing `/tmp/tmux-<uid>/*`), then run
  `tmuxctl salvage --recreate` and confirm every session in the §2 log comes
  back with the right directory and cap, with zero manual intervention.

---

## Test strategy

The plan originally referenced `ps2159`/`winch-app` fixtures as an
already-proven pattern to reuse for isolated-socket testing — those belong
to a different project and don't exist in this repo. The correct in-repo
equivalents to build from:

- `tests_integration/test_server_survives_login_teardown.py` — already uses
  isolated `-L` sockets and an `_isolated_bootstrap_argv` helper that
  retargets `server_bootstrap_argv`; this is the pattern §0's per-session
  bootstrap should follow and extend.
- `tests_integration/test_oom_integration.py` — existing OOM-adjacent
  integration coverage to extend with the "kill one session's server, others
  unaffected" scenario below.

New tests to add:

- Kill one session's server (`systemctl --user stop
  tmuxctl-server-<name>.service`), confirm every other session is completely
  unaffected — still attached, still serving, zero log entries about them.
  This is the core property the whole plan exists to guarantee.
- dtach survives server kill: kill the server, confirm the dtach master (and
  the process inside it) is still running; reattach and confirm it shows the
  live, uninterrupted process (e.g. a long-running counter or a `claude`
  session mid-task).
- Legacy session (created pre-migration, still on the shared/default socket)
  remains attachable and its scheduler jobs keep firing after upgrading the
  code — the migration fallback path actually works, not just in theory.
- Rename then kill: confirm the *correct* scope/socket/server unit gets torn
  down (regression test for the pre-existing rename/kill scope-name bug,
  which per-session sockets would otherwise make worse).
- `salvage --recreate` full round-trip after simulated reboot (see Migration
  section above).

---

## Suggested implementation order for the subagent

**§2 → §3 → §0 → §1 → §5 → §4** — reordered from a strictly-numbered
walkthrough to genuinely low-risk-first. (Correction: the original draft
built §0/§1 — the two most invasive, hardest-to-revert changes — first, and
the "pure addition, low risk, independent" pieces (§2, §3) *after* them.
That front-loads risk during exactly the period when there's no durable log
and no capacity guard yet to catch problems with. Building §2/§3 first costs
nothing — §2's `scope_unit`/`socket_path` fields just get populated
consistent with whatever naming §0 lands on — and means every §0 create/kill
is logged from day one, which doubles as the best debugging aid *for*
building §0.)

1. **§2 (session event log)** — no behavior change, pure addition, can be
   built and shipped independently of everything else. Land first so
   everything after this has a durable log to write to and read from.
2. **§3 (oversubscription guard)** — also independent of §0/§1, and
   addresses the *other* thing that made this incident bad (not just
   correlated failure, genuine 165G/93G oversubscription). Reference real
   numbers from this session's `doctor` output as the test case. Land the
   `doctor` section unconditionally so it's visible even before the guard's
   enforcement point is wired in.
3. **§0 (per-session servers)** — the actual structural fix for "one
   session's OOM shouldn't touch the others." This is the most invasive
   change (touches every `tmux_api.py` call site that assumes a socket, plus
   attach/rename/kill semantics), so it needs the most runway and test
   coverage — build against `test_server_survives_login_teardown.py`'s
   isolated-socket pattern. **Ship the migration fallback (see "Migration"
   above) in the same change**, not as a follow-up — there is no safe
   intermediate state where some sessions resolve per-session and others
   don't without the fallback in place.
4. **§1 (dtach wrapping)** — layer on top of §0 once per-session servers are
   solid. Ship behind the `dtach_wrap` config flag, default off; prototype
   against one real session before flipping the default. This is what
   actually delivers "an unattended Claude session survives and resumes."
5. **§5 (`salvage`)** using the log from §2 and reading the real shape of
   §0+§1 — becomes mostly "is this session's server/dtach socket alive →
   print/run the exact reattach command," with `needs-manual-reclaim`
   reserved for genuine foreign occupants (not this session's own surviving
   dtach master, per §1's correction above).
6. **§4 (daemon health check)** — smallest change, wired in last once §3's
   check function exists to call and §0/§1 define what "unhealthy" and
   "recover" actually mean per-session. Default to detect+log+warn only;
   gate actual auto-recreation behind an explicit `auto_salvage` opt-in.

Test against the actual failure signature from today, adapted to the new
architecture: pick one session, kill *only its own* server
(`systemctl --user stop tmuxctl-server-<name>.service`), and confirm every
other session is completely unaffected (still attached, still serving, no
log entries about them at all) — that's the property this whole plan exists
to guarantee. Then confirm `salvage` finds the killed one, `doctor` flags
oversubscription if applicable, and the daemon's health check logs the
outage within `health_interval` (and, if `auto_salvage` is on, recreates
that one session's server) without any manual `tmuxctl` invocation — and,
since §1 landed before §5, that the foreground process inside it (e.g. a
`claude` run) never stopped running and is exactly where it left off after
reattaching.
