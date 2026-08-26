use std::env;
use std::ffi::OsStr;
use std::fs;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use uuid::Uuid;

#[derive(Clone, Debug)]
pub struct Paths {
    pub state_dir: PathBuf,
    pub runtime_dir: PathBuf,
    pub sessions_dir: PathBuf,
    pub names_dir: PathBuf,
    pub logs_dir: PathBuf,
    pub lock_file: PathBuf,
}

impl Paths {
    pub fn discover() -> Result<Self> {
        let state_dir = if let Some(value) = env::var_os("SHARDMUX_STATE_DIR") {
            PathBuf::from(value)
        } else if let Some(value) = env::var_os("XDG_STATE_HOME") {
            PathBuf::from(value).join("shardmux")
        } else {
            home_dir()?.join(".local/state/shardmux")
        };

        let runtime_dir = if let Some(value) = env::var_os("SHARDMUX_RUNTIME_DIR") {
            PathBuf::from(value)
        } else if let Some(value) = env::var_os("XDG_RUNTIME_DIR") {
            PathBuf::from(value).join("shardmux")
        } else {
            let uid = unsafe { libc::geteuid() };
            PathBuf::from(format!("/tmp/shardmux-{uid}"))
        };

        let paths = Self {
            sessions_dir: state_dir.join("sessions"),
            names_dir: state_dir.join("names"),
            logs_dir: state_dir.join("logs"),
            lock_file: state_dir.join("registry.lock"),
            state_dir,
            runtime_dir,
        };
        paths.ensure()?;
        Ok(paths)
    }

    pub fn ensure(&self) -> Result<()> {
        for path in [
            &self.state_dir,
            &self.runtime_dir,
            &self.sessions_dir,
            &self.names_dir,
            &self.logs_dir,
        ] {
            create_private_dir(path)?;
        }
        Ok(())
    }

    pub fn record_path(&self, id: Uuid) -> PathBuf {
        self.sessions_dir.join(format!("{}.json", id.simple()))
    }

    pub fn name_path(&self, name: &str) -> PathBuf {
        self.names_dir.join(name)
    }

    pub fn socket_path(&self, id: Uuid) -> Result<PathBuf> {
        let path = self.runtime_dir.join(format!("{}.sock", id.simple()));
        ensure_socket_path_fits(&path)?;
        Ok(path)
    }

    pub fn log_path(&self, id: Uuid) -> PathBuf {
        self.logs_dir.join(format!("{}.log", id.simple()))
    }
}

fn home_dir() -> Result<PathBuf> {
    env::var_os("HOME")
        .map(PathBuf::from)
        .context("HOME is not set; set SHARDMUX_STATE_DIR explicitly")
}

fn create_private_dir(path: &Path) -> Result<()> {
    fs::create_dir_all(path)
        .with_context(|| format!("failed to create directory {}", path.display()))?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))
        .with_context(|| format!("failed to set mode 0700 on {}", path.display()))?;
    Ok(())
}

fn ensure_socket_path_fits(path: &Path) -> Result<()> {
    const MAX_UNIX_SOCKET_PATH: usize = 107;
    let length = os_len(path.as_os_str());
    if length > MAX_UNIX_SOCKET_PATH {
        bail!(
            "runtime socket path is {length} bytes, but Linux Unix sockets allow at most \
             {MAX_UNIX_SOCKET_PATH}; set SHARDMUX_RUNTIME_DIR to a shorter path"
        );
    }
    Ok(())
}

fn os_len(value: &OsStr) -> usize {
    value.as_bytes().len()
}
