//! Strict distribution manifests and verified artifact paths.

use std::ffi::OsStr;
use std::fs;
use std::path::{Component, Path, PathBuf};

use serde::Deserialize;

use crate::VERSION;
use crate::args::LauncherArgs;
use crate::error::LauncherError;
use crate::integrity::{sha256_file, verify_artifact, verify_executable};

/// One size- and digest-pinned package artifact.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Artifact {
    /// Package-relative path containing only normal components.
    pub path: PathBuf,
    /// Exact file size in bytes.
    pub size: u64,
    /// Exact lowercase SHA-256 digest.
    pub sha256: String,
}

/// Platform-independent npm package manifest.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DistributionManifest {
    /// Manifest schema version.
    pub schema_version: u32,
    /// Aizim distribution version.
    pub aizim_version: String,
    /// Managed `CPython` minor version.
    pub python_version: String,
    /// Aizim wheel artifact.
    pub wheel: Artifact,
    /// Hash-locked runtime requirements artifact.
    pub runtime_requirements: Artifact,
    /// Exact local Codex package version.
    pub codex_version: String,
    /// Minimum supported Node.js version.
    pub minimum_node_version: String,
    /// Required platform-manifest schema version.
    pub platform_schema_version: u32,
}

/// Native platform npm package manifest.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlatformManifest {
    /// Manifest schema version.
    pub schema_version: u32,
    /// Aizim distribution version.
    pub aizim_version: String,
    /// Exact npm platform package name.
    pub package_name: String,
    /// Closed Aizim target identifier.
    pub target: String,
    /// Node.js platform identifier.
    pub node_platform: String,
    /// Node.js architecture identifier.
    pub node_arch: String,
    /// Rust compilation target triple.
    pub rust_target: String,
    /// Required libc family on Linux.
    pub libc: Option<String>,
    /// Native launcher version.
    pub launcher_version: String,
    /// Bundled uv version.
    pub uv_version: String,
    /// Bundled uv artifact.
    pub uv: Artifact,
    /// Required distribution-manifest schema version.
    pub distribution_schema_version: u32,
}

/// Canonical paths and metadata accepted by the launcher trust boundary.
#[derive(Debug)]
pub struct VerifiedDistribution {
    /// Parsed platform-independent manifest.
    pub distribution: DistributionManifest,
    /// Parsed native platform manifest.
    pub platform: PlatformManifest,
    /// Canonical Aizim wheel path.
    pub wheel: PathBuf,
    /// Canonical hash-locked requirements path.
    pub runtime_requirements: PathBuf,
    /// Canonical bundled uv executable path.
    pub uv: PathBuf,
    /// Canonical package-local Codex executable path.
    pub codex_executable: PathBuf,
    /// SHA-256 of the distribution manifest.
    pub distribution_manifest_sha256: String,
    /// SHA-256 of the platform manifest.
    pub platform_manifest_sha256: String,
}

impl VerifiedDistribution {
    /// Load both manifests and verify every package artifact.
    pub fn load(arguments: &LauncherArgs) -> Result<Self, LauncherError> {
        let (distribution_root, distribution_path) =
            manifest_location(&arguments.distribution_manifest, "distribution.json")?;
        let (platform_root, platform_path) =
            manifest_location(&arguments.platform_manifest, "platform.json")?;
        if distribution_root == platform_root {
            return Err(LauncherError::configuration("MANIFEST_INVALID"));
        }

        let distribution: DistributionManifest = read_manifest(&distribution_path)?;
        let platform: PlatformManifest = read_manifest(&platform_path)?;
        validate_distribution(&distribution)?;
        validate_platform(&platform)?;
        require_equal(
            "DISTRIBUTION_VERSION_MISMATCH",
            &platform.aizim_version,
            &distribution.aizim_version,
        )?;

        let wheel = verify_artifact(&distribution_root, &platform_root, &distribution.wheel)?;
        let runtime_requirements = verify_artifact(
            &distribution_root,
            &platform_root,
            &distribution.runtime_requirements,
        )?;
        let uv = verify_artifact(&platform_root, &distribution_root, &platform.uv)?;
        verify_executable(&uv)?;
        let codex_executable = verify_executable(&arguments.codex_executable)?;

        Ok(Self {
            distribution,
            platform,
            wheel,
            runtime_requirements,
            uv,
            codex_executable,
            distribution_manifest_sha256: sha256_file(&distribution_path)?,
            platform_manifest_sha256: sha256_file(&platform_path)?,
        })
    }
}

fn manifest_location(path: &Path, name: &str) -> Result<(PathBuf, PathBuf), LauncherError> {
    if !path.is_absolute()
        || path.file_name() != Some(OsStr::new(name))
        || !path.components().all(|component| {
            matches!(
                component,
                Component::Prefix(_) | Component::RootDir | Component::Normal(_)
            )
        })
    {
        return Err(LauncherError::configuration("MANIFEST_INVALID"));
    }
    let manifest_directory = path
        .parent()
        .filter(|directory| directory.file_name() == Some(OsStr::new("manifest")))
        .ok_or_else(|| LauncherError::configuration("MANIFEST_INVALID"))?;
    let package_root = manifest_directory
        .parent()
        .ok_or_else(|| LauncherError::configuration("MANIFEST_INVALID"))?;
    for candidate in [package_root, manifest_directory, path] {
        let metadata = fs::symlink_metadata(candidate)
            .map_err(|source| LauncherError::integrity("MANIFEST_IO_FAILED").with_source(source))?;
        if metadata.file_type().is_symlink() {
            return Err(LauncherError::integrity("MANIFEST_IO_FAILED"));
        }
    }
    let canonical_root = fs::canonicalize(package_root)
        .map_err(|source| LauncherError::integrity("MANIFEST_IO_FAILED").with_source(source))?;
    let canonical_manifest = fs::canonicalize(path)
        .map_err(|source| LauncherError::integrity("MANIFEST_IO_FAILED").with_source(source))?;
    if canonical_manifest.parent().and_then(Path::parent) != Some(canonical_root.as_path()) {
        return Err(LauncherError::integrity("MANIFEST_IO_FAILED"));
    }
    Ok((canonical_root, canonical_manifest))
}

fn read_manifest<T>(path: &Path) -> Result<T, LauncherError>
where
    T: for<'de> Deserialize<'de>,
{
    let bytes = fs::read(path)
        .map_err(|source| LauncherError::integrity("MANIFEST_IO_FAILED").with_source(source))?;
    serde_json::from_slice(&bytes)
        .map_err(|source| LauncherError::configuration("MANIFEST_INVALID").with_source(source))
}

fn validate_distribution(manifest: &DistributionManifest) -> Result<(), LauncherError> {
    require_number("MANIFEST_INVALID", manifest.schema_version, 1)?;
    require_equal(
        "DISTRIBUTION_VERSION_MISMATCH",
        &manifest.aizim_version,
        VERSION,
    )?;
    require_equal(
        "DISTRIBUTION_VERSION_MISMATCH",
        &manifest.python_version,
        "3.12",
    )?;
    require_equal(
        "DISTRIBUTION_VERSION_MISMATCH",
        &manifest.codex_version,
        "0.145.0",
    )?;
    require_equal(
        "DISTRIBUTION_VERSION_MISMATCH",
        &manifest.minimum_node_version,
        "22.14.0",
    )?;
    require_number("MANIFEST_INVALID", manifest.platform_schema_version, 1)
}

fn validate_platform(manifest: &PlatformManifest) -> Result<(), LauncherError> {
    require_number("MANIFEST_INVALID", manifest.schema_version, 1)?;
    require_number("MANIFEST_INVALID", manifest.distribution_schema_version, 1)?;
    require_equal(
        "PLATFORM_VERSION_MISMATCH",
        &manifest.aizim_version,
        VERSION,
    )?;
    require_equal(
        "PLATFORM_VERSION_MISMATCH",
        &manifest.launcher_version,
        VERSION,
    )?;
    require_equal("PLATFORM_VERSION_MISMATCH", &manifest.uv_version, "0.11.31")?;

    let expected = host_target().ok_or_else(|| LauncherError::configuration("TARGET_MISMATCH"))?;
    require_equal("TARGET_MISMATCH", &manifest.target, expected.id)?;
    require_equal(
        "TARGET_MISMATCH",
        &manifest.package_name,
        &format!("@aiz.im/aizim-{}", expected.id),
    )?;
    require_equal(
        "TARGET_MISMATCH",
        &manifest.node_platform,
        expected.node_platform,
    )?;
    require_equal("TARGET_MISMATCH", &manifest.node_arch, expected.node_arch)?;
    require_equal(
        "TARGET_MISMATCH",
        &manifest.rust_target,
        expected.rust_triple,
    )?;
    require_equal(
        "TARGET_MISMATCH",
        manifest.libc.as_deref().unwrap_or("none"),
        expected.libc.unwrap_or("none"),
    )
}

struct Target {
    id: &'static str,
    node_platform: &'static str,
    node_arch: &'static str,
    rust_triple: &'static str,
    libc: Option<&'static str>,
}

fn host_target() -> Option<Target> {
    match (std::env::consts::OS, std::env::consts::ARCH) {
        ("macos", "aarch64") => Some(Target {
            id: "darwin-arm64",
            node_platform: "darwin",
            node_arch: "arm64",
            rust_triple: "aarch64-apple-darwin",
            libc: None,
        }),
        ("macos", "x86_64") => Some(Target {
            id: "darwin-x64",
            node_platform: "darwin",
            node_arch: "x64",
            rust_triple: "x86_64-apple-darwin",
            libc: None,
        }),
        ("linux", "aarch64") => Some(Target {
            id: "linux-arm64",
            node_platform: "linux",
            node_arch: "arm64",
            rust_triple: "aarch64-unknown-linux-gnu",
            libc: Some("glibc"),
        }),
        ("linux", "x86_64") => Some(Target {
            id: "linux-x64",
            node_platform: "linux",
            node_arch: "x64",
            rust_triple: "x86_64-unknown-linux-gnu",
            libc: Some("glibc"),
        }),
        _ => None,
    }
}

fn require_equal(code: &'static str, observed: &str, expected: &str) -> Result<(), LauncherError> {
    if observed == expected {
        return Ok(());
    }
    Err(LauncherError::configuration(code)
        .context("expected", expected)
        .context("observed", observed))
}

fn require_number(code: &'static str, observed: u32, expected: u32) -> Result<(), LauncherError> {
    if observed == expected {
        return Ok(());
    }
    Err(LauncherError::configuration(code)
        .context("expected", expected.to_string())
        .context("observed", observed.to_string()))
}
