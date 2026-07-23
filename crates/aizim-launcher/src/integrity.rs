use std::fmt::Write as _;
use std::fs::{self, File};
use std::io::Read;
use std::path::{Component, Path, PathBuf};

use sha2::{Digest, Sha256};

use crate::error::LauncherError;
use crate::manifest::Artifact;

/// Verify one package-relative artifact without crossing package roots.
pub fn verify_artifact(
    package_root: &Path,
    other_package_root: &Path,
    artifact: &Artifact,
) -> Result<PathBuf, LauncherError> {
    validate_relative(&artifact.path)?;
    validate_digest(&artifact.sha256)?;
    let candidate = package_root.join(&artifact.path);
    let metadata = fs::symlink_metadata(&candidate).map_err(|source| {
        LauncherError::integrity("ARTIFACT_INTEGRITY_FAILED").with_source(source)
    })?;
    if !metadata.file_type().is_file() {
        return Err(LauncherError::integrity("ARTIFACT_INTEGRITY_FAILED"));
    }
    let canonical = fs::canonicalize(&candidate).map_err(|source| {
        LauncherError::integrity("ARTIFACT_INTEGRITY_FAILED").with_source(source)
    })?;
    if !canonical.starts_with(package_root) || canonical.starts_with(other_package_root) {
        return Err(LauncherError::integrity("ARTIFACT_INTEGRITY_FAILED"));
    }
    let canonical_metadata = fs::metadata(&canonical).map_err(|source| {
        LauncherError::integrity("ARTIFACT_INTEGRITY_FAILED").with_source(source)
    })?;
    if !canonical_metadata.is_file() || canonical_metadata.len() != artifact.size {
        return Err(LauncherError::integrity("ARTIFACT_INTEGRITY_FAILED"));
    }
    let digest = sha256_file(&canonical)?;
    if digest != artifact.sha256 {
        return Err(LauncherError::integrity("ARTIFACT_INTEGRITY_FAILED"));
    }
    Ok(canonical)
}

/// Verify and canonicalize one absolute executable file.
pub fn verify_executable(path: &Path) -> Result<PathBuf, LauncherError> {
    use std::os::unix::fs::PermissionsExt;

    if !path.is_absolute() {
        return Err(LauncherError::configuration("ARGUMENTS_INVALID"));
    }
    let metadata = fs::symlink_metadata(path)
        .map_err(|source| LauncherError::integrity("CODEX_INTEGRITY_FAILED").with_source(source))?;
    if !metadata.file_type().is_file() || metadata.permissions().mode() & 0o111 == 0 {
        return Err(LauncherError::integrity("CODEX_INTEGRITY_FAILED"));
    }
    fs::canonicalize(path)
        .map_err(|source| LauncherError::integrity("CODEX_INTEGRITY_FAILED").with_source(source))
}

/// Stream a file and return its lowercase SHA-256 digest.
pub fn sha256_file(path: &Path) -> Result<String, LauncherError> {
    let mut file = File::open(path)
        .map_err(|source| LauncherError::integrity("MANIFEST_IO_FAILED").with_source(source))?;
    let mut hasher = Sha256::new();
    let mut buffer = vec![0_u8; 64 * 1024].into_boxed_slice();
    loop {
        let read = file
            .read(&mut buffer)
            .map_err(|source| LauncherError::integrity("MANIFEST_IO_FAILED").with_source(source))?;
        if read == 0 {
            break;
        }
        hasher.update(
            buffer
                .get(..read)
                .ok_or_else(|| LauncherError::internal("HASH_BUFFER_INVALID"))?,
        );
    }
    let digest = hasher.finalize();
    let mut encoded = String::with_capacity(64);
    for byte in digest {
        write!(&mut encoded, "{byte:02x}")
            .map_err(|_| LauncherError::internal("DIGEST_ENCODING_FAILED"))?;
    }
    Ok(encoded)
}

fn validate_relative(path: &Path) -> Result<(), LauncherError> {
    if path.as_os_str().is_empty()
        || path.is_absolute()
        || !path
            .components()
            .all(|component| matches!(component, Component::Normal(_)))
    {
        return Err(LauncherError::configuration("ARTIFACT_PATH_INVALID"));
    }
    Ok(())
}

fn validate_digest(digest: &str) -> Result<(), LauncherError> {
    if digest.len() != 64
        || !digest
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(LauncherError::configuration("MANIFEST_INVALID"));
    }
    Ok(())
}
