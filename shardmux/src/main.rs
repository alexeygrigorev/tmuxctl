mod cli;
mod client;
mod launcher;
mod model;
mod paths;
mod protocol;
mod registry;
mod server;
mod util;

use std::fs;
use std::io::{self, IsTerminal};
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;
use std::process::ExitCode;
use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail};
use clap::Parser;
use serde_json::json;
use uuid::Uuid;

use crate::cli::{Cli, Commands, SessionArgs, TargetArgs};
use crate::model::{
    DEFAULT_MEMORY_MAX, DEFAULT_MEMORY_SWAP_MAX, LauncherKind, MemoryLimits, SessionRecord,
    SessionState, now_ms,
};
use crate::registry::{Registry, validate_name};
use crate::util::{format_age, systemd_user_available, truncate_middle};

const MAX_SCROLLBACK_BYTES: usize = 64 * 1024 * 1024;

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("shardmux: {error:#}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<()> {
    let cli = Cli::parse();
    match cli.command.unwrap_or(Commands::List { json: false }) {
        Commands::New(args) => {
            let registry = Registry::open()?;
            let record = create_or_find(&registry, args)?;
            require_terminal()?;
            client::attach(&record)
        }
        Commands::Create(args) => {
            let registry = Registry::open()?;
            let record = create_or_find(&registry, args)?;
            println!("{}", record.name);
            Ok(())
        }
        Commands::Attach(TargetArgs { target }) => {
            let registry = Registry::open()?;
            let record = registry.resolve(&target)?;
            ensure_live(&record)?;
            require_terminal()?;
            client::attach(&record)
        }
        Commands::List { json } => list_sessions(&Registry::open()?, json),
        Commands::Status { target, json } => {
            status_session(&Registry::open()?, &target, json)
        }
        Commands::Send {
            target,
            message,
            no_enter,
        } => send_message(&Registry::open()?, &target, &message, no_enter),
        Commands::Kill(TargetArgs { target }) => kill_session(&Registry::open()?, &target),
        Commands::Rename { target, new_name } => {
            rename_session(&Registry::open()?, &target, &new_name)
        }
        Commands::Prune => prune(&Registry::open()?),
        Commands::Doctor => doctor(&Registry::open()?),
        Commands::Serve { id } => server::run(id),
    }
}

fn create_or_find(registry: &Registry, args: SessionArgs) -> Result<SessionRecord> {
    validate_name(&args.name)?;
    if args.scrollback_bytes > MAX_SCROLLBACK_BYTES {
        bail!(
            "--scrollback-bytes is capped at {MAX_SCROLLBACK_BYTES} bytes in this first version"
        );
    }

    let existing = match registry.find_by_name(&args.name) {
        Ok(record) => record,
        Err(error) => {
            eprintln!("warning: {error:#}");
            None
        }
    };
    if let Some(record) = existing {
        if client::is_live(&record) {
            return Ok(record);
        }
        mark_and_release_stale(registry, &record, "server socket is not responding")?;
    }

    let cwd = canonical_directory(args.cwd)?;
    let limits = resolve_limits(&args)?;
    let launcher = if args.direct {
        LauncherKind::Direct
    } else {
        LauncherKind::Systemd
    };
    if launcher == LauncherKind::Direct && limits.is_enabled() {
        bail!(
            "--direct cannot enforce memory limits; add --no-limit to explicitly accept an \
             unprotected development session"
        );
    }

    let id = Uuid::new_v4();
    let socket = registry.paths.socket_path(id)?;
    let unit_name = match launcher {
        LauncherKind::Systemd => Some(format!("shardmux-{}.service", id.simple())),
        LauncherKind::Direct => None,
    };
    let limits_applied = launcher == LauncherKind::Systemd && limits.is_enabled();
    let record = SessionRecord::new(
        id,
        args.name,
        cwd,
        args.command,
        socket,
        unit_name,
        launcher,
        limits,
        limits_applied,
        args.scrollback_bytes,
    );

    registry.create_session(&record)?;
    if let Err(error) = launcher::launch(registry, &record) {
        if let Some(unit) = &record.unit_name {
            let _ = launcher::stop_unit(unit);
        }
        let _ = registry.mark_stale(record.id, format!("launch failed: {error:#}"));
        let _ = registry.release_name_if_matches(&record.name, record.id);
        return Err(error).context(format!("failed to create session '{}'", record.name));
    }
    registry.load(record.id)
}

fn resolve_limits(args: &SessionArgs) -> Result<MemoryLimits> {
    if args.no_limit {
        return Ok(MemoryLimits::disabled());
    }
    let memory_max = args
        .memory_max
        .clone()
        .unwrap_or_else(|| DEFAULT_MEMORY_MAX.to_owned());
    let memory_swap_max = args
        .memory_swap_max
        .clone()
        .unwrap_or_else(|| DEFAULT_MEMORY_SWAP_MAX.to_owned());
    util::validate_systemd_value(&memory_max, "--memory-max")?;
    util::validate_systemd_value(&memory_swap_max, "--memory-swap-max")?;
    Ok(MemoryLimits::enabled(memory_max, memory_swap_max))
}

fn canonical_directory(path: PathBuf) -> Result<PathBuf> {
    let canonical = fs::canonicalize(&path)
        .with_context(|| format!("working directory does not exist: {}", path.display()))?;
    if !canonical.is_dir() {
        bail!("working directory is not a directory: {}", canonical.display());
    }
    Ok(canonical)
}

fn require_terminal() -> Result<()> {
    if !io::stdin().is_terminal() || !io::stdout().is_terminal() {
        bail!("attach requires an interactive terminal");
    }
    Ok(())
}

fn ensure_live(record: &SessionRecord) -> Result<()> {
    if client::is_live(record) {
        Ok(())
    } else {
        bail!(
            "session '{}' is not running; inspect it with `shardmux status {}` or remove stale records with `shardmux prune`",
            record.name,
            record.name
        )
    }
}

fn list_sessions(registry: &Registry, as_json: bool) -> Result<()> {
    let records = registry.list()?;
    let rows: Vec<_> = records
        .iter()
        .map(|record| {
            let live = client::is_live(record);
            json!({ "live": live, "session": record })
        })
        .collect();

    if as_json {
        println!("{}", serde_json::to_string_pretty(&rows)?);
        return Ok(());
    }
    if records.is_empty() {
        println!("No shardmux sessions.");
        return Ok(());
    }

    println!(
        "{:<24} {:<8} {:<12} {:<10} {:<7} DIRECTORY",
        "NAME", "LIVE", "STATE", "MEMORY", "AGE"
    );
    let now = now_ms();
    for record in records {
        let live = client::is_live(&record);
        let memory = memory_label(&record);
        println!(
            "{:<24} {:<8} {:<12} {:<10} {:<7} {}",
            truncate_middle(&record.name, 24),
            if live { "yes" } else { "no" },
            state_label(&record.state),
            truncate_middle(&memory, 10),
            format_age(record.updated_at_ms, now),
            truncate_middle(&record.cwd.display().to_string(), 60)
        );
    }
    Ok(())
}

fn status_session(registry: &Registry, target: &str, as_json: bool) -> Result<()> {
    let record = registry.resolve(target)?;
    let live = client::is_live(&record);
    if as_json {
        println!(
            "{}",
            serde_json::to_string_pretty(&json!({ "live": live, "session": record }))?
        );
        return Ok(());
    }

    println!("Name:              {}", record.name);
    println!("ID:                {}", record.id);
    println!("Live:              {}", if live { "yes" } else { "no" });
    println!("State:             {}", state_label(&record.state));
    println!("Launcher:          {}", launcher_label(&record.launcher));
    println!(
        "OOM-contained:     {}",
        if record.limits_applied { "yes" } else { "no" }
    );
    println!("MemoryMax:         {}", option_label(&record.limits.memory_max));
    println!(
        "MemorySwapMax:     {}",
        option_label(&record.limits.memory_swap_max)
    );
    println!("Server PID:        {}", pid_label(record.server_pid));
    println!("Child PID:         {}", pid_label(record.child_pid));
    println!("Directory:         {}", record.cwd.display());
    println!("Socket:            {}", record.socket.display());
    println!(
        "Systemd unit:      {}",
        record.unit_name.as_deref().unwrap_or("none")
    );
    println!("Scrollback bytes:  {}", record.scrollback_bytes);
    println!(
        "Command:           {}",
        if record.command.is_empty() {
            "$SHELL -l".to_owned()
        } else {
            record.command.join(" ")
        }
    );
    if let Some(message) = &record.message {
        println!("Last message:      {message}");
    }
    Ok(())
}

fn send_message(registry: &Registry, target: &str, message: &str, no_enter: bool) -> Result<()> {
    let record = registry.resolve(target)?;
    ensure_live(&record)?;
    let mut bytes = message.as_bytes().to_vec();
    if !no_enter {
        bytes.push(b'\r');
    }
    client::inject(&record, &bytes)
}

fn kill_session(registry: &Registry, target: &str) -> Result<()> {
    let record = registry.resolve(target)?;
    if client::is_live(&record) {
        let _ = client::request_kill(&record);
        if wait_until_dead(&record, Duration::from_secs(5)) {
            println!("killed {}", record.name);
            return Ok(());
        }
    }

    match record.launcher {
        LauncherKind::Systemd => {
            if let Some(unit) = &record.unit_name {
                let _ = launcher::stop_unit(unit);
            }
        }
        LauncherKind::Direct => {
            if let Some(pid) = record.server_pid {
                terminate_pid(pid);
            }
        }
    }
    let _ = wait_until_dead(&record, Duration::from_secs(2));
    mark_and_release_stale(registry, &record, "session was stopped")?;
    let _ = fs::remove_file(&record.socket);
    println!("killed {}", record.name);
    Ok(())
}

fn wait_until_dead(record: &SessionRecord, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if !client::is_live(record) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    !client::is_live(record)
}

fn terminate_pid(pid: u32) {
    unsafe {
        libc::kill(pid as libc::pid_t, libc::SIGTERM);
    }
    std::thread::sleep(Duration::from_millis(500));
    unsafe {
        if libc::kill(pid as libc::pid_t, 0) == 0 {
            libc::kill(pid as libc::pid_t, libc::SIGKILL);
        }
    }
}

fn rename_session(registry: &Registry, target: &str, new_name: &str) -> Result<()> {
    validate_name(new_name)?;
    if let Ok(Some(existing)) = registry.find_by_name(new_name) {
        if client::is_live(&existing) {
            bail!("session name '{new_name}' is already in use");
        }
        mark_and_release_stale(registry, &existing, "reclaimed during rename")?;
    }
    let record = registry.resolve(target)?;
    let old_name = record.name.clone();
    let updated = registry.rename(record.id, new_name)?;
    println!("renamed {old_name} to {}", updated.name);
    Ok(())
}

fn prune(registry: &Registry) -> Result<()> {
    let mut removed = 0_usize;
    for record in registry.list()? {
        if client::is_live(&record) {
            continue;
        }
        if let Some(unit) = &record.unit_name {
            let _ = launcher::stop_unit(unit);
        }
        registry.remove_record(record.id)?;
        removed += 1;
    }
    println!("pruned {removed} dead session record(s)");
    Ok(())
}

fn doctor(registry: &Registry) -> Result<()> {
    let systemd = systemd_user_available();
    println!(
        "[{}] systemd user manager",
        if systemd { "ok" } else { "fail" }
    );
    if !systemd {
        println!(
            "       Default sessions cannot be created safely. Check `systemctl --user status` and user lingering."
        );
    }

    check_directory("state directory", &registry.paths.state_dir)?;
    check_directory("runtime directory", &registry.paths.runtime_dir)?;

    let records = registry.list()?;
    let mut live = 0;
    let mut contained = 0;
    let mut stale = 0;
    for record in &records {
        if client::is_live(record) {
            live += 1;
            if record.limits_applied {
                contained += 1;
            } else {
                println!(
                    "[warn] session '{}' is live but not memory-contained ({})",
                    record.name,
                    launcher_label(&record.launcher)
                );
            }
        } else {
            stale += 1;
            println!(
                "[warn] session '{}' has a record but no responding server",
                record.name
            );
        }
    }
    println!("[info] {live} live session(s), {contained} OOM-contained, {stale} stale");
    if stale > 0 {
        println!("       Run `shardmux prune` after inspecting stale sessions.");
    }
    Ok(())
}

fn check_directory(label: &str, path: &std::path::Path) -> Result<()> {
    let metadata = fs::metadata(path)
        .with_context(|| format!("cannot inspect {label} {}", path.display()))?;
    let mode = metadata.permissions().mode() & 0o777;
    let protected = mode & 0o077 == 0;
    println!(
        "[{}] {label}: {} (mode {mode:04o})",
        if protected { "ok" } else { "warn" },
        path.display()
    );
    Ok(())
}

fn mark_and_release_stale(
    registry: &Registry,
    record: &SessionRecord,
    message: &str,
) -> Result<()> {
    if let Some(unit) = &record.unit_name {
        let _ = launcher::stop_unit(unit);
    }
    let _ = registry.mark_stale(record.id, message.to_owned());
    registry.release_name_if_matches(&record.name, record.id)?;
    let _ = fs::remove_file(&record.socket);
    Ok(())
}

fn state_label(state: &SessionState) -> &'static str {
    match state {
        SessionState::Starting => "starting",
        SessionState::Running => "running",
        SessionState::Exited => "exited",
        SessionState::Failed => "failed",
    }
}

fn launcher_label(launcher: &LauncherKind) -> &'static str {
    match launcher {
        LauncherKind::Systemd => "systemd",
        LauncherKind::Direct => "direct",
    }
}

fn memory_label(record: &SessionRecord) -> String {
    if record.limits_applied {
        record
            .limits
            .memory_max
            .clone()
            .unwrap_or_else(|| "none".to_owned())
    } else {
        "unprotected".to_owned()
    }
}

fn option_label(value: &Option<String>) -> &str {
    value.as_deref().unwrap_or("none")
}

fn pid_label(pid: Option<u32>) -> String {
    pid.map(|value| value.to_string())
        .unwrap_or_else(|| "none".to_owned())
}
