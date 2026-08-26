use std::fs::OpenOptions;
use std::io;
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::process::CommandExt;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use anyhow::{Context, Result, bail};

use crate::client;
use crate::model::{LauncherKind, SessionRecord};
use crate::registry::Registry;
use crate::util::{path_to_utf8, systemd_user_available, validate_systemd_value};

const START_TIMEOUT: Duration = Duration::from_secs(8);

pub fn launch(registry: &Registry, record: &SessionRecord) -> Result<()> {
    match record.launcher {
        LauncherKind::Systemd => launch_systemd(registry, record)?,
        LauncherKind::Direct => launch_direct(registry, record)?,
    }
    wait_until_ready(record).map_err(|error| {
        let diagnostics = diagnostics(registry, record);
        error.context(diagnostics)
    })
}

fn launch_systemd(registry: &Registry, record: &SessionRecord) -> Result<()> {
    if !systemd_user_available() {
        bail!(
            "the systemd user manager is unavailable; shardmux will not silently drop OOM \
             isolation. Start a user manager/enable lingering, or use --direct --no-limit \
             for an explicitly unprotected development session"
        );
    }

    let unit = record
        .unit_name
        .as_deref()
        .context("systemd session is missing a unit name")?;
    if let Some(value) = &record.limits.memory_max {
        validate_systemd_value(value, "--memory-max")?;
    }
    if let Some(value) = &record.limits.memory_swap_max {
        validate_systemd_value(value, "--memory-swap-max")?;
    }

    let executable = std::env::current_exe().context("failed to locate shardmux executable")?;
    let state_dir = path_to_utf8(&registry.paths.state_dir, "state directory")?;
    let runtime_dir = path_to_utf8(&registry.paths.runtime_dir, "runtime directory")?;

    let mut command = Command::new("systemd-run");
    command
        .arg("--user")
        .arg("--quiet")
        .arg("--collect")
        .arg(format!("--unit={unit}"))
        .arg("--service-type=exec")
        .arg("--property=KillMode=control-group")
        .arg("--property=MemoryOOMGroup=yes")
        .arg("--property=OOMPolicy=kill")
        .arg("--property=Restart=no")
        .arg("--property=TimeoutStopSec=5s")
        .arg(format!("--setenv=SHARDMUX_STATE_DIR={state_dir}"))
        .arg(format!("--setenv=SHARDMUX_RUNTIME_DIR={runtime_dir}"));

    if let Some(value) = &record.limits.memory_max {
        command.arg(format!("--property=MemoryMax={value}"));
    }
    if let Some(value) = &record.limits.memory_swap_max {
        command.arg(format!("--property=MemorySwapMax={value}"));
    }

    let output = command
        .arg("--")
        .arg(executable)
        .arg("serve")
        .arg("--id")
        .arg(record.id.to_string())
        .stdin(Stdio::null())
        .output()
        .context("failed to run systemd-run")?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_owned();
        bail!(
            "systemd-run failed for {unit}: {}",
            if stderr.is_empty() {
                output.status.to_string()
            } else {
                stderr
            }
        );
    }
    Ok(())
}

fn launch_direct(registry: &Registry, record: &SessionRecord) -> Result<()> {
    if record.limits.is_enabled() {
        bail!(
            "--direct cannot enforce memory limits; add --no-limit to acknowledge that this \
             development session is not OOM-contained"
        );
    }

    let executable = std::env::current_exe().context("failed to locate shardmux executable")?;
    let log_path = registry.paths.log_path(record.id);
    let log = OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .open(&log_path)
        .with_context(|| format!("failed to open direct-launch log {}", log_path.display()))?;
    let stderr = log.try_clone()?;

    let mut command = Command::new(executable);
    command
        .arg("serve")
        .arg("--id")
        .arg(record.id.to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::from(log))
        .stderr(Stdio::from(stderr));

    unsafe {
        command.pre_exec(|| {
            if libc::setsid() == -1 {
                return Err(io::Error::last_os_error());
            }
            Ok(())
        });
    }

    let child = command
        .spawn()
        .context("failed to launch shardmux server")?;
    let pid = child.id();
    drop(child);
    registry.update(record.id, |stored| {
        stored.server_pid = Some(pid);
        Ok(())
    })?;
    Ok(())
}

fn wait_until_ready(record: &SessionRecord) -> Result<()> {
    let deadline = Instant::now() + START_TIMEOUT;
    let mut last_error = None;
    while Instant::now() < deadline {
        match client::ping(record, Duration::from_millis(250)) {
            Ok(()) => return Ok(()),
            Err(error) => last_error = Some(error),
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    let detail = last_error
        .map(|error| format!(": {error:#}"))
        .unwrap_or_default();
    bail!("session server did not become ready within 8 seconds{detail}")
}

pub fn stop_unit(unit: &str) -> Result<()> {
    let status = Command::new("systemctl")
        .args(["--user", "stop", unit])
        .stdin(Stdio::null())
        .status()
        .with_context(|| format!("failed to stop {unit}"))?;
    if !status.success() {
        bail!("systemctl failed while stopping {unit}: {status}");
    }
    Ok(())
}

fn diagnostics(registry: &Registry, record: &SessionRecord) -> String {
    match record.launcher {
        LauncherKind::Systemd => record
            .unit_name
            .as_deref()
            .map(unit_diagnostics)
            .unwrap_or_else(|| "no systemd unit name was recorded".to_owned()),
        LauncherKind::Direct => {
            let path = registry.paths.log_path(record.id);
            match std::fs::read_to_string(&path) {
                Ok(content) => {
                    let tail: Vec<&str> = content.lines().rev().take(20).collect();
                    format!(
                        "direct server log {}:\n{}",
                        path.display(),
                        tail.into_iter().rev().collect::<Vec<_>>().join("\n")
                    )
                }
                Err(error) => format!(
                    "could not read direct server log {}: {error}",
                    path.display()
                ),
            }
        }
    }
}

fn unit_diagnostics(unit: &str) -> String {
    let output = Command::new("journalctl")
        .args(["--user", "--unit", unit, "--lines", "20", "--no-pager"])
        .output();
    match output {
        Ok(output) if !output.stdout.is_empty() => format!(
            "recent journal for {unit}:\n{}",
            String::from_utf8_lossy(&output.stdout).trim()
        ),
        Ok(_) => format!("no journal output was available for {unit}"),
        Err(error) => format!("could not read the journal for {unit}: {error}"),
    }
}
