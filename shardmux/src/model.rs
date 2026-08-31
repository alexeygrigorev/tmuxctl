use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use uuid::Uuid;

pub const SCHEMA_VERSION: u32 = 1;
pub const DEFAULT_MEMORY_MAX: &str = "12G";
pub const DEFAULT_MEMORY_SWAP_MAX: &str = "8G";
pub const DEFAULT_SCROLLBACK_BYTES: usize = 1024 * 1024;

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SessionState {
    Starting,
    Running,
    Exited,
    Failed,
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum LauncherKind {
    Systemd,
    Direct,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct MemoryLimits {
    pub memory_max: Option<String>,
    pub memory_swap_max: Option<String>,
}

impl MemoryLimits {
    pub fn disabled() -> Self {
        Self {
            memory_max: None,
            memory_swap_max: None,
        }
    }

    pub fn enabled(memory_max: String, memory_swap_max: String) -> Self {
        Self {
            memory_max: Some(memory_max),
            memory_swap_max: Some(memory_swap_max),
        }
    }

    pub fn is_enabled(&self) -> bool {
        self.memory_max.is_some()
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SessionRecord {
    pub schema_version: u32,
    pub id: Uuid,
    pub name: String,
    pub cwd: PathBuf,
    pub command: Vec<String>,
    pub socket: PathBuf,
    pub unit_name: Option<String>,
    pub launcher: LauncherKind,
    pub limits: MemoryLimits,
    pub limits_applied: bool,
    pub scrollback_bytes: usize,
    pub state: SessionState,
    pub server_pid: Option<u32>,
    pub child_pid: Option<u32>,
    pub exit_code: Option<u32>,
    pub exit_signal: Option<String>,
    pub message: Option<String>,
    pub created_at_ms: u64,
    pub updated_at_ms: u64,
}

impl SessionRecord {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        id: Uuid,
        name: String,
        cwd: PathBuf,
        command: Vec<String>,
        socket: PathBuf,
        unit_name: Option<String>,
        launcher: LauncherKind,
        limits: MemoryLimits,
        limits_applied: bool,
        scrollback_bytes: usize,
    ) -> Self {
        let now = now_ms();
        Self {
            schema_version: SCHEMA_VERSION,
            id,
            name,
            cwd,
            command,
            socket,
            unit_name,
            launcher,
            limits,
            limits_applied,
            scrollback_bytes,
            state: SessionState::Starting,
            server_pid: None,
            child_pid: None,
            exit_code: None,
            exit_signal: None,
            message: None,
            created_at_ms: now,
            updated_at_ms: now,
        }
    }

    pub fn touch(&mut self) {
        self.updated_at_ms = now_ms();
    }
}

pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
        .try_into()
        .unwrap_or(u64::MAX)
}
