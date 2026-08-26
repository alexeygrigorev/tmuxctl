use std::path::PathBuf;

use clap::{Args, Parser, Subcommand};
use uuid::Uuid;

use crate::model::DEFAULT_SCROLLBACK_BYTES;

#[derive(Debug, Parser)]
#[command(
    name = "shardmux",
    version,
    about = "Failure-isolated terminal sessions",
    long_about = "A Linux-first terminal session manager with one PTY server and one cgroup per session. No global multiplexer daemon means one session server cannot take every other session down."
)]
pub struct Cli {
    #[command(subcommand)]
    pub command: Option<Commands>,
}

#[derive(Debug, Subcommand)]
pub enum Commands {
    /// Create a session if needed, then attach to it.
    #[command(visible_alias = "n")]
    New(SessionArgs),

    /// Create a detached session and print its name.
    #[command(visible_alias = "c")]
    Create(SessionArgs),

    /// Attach to a running session. Press Ctrl-b d to detach.
    #[command(visible_alias = "a")]
    Attach(TargetArgs),

    /// List registered sessions, newest first.
    #[command(visible_alias = "ls")]
    List {
        /// Emit machine-readable JSON.
        #[arg(long)]
        json: bool,
    },

    /// Show one session's persisted and live state.
    Status {
        /// Session name or immutable UUID.
        target: String,
        /// Emit machine-readable JSON.
        #[arg(long)]
        json: bool,
    },

    /// Write text to a session's PTY.
    Send {
        /// Session name or immutable UUID.
        target: String,
        /// Text to send.
        #[arg(short = 'm', long)]
        message: String,
        /// Do not append Enter (carriage return).
        #[arg(long)]
        no_enter: bool,
    },

    /// Stop one session without touching any other session.
    #[command(visible_alias = "k")]
    Kill(TargetArgs),

    /// Change a logical name without changing the socket or systemd unit identity.
    Rename {
        /// Session name or immutable UUID.
        target: String,
        /// New logical name.
        new_name: String,
    },

    /// Remove records and stale sockets for sessions that are no longer alive.
    Prune,

    /// Check systemd, storage paths, memory isolation and stale records.
    Doctor,

    /// Run one per-session PTY server. This is launched internally.
    #[command(hide = true)]
    Serve {
        #[arg(long)]
        id: Uuid,
    },
}

#[derive(Clone, Debug, Args)]
pub struct SessionArgs {
    /// Logical session name (1-64 safe ASCII characters).
    pub name: String,

    /// Working directory for the command or shell.
    #[arg(short = 'c', long, default_value = ".")]
    pub cwd: PathBuf,

    /// Hard cgroup memory ceiling, such as 12G.
    #[arg(long, env = "SHARDMUX_MEMORY_MAX")]
    pub memory_max: Option<String>,

    /// Hard cgroup swap ceiling, such as 8G or 0.
    #[arg(long, env = "SHARDMUX_MEMORY_SWAP_MAX")]
    pub memory_swap_max: Option<String>,

    /// Create a per-session systemd service without memory ceilings.
    #[arg(long)]
    pub no_limit: bool,

    /// Bypass systemd. Requires --no-limit and is intentionally not OOM-contained.
    #[arg(long)]
    pub direct: bool,

    /// Raw output bytes retained for reattach.
    #[arg(long, default_value_t = DEFAULT_SCROLLBACK_BYTES)]
    pub scrollback_bytes: usize,

    /// Command and arguments. Omit to start $SHELL -l.
    #[arg(last = true)]
    pub command: Vec<String>,
}

#[derive(Debug, Args)]
pub struct TargetArgs {
    /// Session name or immutable UUID.
    pub target: String,
}
