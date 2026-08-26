use std::fs;
use std::io;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::process::{Command, Stdio};

use anyhow::{Result, bail};

pub fn systemd_user_available() -> bool {
    Command::new("systemctl")
        .args(["--user", "show-environment"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .is_ok_and(|status| status.success())
}

pub fn validate_systemd_value(value: &str, field: &str) -> Result<()> {
    if value.is_empty() || value.len() > 32 {
        bail!("{field} must contain between 1 and 32 characters");
    }
    if !value
        .chars()
        .all(|character| character.is_ascii_alphanumeric() || matches!(character, '.' | '%'))
    {
        bail!(
            "{field} contains unsupported characters; examples: 512M, 12G, 75% or infinity"
        );
    }
    Ok(())
}

pub fn format_age(updated_at_ms: u64, now_ms: u64) -> String {
    let seconds = now_ms.saturating_sub(updated_at_ms) / 1000;
    match seconds {
        0..=59 => format!("{seconds}s"),
        60..=3599 => format!("{}m", seconds / 60),
        3600..=86_399 => format!("{}h", seconds / 3600),
        _ => format!("{}d", seconds / 86_400),
    }
}

pub fn truncate_middle(value: &str, max_chars: usize) -> String {
    if value.chars().count() <= max_chars {
        return value.to_owned();
    }
    if max_chars <= 3 {
        return value.chars().take(max_chars).collect();
    }
    let left = (max_chars - 1) / 2;
    let right = max_chars - left - 1;
    let start: String = value.chars().take(left).collect();
    let end: String = value
        .chars()
        .rev()
        .take(right)
        .collect::<String>()
        .chars()
        .rev()
        .collect();
    format!("{start}…{end}")
}

pub fn set_mode(path: &Path, mode: u32) -> io::Result<()> {
    fs::set_permissions(path, fs::Permissions::from_mode(mode))
}

pub fn path_to_utf8(path: &Path, field: &str) -> Result<String> {
    match path.to_str() {
        Some(value) => Ok(value.to_owned()),
        None => bail!("{field} must be valid UTF-8: {}", path.display()),
    }
}
