//! Versioned runtime cache ownership, locking, and atomic promotion.

use std::ffi::OsStr;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use fs2::FileExt;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::error::LauncherError;

/// Schema segment included in every managed runtime path.
pub const CACHE_SCHEMA_VERSION: u32 = 1;
static STAGING_COUNTER: AtomicU64 = AtomicU64::new(0);

/// Environment inputs used to choose one platform cache root.
#[derive(Debug, Clone)]
pub struct CacheEnvironment {
    /// Node-style platform identifier.
    pub platform: &'static str,
    /// Absolute user home directory when available.
    pub home: Option<PathBuf>,
    /// Absolute Linux XDG cache root when configured.
    pub xdg_cache_home: Option<PathBuf>,
    /// Explicit absolute Aizim cache root override.
    pub override_root: Option<PathBuf>,
}

impl CacheEnvironment {
    /// Compute and lexically normalize the selected absolute cache root.
    pub fn root_path(&self) -> Result<PathBuf, LauncherError> {
        if let Some(root) = &self.override_root {
            return normalize_absolute(root);
        }
        match self.platform {
            "darwin" => {
                let home = self
                    .home
                    .as_deref()
                    .ok_or_else(|| LauncherError::configuration("CACHE_ROOT_INVALID"))?;
                normalize_absolute(&home.join("Library").join("Caches").join("aizim"))
            }
            "linux" => {
                if let Some(root) = &self.xdg_cache_home {
                    return normalize_absolute(&root.join("aizim"));
                }
                let home = self
                    .home
                    .as_deref()
                    .ok_or_else(|| LauncherError::configuration("CACHE_ROOT_INVALID"))?;
                normalize_absolute(&home.join(".cache").join("aizim"))
            }
            _ => Err(LauncherError::configuration("CACHE_ROOT_INVALID")),
        }
    }
}

/// Identity inputs that select one immutable managed runtime.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CacheKey {
    /// Aizim package version.
    pub aizim_version: String,
    /// Closed native target identifier.
    pub target: String,
    /// Verified wheel digest.
    pub wheel_sha256: String,
    /// Managed `CPython` minor version.
    pub python_version: String,
}

impl CacheKey {
    /// Return a path-free digest suitable for bounded status output.
    pub fn fingerprint(&self, schema_version: u32) -> String {
        let mut hasher = Sha256::new();
        for value in [
            schema_version.to_string(),
            self.aizim_version.clone(),
            self.target.clone(),
            self.wheel_sha256.clone(),
            self.python_version.clone(),
        ] {
            hasher.update(value.as_bytes());
            hasher.update([0]);
        }
        encode_digest(&hasher.finalize())
    }
}

/// Canonical directories and lock path for one runtime identity.
#[derive(Debug, Clone)]
pub struct CacheLayout {
    /// Canonical Aizim cache root.
    pub root: PathBuf,
    /// Shared managed Python installation root.
    pub managed_python_root: PathBuf,
    /// Shared uv download and build cache root.
    pub uv_cache_root: PathBuf,
    /// Final immutable runtime directory.
    pub runtime_root: PathBuf,
    /// OS-backed advisory lock file beside the runtime directory.
    pub lock_path: PathBuf,
    schema_version: u32,
}

impl CacheLayout {
    /// Create the current-schema layout and all managed parent directories.
    pub fn new(environment: &CacheEnvironment, key: &CacheKey) -> Result<Self, LauncherError> {
        Self::with_schema(environment, key, CACHE_SCHEMA_VERSION)
    }

    /// Create a layout for an explicit schema version.
    pub fn with_schema(
        environment: &CacheEnvironment,
        key: &CacheKey,
        schema_version: u32,
    ) -> Result<Self, LauncherError> {
        validate_key(key)?;
        if schema_version == 0 {
            return Err(LauncherError::configuration("CACHE_ROOT_INVALID"));
        }
        let root = environment.root_path()?;
        ensure_directory_tree(&root)?;
        let managed_python_root = root.join("python");
        let uv_cache_root = root.join("uv");
        ensure_directory_tree(&managed_python_root)?;
        ensure_directory_tree(&uv_cache_root)?;
        let runtime_root = root
            .join("runtime")
            .join(format!("v{schema_version}"))
            .join(&key.aizim_version)
            .join(&key.target)
            .join(&key.wheel_sha256)
            .join(format!("py{}", key.python_version));
        let runtime_parent = runtime_root
            .parent()
            .ok_or_else(|| LauncherError::internal("CACHE_LAYOUT_INVALID"))?;
        ensure_directory_tree(runtime_parent)?;
        let leaf = runtime_root
            .file_name()
            .ok_or_else(|| LauncherError::internal("CACHE_LAYOUT_INVALID"))?;
        let lock_path = runtime_parent.join(format!(".{}.lock", leaf.to_string_lossy()));
        Ok(Self {
            root,
            managed_python_root,
            uv_cache_root,
            runtime_root,
            lock_path,
            schema_version,
        })
    }

    /// Create one private same-parent staging directory.
    pub fn create_staging(&self) -> Result<PathBuf, LauncherError> {
        let parent = self.runtime_parent()?;
        let counter = STAGING_COUNTER.fetch_add(1, Ordering::Relaxed);
        let staging = parent.join(format!(
            "{}{}-{counter}",
            self.staging_prefix()?,
            std::process::id()
        ));
        fs::create_dir(&staging).map_err(cache_integrity)?;
        set_private_directory(&staging)?;
        Ok(staging)
    }

    /// Remove only stale staging entries for this exact runtime leaf.
    pub fn cleanup_staging(&self) -> Result<(), LauncherError> {
        let parent = self.runtime_parent()?;
        let prefix = self.staging_prefix()?;
        for entry in fs::read_dir(parent).map_err(cache_integrity)? {
            let entry = entry.map_err(cache_integrity)?;
            let name = entry.file_name();
            if !name.to_string_lossy().starts_with(&prefix) {
                continue;
            }
            remove_entry(&entry.path())?;
        }
        Ok(())
    }

    /// Atomically write a strict ready marker inside a staging runtime.
    pub fn write_ready(&self, staging: &Path, marker: &ReadyMarker) -> Result<(), LauncherError> {
        self.validate_staging(staging)?;
        validate_entrypoint(&marker.aizim_entrypoint)?;
        validate_entrypoint(&marker.sidecar_entrypoint)?;
        if marker.schema_version != self.schema_version {
            return Err(LauncherError::integrity("CACHE_LAYOUT_INVALID"));
        }
        let temporary = staging.join("READY.json.tmp");
        let final_path = staging.join("READY.json");
        let bytes = serde_json::to_vec(marker).map_err(cache_integrity)?;
        let mut file = open_private_new(&temporary)?;
        file.write_all(&bytes).map_err(cache_integrity)?;
        file.sync_all().map_err(cache_integrity)?;
        fs::rename(&temporary, &final_path).map_err(cache_integrity)?;
        sync_directory(staging)
    }

    /// Return the exact reusable runtime when its marker and entry points match.
    pub fn ready_runtime(&self, key: &CacheKey) -> Result<Option<ReadyRuntime>, LauncherError> {
        let root_metadata = match fs::symlink_metadata(&self.runtime_root) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(cache_integrity(error)),
        };
        if !root_metadata.file_type().is_dir() {
            return Ok(None);
        }
        let marker_path = self.runtime_root.join("READY.json");
        let marker_metadata = match fs::symlink_metadata(&marker_path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(cache_integrity(error)),
        };
        if !marker_metadata.file_type().is_file() {
            return Ok(None);
        }
        let bytes = fs::read(&marker_path).map_err(cache_integrity)?;
        let marker: ReadyMarker = match serde_json::from_slice(&bytes) {
            Ok(marker) => marker,
            Err(_) => return Ok(None),
        };
        if marker.schema_version != self.schema_version
            || marker.aizim_version != key.aizim_version
            || marker.target != key.target
            || marker.wheel_sha256 != key.wheel_sha256
            || marker.python_version != key.python_version
        {
            return Ok(None);
        }
        let Some(aizim) = ready_executable(&self.runtime_root, &marker.aizim_entrypoint)? else {
            return Ok(None);
        };
        let Some(sidecar) = ready_executable(&self.runtime_root, &marker.sidecar_entrypoint)?
        else {
            return Ok(None);
        };
        Ok(Some(ReadyRuntime { aizim, sidecar }))
    }

    /// Promote one valid same-parent staging directory without overwriting final state.
    pub fn promote(&self, staging: &Path, key: &CacheKey) -> Result<ReadyRuntime, LauncherError> {
        self.validate_staging(staging)?;
        if self.runtime_root.exists() {
            if let Some(ready) = self.ready_runtime(key)? {
                remove_entry(staging)?;
                return Ok(ready);
            }
            return Err(LauncherError::integrity("CACHE_LAYOUT_INVALID"));
        }
        fs::rename(staging, &self.runtime_root).map_err(cache_integrity)?;
        sync_directory(self.runtime_parent()?)?;
        self.ready_runtime(key)?
            .ok_or_else(|| LauncherError::integrity("CACHE_LAYOUT_INVALID"))
    }

    fn runtime_parent(&self) -> Result<&Path, LauncherError> {
        self.runtime_root
            .parent()
            .ok_or_else(|| LauncherError::internal("CACHE_LAYOUT_INVALID"))
    }

    fn staging_prefix(&self) -> Result<String, LauncherError> {
        let leaf = self
            .runtime_root
            .file_name()
            .and_then(OsStr::to_str)
            .ok_or_else(|| LauncherError::internal("CACHE_LAYOUT_INVALID"))?;
        Ok(format!(".{leaf}.staging-"))
    }

    fn validate_staging(&self, staging: &Path) -> Result<(), LauncherError> {
        let prefix = self.staging_prefix()?;
        if staging.parent() != Some(self.runtime_parent()?)
            || !staging
                .file_name()
                .and_then(OsStr::to_str)
                .is_some_and(|name| name.starts_with(&prefix))
        {
            return Err(LauncherError::integrity("CACHE_LAYOUT_INVALID"));
        }
        let metadata = fs::symlink_metadata(staging).map_err(cache_integrity)?;
        if !metadata.file_type().is_dir() {
            return Err(LauncherError::integrity("CACHE_LAYOUT_INVALID"));
        }
        Ok(())
    }
}

/// Strict completion marker written only after successful provisioning.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReadyMarker {
    /// Cache schema version.
    pub schema_version: u32,
    /// Aizim package version.
    pub aizim_version: String,
    /// Closed native target identifier.
    pub target: String,
    /// Verified wheel digest.
    pub wheel_sha256: String,
    /// Managed `CPython` minor version.
    pub python_version: String,
    /// Runtime-relative Aizim console entry point.
    pub aizim_entrypoint: String,
    /// Runtime-relative gateway-sidecar console entry point.
    pub sidecar_entrypoint: String,
}

/// Canonical executable paths from one reusable ready runtime.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReadyRuntime {
    /// Canonical Aizim console entry point.
    pub aizim: PathBuf,
    /// Canonical gateway-sidecar console entry point.
    pub sidecar: PathBuf,
}

/// Held exclusive advisory lock for one runtime identity.
#[derive(Debug)]
pub struct RuntimeLock {
    file: File,
}

impl RuntimeLock {
    /// Open and exclusively lock the persistent lock file.
    pub fn acquire(path: &Path) -> Result<Self, LauncherError> {
        use std::os::unix::fs::OpenOptionsExt;

        let file = OpenOptions::new()
            .create(true)
            .read(true)
            .truncate(false)
            .write(true)
            .mode(0o600)
            .open(path)
            .map_err(cache_integrity)?;
        file.lock_exclusive().map_err(cache_integrity)?;
        Ok(Self { file })
    }
}

impl Drop for RuntimeLock {
    fn drop(&mut self) {
        drop(FileExt::unlock(&self.file));
    }
}

fn validate_key(key: &CacheKey) -> Result<(), LauncherError> {
    for value in [
        &key.aizim_version,
        &key.target,
        &key.python_version,
        &key.wheel_sha256,
    ] {
        if !safe_component(value) {
            return Err(LauncherError::configuration("CACHE_ROOT_INVALID"));
        }
    }
    if key.wheel_sha256.len() != 64
        || !key
            .wheel_sha256
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(LauncherError::configuration("CACHE_ROOT_INVALID"));
    }
    Ok(())
}

fn safe_component(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 80
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

fn normalize_absolute(path: &Path) -> Result<PathBuf, LauncherError> {
    if !path.is_absolute() {
        return Err(LauncherError::configuration("CACHE_ROOT_INVALID"));
    }
    let mut normalized = PathBuf::new();
    for component in path.components() {
        match component {
            Component::Prefix(prefix) => normalized.push(prefix.as_os_str()),
            Component::RootDir => normalized.push(Path::new("/")),
            Component::CurDir => {}
            Component::Normal(value) => normalized.push(value),
            Component::ParentDir if normalized.pop() => {}
            Component::ParentDir => {
                return Err(LauncherError::configuration("CACHE_ROOT_INVALID"));
            }
        }
    }
    Ok(normalized)
}

fn ensure_directory_tree(path: &Path) -> Result<(), LauncherError> {
    use std::os::unix::fs::PermissionsExt;

    let mut current = PathBuf::new();
    for component in path.components() {
        match component {
            Component::Prefix(prefix) => current.push(prefix.as_os_str()),
            Component::RootDir => current.push(Path::new("/")),
            Component::Normal(value) => {
                current.push(value);
                match fs::symlink_metadata(&current) {
                    Ok(metadata) if metadata.file_type().is_dir() => {}
                    Ok(_) => return Err(LauncherError::integrity("CACHE_LAYOUT_INVALID")),
                    Err(error) if error.kind() == io::ErrorKind::NotFound => {
                        fs::create_dir(&current).map_err(cache_integrity)?;
                        let mut permissions = fs::metadata(&current)
                            .map_err(cache_integrity)?
                            .permissions();
                        permissions.set_mode(0o700);
                        fs::set_permissions(&current, permissions).map_err(cache_integrity)?;
                    }
                    Err(error) => return Err(cache_integrity(error)),
                }
            }
            Component::CurDir | Component::ParentDir => {
                return Err(LauncherError::configuration("CACHE_ROOT_INVALID"));
            }
        }
    }
    Ok(())
}

fn set_private_directory(path: &Path) -> Result<(), LauncherError> {
    use std::os::unix::fs::PermissionsExt;

    let mut permissions = fs::metadata(path).map_err(cache_integrity)?.permissions();
    permissions.set_mode(0o700);
    fs::set_permissions(path, permissions).map_err(cache_integrity)
}

fn open_private_new(path: &Path) -> Result<File, LauncherError> {
    use std::os::unix::fs::OpenOptionsExt;

    OpenOptions::new()
        .create_new(true)
        .write(true)
        .mode(0o600)
        .open(path)
        .map_err(cache_integrity)
}

fn sync_directory(path: &Path) -> Result<(), LauncherError> {
    File::open(path)
        .and_then(|directory| directory.sync_all())
        .map_err(cache_integrity)
}

fn validate_entrypoint(value: &str) -> Result<(), LauncherError> {
    let path = Path::new(value);
    if value.is_empty()
        || path.is_absolute()
        || !path
            .components()
            .all(|component| matches!(component, Component::Normal(_)))
    {
        return Err(LauncherError::integrity("CACHE_LAYOUT_INVALID"));
    }
    Ok(())
}

fn ready_executable(root: &Path, relative: &str) -> Result<Option<PathBuf>, LauncherError> {
    use std::os::unix::fs::PermissionsExt;

    if validate_entrypoint(relative).is_err() {
        return Ok(None);
    }
    let root = fs::canonicalize(root).map_err(cache_integrity)?;
    let candidate = root.join(relative);
    let metadata = match fs::symlink_metadata(&candidate) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(cache_integrity(error)),
    };
    if !metadata.file_type().is_file() || metadata.permissions().mode() & 0o111 == 0 {
        return Ok(None);
    }
    let canonical = fs::canonicalize(candidate).map_err(cache_integrity)?;
    if !canonical.starts_with(&root) {
        return Ok(None);
    }
    Ok(Some(canonical))
}

fn remove_entry(path: &Path) -> Result<(), LauncherError> {
    let metadata = fs::symlink_metadata(path).map_err(cache_integrity)?;
    if metadata.file_type().is_dir() {
        fs::remove_dir_all(path).map_err(cache_integrity)
    } else {
        fs::remove_file(path).map_err(cache_integrity)
    }
}

fn encode_digest(digest: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";

    let mut encoded = String::with_capacity(digest.len() * 2);
    for byte in digest {
        let high = usize::from(byte >> 4);
        let low = usize::from(byte & 0x0f);
        if let (Some(high), Some(low)) = (HEX.get(high), HEX.get(low)) {
            encoded.push(char::from(*high));
            encoded.push(char::from(*low));
        }
    }
    encoded
}

fn cache_integrity(source: impl std::error::Error + Send + Sync + 'static) -> LauncherError {
    LauncherError::integrity("CACHE_LAYOUT_INVALID").with_source(source)
}
