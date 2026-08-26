use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::Path;

use anyhow::{Context, Result, anyhow, bail};
use fs2::FileExt;
use uuid::Uuid;

use crate::model::{SCHEMA_VERSION, SessionRecord, SessionState};
use crate::paths::Paths;

#[derive(Clone, Debug)]
pub struct Registry {
    pub paths: Paths,
}

impl Registry {
    pub fn open() -> Result<Self> {
        Ok(Self {
            paths: Paths::discover()?,
        })
    }

    pub fn create_session(&self, record: &SessionRecord) -> Result<()> {
        validate_name(&record.name)?;
        self.with_lock(|| {
            let alias = self.paths.name_path(&record.name);
            if alias.exists() {
                bail!("session name '{}' is already reserved", record.name);
            }

            self.save_record_unlocked(record)?;
            if let Err(error) = write_new_alias(&alias, record.id) {
                let _ = fs::remove_file(self.paths.record_path(record.id));
                return Err(error);
            }
            Ok(())
        })
    }

    pub fn load(&self, id: Uuid) -> Result<SessionRecord> {
        let path = self.paths.record_path(id);
        let bytes = fs::read(&path)
            .with_context(|| format!("session {} is not registered", id.simple()))?;
        let record: SessionRecord = serde_json::from_slice(&bytes)
            .with_context(|| format!("invalid session record {}", path.display()))?;
        if record.schema_version > SCHEMA_VERSION {
            bail!(
                "session record {} uses schema {}, newer than this shardmux supports ({})",
                id.simple(),
                record.schema_version,
                SCHEMA_VERSION
            );
        }
        Ok(record)
    }

    pub fn find_by_name(&self, name: &str) -> Result<Option<SessionRecord>> {
        validate_name(name)?;
        let path = self.paths.name_path(name);
        if !path.exists() {
            return Ok(None);
        }
        let id = read_alias(&path)?;
        match self.load(id) {
            Ok(record) => Ok(Some(record)),
            Err(error) => {
                self.release_name_if_matches(name, id)?;
                Err(error).context("the stale name reservation was removed")
            }
        }
    }

    pub fn resolve(&self, target: &str) -> Result<SessionRecord> {
        if let Ok(Some(record)) = self.find_by_name(target) {
            return Ok(record);
        }
        if let Ok(id) = Uuid::parse_str(target) {
            return self.load(id);
        }
        bail!("unknown session '{target}'")
    }

    pub fn list(&self) -> Result<Vec<SessionRecord>> {
        let mut records = Vec::new();
        for entry in fs::read_dir(&self.paths.sessions_dir).with_context(|| {
            format!(
                "failed to read session directory {}",
                self.paths.sessions_dir.display()
            )
        })? {
            let entry = entry?;
            if entry.path().extension().and_then(|value| value.to_str()) != Some("json") {
                continue;
            }
            match fs::read(entry.path())
                .with_context(|| format!("failed to read {}", entry.path().display()))
                .and_then(|bytes| {
                    serde_json::from_slice::<SessionRecord>(&bytes)
                        .with_context(|| format!("invalid record {}", entry.path().display()))
                }) {
                Ok(record) => records.push(record),
                Err(error) => eprintln!("warning: {error:#}"),
            }
        }
        records.sort_by(|left, right| right.updated_at_ms.cmp(&left.updated_at_ms));
        Ok(records)
    }

    pub fn update<F>(&self, id: Uuid, update: F) -> Result<SessionRecord>
    where
        F: FnOnce(&mut SessionRecord) -> Result<()>,
    {
        self.with_lock(|| {
            let mut record = self.load(id)?;
            update(&mut record)?;
            record.touch();
            self.save_record_unlocked(&record)?;
            Ok(record)
        })
    }

    pub fn rename(&self, id: Uuid, new_name: &str) -> Result<SessionRecord> {
        validate_name(new_name)?;
        self.with_lock(|| {
            let mut record = self.load(id)?;
            if record.name == new_name {
                return Ok(record);
            }

            let new_alias = self.paths.name_path(new_name);
            if new_alias.exists() {
                bail!("session name '{new_name}' is already reserved");
            }
            write_new_alias(&new_alias, id)?;

            let old_name = record.name.clone();
            record.name = new_name.to_owned();
            record.touch();
            if let Err(error) = self.save_record_unlocked(&record) {
                let _ = fs::remove_file(&new_alias);
                return Err(error);
            }
            self.release_name_if_matches_unlocked(&old_name, id)?;
            Ok(record)
        })
    }

    pub fn mark_stale(&self, id: Uuid, message: impl Into<String>) -> Result<SessionRecord> {
        let message = message.into();
        self.update(id, move |record| {
            if matches!(record.state, SessionState::Starting | SessionState::Running) {
                record.state = SessionState::Failed;
            }
            record.message = Some(message);
            Ok(())
        })
    }

    pub fn release_name_if_matches(&self, name: &str, id: Uuid) -> Result<()> {
        validate_name(name)?;
        self.with_lock(|| self.release_name_if_matches_unlocked(name, id))
    }

    pub fn remove_record(&self, id: Uuid) -> Result<()> {
        self.with_lock(|| {
            let record = self.load(id).ok();
            if let Some(record) = record {
                self.release_name_if_matches_unlocked(&record.name, id)?;
                let _ = fs::remove_file(&record.socket);
            }
            match fs::remove_file(self.paths.record_path(id)) {
                Ok(()) => Ok(()),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
                Err(error) => Err(error).context("failed to remove session record"),
            }
        })
    }

    fn release_name_if_matches_unlocked(&self, name: &str, id: Uuid) -> Result<()> {
        let path = self.paths.name_path(name);
        if !path.exists() {
            return Ok(());
        }
        match read_alias(&path) {
            Ok(existing) if existing == id => match fs::remove_file(&path) {
                Ok(()) => Ok(()),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
                Err(error) => Err(error).with_context(|| {
                    format!("failed to remove name reservation {}", path.display())
                }),
            },
            Ok(_) => Ok(()),
            Err(error) => Err(error),
        }
    }

    fn save_record_unlocked(&self, record: &SessionRecord) -> Result<()> {
        let target = self.paths.record_path(record.id);
        let temporary = target.with_extension(format!("json.tmp-{}", std::process::id()));
        let payload = serde_json::to_vec_pretty(record)?;
        let mut file = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .mode(0o600)
            .open(&temporary)
            .with_context(|| format!("failed to open {}", temporary.display()))?;
        file.write_all(&payload)?;
        file.write_all(b"\n")?;
        file.sync_all()?;
        fs::set_permissions(&temporary, fs::Permissions::from_mode(0o600))?;
        fs::rename(&temporary, &target).with_context(|| {
            format!(
                "failed to atomically replace session record {}",
                target.display()
            )
        })?;
        Ok(())
    }

    fn with_lock<T>(&self, operation: impl FnOnce() -> Result<T>) -> Result<T> {
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .mode(0o600)
            .open(&self.paths.lock_file)
            .with_context(|| {
                format!(
                    "failed to open registry lock {}",
                    self.paths.lock_file.display()
                )
            })?;
        lock.lock_exclusive().context("failed to lock registry")?;
        let result = operation();
        FileExt::unlock(&lock).context("failed to unlock registry")?;
        result
    }
}

pub fn validate_name(name: &str) -> Result<()> {
    if name.is_empty() || name.len() > 64 {
        bail!("session names must contain 1 to 64 characters");
    }
    let mut chars = name.chars();
    let first = chars.next().ok_or_else(|| anyhow!("empty session name"))?;
    if !first.is_ascii_alphanumeric() {
        bail!("session names must start with an ASCII letter or digit");
    }
    if !chars.all(|character| {
        character.is_ascii_alphanumeric() || matches!(character, '.' | '_' | '-')
    }) {
        bail!("session names may contain only ASCII letters, digits, '.', '_' and '-'");
    }
    Ok(())
}

fn write_new_alias(path: &Path, id: Uuid) -> Result<()> {
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)
        .with_context(|| format!("failed to reserve name at {}", path.display()))?;
    writeln!(file, "{id}")?;
    file.sync_all()?;
    Ok(())
}

fn read_alias(path: &Path) -> Result<Uuid> {
    let mut content = String::new();
    File::open(path)
        .with_context(|| format!("failed to open name reservation {}", path.display()))?
        .read_to_string(&mut content)?;
    Uuid::parse_str(content.trim())
        .with_context(|| format!("invalid name reservation {}", path.display()))
}

#[cfg(test)]
mod tests {
    use tempfile::TempDir;

    use super::*;
    use crate::model::{LauncherKind, MemoryLimits};

    fn registry() -> (TempDir, Registry) {
        let temp = TempDir::new().unwrap();
        let state_dir = temp.path().join("state");
        let runtime_dir = temp.path().join("run");
        unsafe {
            std::env::set_var("SHARDMUX_STATE_DIR", &state_dir);
            std::env::set_var("SHARDMUX_RUNTIME_DIR", &runtime_dir);
        }
        let registry = Registry::open().unwrap();
        (temp, registry)
    }

    fn record(registry: &Registry, name: &str) -> SessionRecord {
        let id = Uuid::new_v4();
        SessionRecord::new(
            id,
            name.to_owned(),
            std::env::current_dir().unwrap(),
            vec!["/bin/sh".to_owned()],
            registry.paths.socket_path(id).unwrap(),
            None,
            LauncherKind::Direct,
            MemoryLimits::disabled(),
            false,
            1024,
        )
    }

    #[test]
    fn creates_resolves_and_renames_session() {
        let (_temp, registry) = registry();
        let record = record(&registry, "alpha");
        registry.create_session(&record).unwrap();
        assert_eq!(registry.resolve("alpha").unwrap().id, record.id);

        registry.rename(record.id, "beta").unwrap();
        assert!(registry.find_by_name("alpha").unwrap().is_none());
        assert_eq!(registry.resolve("beta").unwrap().id, record.id);
    }

    #[test]
    fn validates_names() {
        assert!(validate_name("alpha-1.test").is_ok());
        assert!(validate_name("-alpha").is_err());
        assert!(validate_name("alpha/beta").is_err());
    }
}
