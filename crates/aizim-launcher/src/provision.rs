//! Bundled-uv provisioning and final npm distribution context.

use std::collections::BTreeMap;
use std::ffi::{OsStr, OsString};
use std::fs;
use std::io::Write;
use std::os::unix::ffi::OsStrExt;
use std::path::{Component, Path, PathBuf};

use crate::cache::{
    CACHE_SCHEMA_VERSION, CacheKey, CacheLayout, ReadyMarker, ReadyRuntime, RuntimeLock,
};
use crate::error::LauncherError;
use crate::integrity::verify_executable;
use crate::process::{CommandRunner, CommandSpec};

/// Verified artifacts and process context required to provision one runtime.
#[derive(Debug, Clone)]
pub struct ProvisionRequest {
    /// Canonical bundled uv executable.
    pub uv: PathBuf,
    /// Canonical Aizim wheel.
    pub wheel: PathBuf,
    /// Canonical hash-locked runtime requirements.
    pub runtime_requirements: PathBuf,
    /// Canonical package-local Claude executable.
    pub claude_executable: PathBuf,
    /// Canonical package-local Codex executable.
    pub codex_executable: PathBuf,
    /// Versioned cache layout.
    pub layout: CacheLayout,
    /// Immutable runtime identity.
    pub key: CacheKey,
    /// Verified distribution-manifest digest.
    pub distribution_manifest_sha256: String,
    /// Verified platform-manifest digest.
    pub platform_manifest_sha256: String,
    /// Original launcher environment.
    pub inherited_environment: Vec<(OsString, OsString)>,
    /// Platform temporary-directory environment key.
    pub temporary_variable: &'static str,
    /// Original absolute working directory.
    pub cwd: PathBuf,
}

/// Reuse or atomically provision the exact managed Python runtime.
pub fn ensure_runtime(
    request: &ProvisionRequest,
    runner: &mut dyn CommandRunner,
    status: &mut dyn Write,
) -> Result<ReadyRuntime, LauncherError> {
    let _lock = RuntimeLock::acquire(&request.layout.lock_path)?;
    if let Some(ready) = request.layout.ready_runtime(&request.key)? {
        return Ok(ready);
    }
    request.layout.cleanup_staging()?;
    let staging = request.layout.create_staging()?;
    let fingerprint = request.key.fingerprint(CACHE_SCHEMA_VERSION);
    writeln!(
        status,
        "aizim: provisioning managed Python {} runtime ({fingerprint})",
        request.key.python_version
    )
    .map_err(status_error)?;

    let provisioned = provision_staging(request, &staging, runner)
        .and_then(|()| request.layout.promote(&staging, &request.key));
    let ready = match provisioned {
        Ok(ready) => ready,
        Err(error) => {
            request.layout.cleanup_staging()?;
            return Err(error);
        }
    };
    writeln!(status, "aizim: runtime ready ({fingerprint})").map_err(status_error)?;
    Ok(ready)
}

/// Build the environment-cleared final process specification.
pub fn build_exec_spec(
    request: &ProvisionRequest,
    ready: &ReadyRuntime,
    user_args: &[OsString],
) -> Result<CommandSpec, LauncherError> {
    let mut environment = BTreeMap::new();
    for (key, value) in &request.inherited_environment {
        if !is_injection_variable(key) {
            environment.insert(key.clone(), value.clone());
        }
    }
    for (key, value) in [
        ("AIZIM_DISTRIBUTION_MODE", OsString::from("npm")),
        (
            "AIZIM_DISTRIBUTION_VERSION",
            OsString::from(&request.key.aizim_version),
        ),
        (
            "AIZIM_DISTRIBUTION_TARGET",
            OsString::from(&request.key.target),
        ),
        (
            "AIZIM_CLAUDE_EXECUTABLE",
            request.claude_executable.clone().into_os_string(),
        ),
        (
            "AIZIM_CODEX_EXECUTABLE",
            request.codex_executable.clone().into_os_string(),
        ),
        (
            "AIZIM_DISTRIBUTION_MANIFEST_SHA256",
            OsString::from(&request.distribution_manifest_sha256),
        ),
        (
            "AIZIM_PLATFORM_MANIFEST_SHA256",
            OsString::from(&request.platform_manifest_sha256),
        ),
    ] {
        environment.insert(OsString::from(key), value);
    }
    let inherited_path = environment.get(OsStr::new("PATH")).map(OsString::as_os_str);
    environment.insert(
        OsString::from("PATH"),
        runtime_path(request, inherited_path)?,
    );
    if !request.cwd.is_absolute() {
        return Err(LauncherError::internal("WORKING_DIRECTORY_INVALID"));
    }
    Ok(CommandSpec {
        program: ready.aizim.clone(),
        args: user_args.to_vec(),
        env_clear: true,
        env: environment.into_iter().collect(),
        cwd: request.cwd.clone(),
        expected_stdout: None,
    })
}

fn runtime_path(
    request: &ProvisionRequest,
    inherited_path: Option<&OsStr>,
) -> Result<OsString, LauncherError> {
    let codex_root = request
        .codex_executable
        .parent()
        .and_then(Path::parent)
        .ok_or_else(|| LauncherError::integrity("CODEX_INTEGRITY_FAILED"))?;
    let ripgrep = verify_executable(&codex_root.join("codex-path").join("rg"))?;
    let codex_path = ripgrep
        .parent()
        .filter(|directory| directory.parent() == Some(codex_root))
        .ok_or_else(|| LauncherError::integrity("CODEX_INTEGRITY_FAILED"))?;
    let mut paths = vec![codex_path.to_path_buf()];
    if let Some(value) = inherited_path {
        paths.extend(std::env::split_paths(value));
    }
    std::env::join_paths(paths)
        .map_err(|source| LauncherError::integrity("CODEX_INTEGRITY_FAILED").with_source(source))
}

fn provision_staging(
    request: &ProvisionRequest,
    staging: &Path,
    runner: &mut dyn CommandRunner,
) -> Result<(), LauncherError> {
    let venv = staging.join("venv");
    let python = venv.join("bin").join("python");
    let aizim = venv.join("bin").join("aizim");
    let sidecar = venv.join("bin").join("aizim-gateway-sidecar");
    let uv_environment = bootstrap_environment(request, true);
    let validation_environment = bootstrap_environment(request, false);
    let commands = [
        CommandSpec {
            program: request.uv.clone(),
            args: os_strings(["--no-config", "python", "install", "--install-dir"])
                .into_iter()
                .chain([
                    request.layout.managed_python_root.clone().into_os_string(),
                    OsString::from(&request.key.python_version),
                ])
                .collect(),
            env_clear: true,
            env: uv_environment.clone(),
            cwd: request.layout.root.clone(),
            expected_stdout: None,
        },
        CommandSpec {
            program: request.uv.clone(),
            args: os_strings(["--no-config", "venv", "--managed-python", "--python"])
                .into_iter()
                .chain([
                    OsString::from(&request.key.python_version),
                    venv.into_os_string(),
                ])
                .collect(),
            env_clear: true,
            env: uv_environment.clone(),
            cwd: request.layout.root.clone(),
            expected_stdout: None,
        },
        CommandSpec {
            program: request.uv.clone(),
            args: os_strings(["--no-config", "pip", "install", "--python"])
                .into_iter()
                .chain([python.clone().into_os_string()])
                .chain(os_strings([
                    "--require-hashes",
                    "--no-deps",
                    "--default-index",
                    "https://pypi.org/simple",
                    "-r",
                ]))
                .chain([request.runtime_requirements.clone().into_os_string()])
                .collect(),
            env_clear: true,
            env: uv_environment.clone(),
            cwd: request.layout.root.clone(),
            expected_stdout: None,
        },
        CommandSpec {
            program: request.uv.clone(),
            args: os_strings(["--no-config", "pip", "install", "--python"])
                .into_iter()
                .chain([python.into_os_string()])
                .chain(os_strings(["--no-deps"]))
                .chain([request.wheel.clone().into_os_string()])
                .collect(),
            env_clear: true,
            env: uv_environment,
            cwd: request.layout.root.clone(),
            expected_stdout: None,
        },
        CommandSpec {
            program: aizim.clone(),
            args: os_strings(["--version"]),
            env_clear: true,
            env: validation_environment,
            cwd: staging.to_path_buf(),
            expected_stdout: Some(format!("aizim {}", request.key.aizim_version)),
        },
    ];
    let fingerprint = request.key.fingerprint(CACHE_SCHEMA_VERSION);
    for command in &commands {
        let result = runner
            .run(command)
            .map_err(|error| error.context("cache_key", &fingerprint))?;
        if !result.success() {
            return Err(LauncherError::unavailable("RUNTIME_PROVISION_FAILED")
                .context("cache_key", &fingerprint));
        }
    }
    finalize_staging(request, staging, &aizim, &sidecar)
}

fn finalize_staging(
    request: &ProvisionRequest,
    staging: &Path,
    aizim: &Path,
    sidecar: &Path,
) -> Result<(), LauncherError> {
    relocate_venv_scripts(staging, &request.layout.runtime_root)?;
    verify_staging_executable(staging, aizim)?;
    verify_staging_executable(staging, sidecar)?;
    request.layout.write_ready(
        staging,
        &ReadyMarker {
            schema_version: CACHE_SCHEMA_VERSION,
            aizim_version: request.key.aizim_version.clone(),
            target: request.key.target.clone(),
            wheel_sha256: request.key.wheel_sha256.clone(),
            python_version: request.key.python_version.clone(),
            aizim_entrypoint: "venv/bin/aizim".to_owned(),
            sidecar_entrypoint: "venv/bin/aizim-gateway-sidecar".to_owned(),
        },
    )
}

fn relocate_venv_scripts(staging: &Path, runtime_root: &Path) -> Result<(), LauncherError> {
    let source = staging.as_os_str().as_bytes();
    let destination = runtime_root.as_os_str().as_bytes();
    let binaries = staging.join("venv").join("bin");
    for entry in fs::read_dir(&binaries).map_err(staging_error)? {
        let path = entry.map_err(staging_error)?.path();
        let metadata = fs::symlink_metadata(&path).map_err(staging_error)?;
        if !metadata.file_type().is_file() {
            continue;
        }
        let content = fs::read(&path).map_err(staging_error)?;
        if !content.windows(source.len()).any(|window| window == source) {
            continue;
        }
        let relocated = replace_bytes(&content, source, destination);
        fs::write(&path, relocated).map_err(staging_error)?;
    }
    Ok(())
}

fn replace_bytes(content: &[u8], source: &[u8], destination: &[u8]) -> Vec<u8> {
    let mut output = Vec::with_capacity(content.len());
    let mut offset = 0;
    while offset < content.len() {
        let Some(remaining) = content.get(offset..) else {
            break;
        };
        if remaining.starts_with(source) {
            output.extend_from_slice(destination);
            offset += source.len();
        } else if let Some(byte) = remaining.first() {
            output.push(*byte);
            offset += 1;
        }
    }
    output
}

fn bootstrap_environment(
    request: &ProvisionRequest,
    include_uv: bool,
) -> Vec<(OsString, OsString)> {
    let mut environment = BTreeMap::new();
    for (key, value) in &request.inherited_environment {
        if bootstrap_allowed(key, request.temporary_variable) {
            environment.insert(key.clone(), value.clone());
        }
    }
    if include_uv {
        environment.insert(
            OsString::from("UV_CACHE_DIR"),
            request.layout.uv_cache_root.clone().into_os_string(),
        );
        environment.insert(
            OsString::from("UV_PYTHON_INSTALL_DIR"),
            request.layout.managed_python_root.clone().into_os_string(),
        );
        environment.insert(OsString::from("UV_NO_PROGRESS"), OsString::from("1"));
    }
    environment.into_iter().collect()
}

fn bootstrap_allowed(key: &OsStr, temporary_variable: &str) -> bool {
    key == OsStr::new(temporary_variable)
        || matches!(
            key.to_str(),
            Some(
                "HTTP_PROXY"
                    | "HTTPS_PROXY"
                    | "NO_PROXY"
                    | "SSL_CERT_FILE"
                    | "SSL_CERT_DIR"
                    | "LANG"
                    | "LC_ALL"
            )
        )
}

fn is_injection_variable(key: &OsStr) -> bool {
    let Some(key) = key.to_str() else {
        return false;
    };
    key.starts_with("UV_")
        || key.starts_with("PIP_")
        || matches!(key, "PYTHONPATH" | "PYTHONHOME" | "VIRTUAL_ENV")
        || key.starts_with("AIZIM_DISTRIBUTION_")
        || key == "AIZIM_CLAUDE_EXECUTABLE"
        || key == "AIZIM_CODEX_EXECUTABLE"
}

fn verify_staging_executable(staging: &Path, path: &Path) -> Result<(), LauncherError> {
    use std::os::unix::fs::PermissionsExt;

    let relative = path
        .strip_prefix(staging)
        .map_err(|source| LauncherError::integrity("CACHE_LAYOUT_INVALID").with_source(source))?;
    if !relative
        .components()
        .all(|component| matches!(component, Component::Normal(_)))
    {
        return Err(LauncherError::integrity("CACHE_LAYOUT_INVALID"));
    }
    let metadata = fs::symlink_metadata(path).map_err(staging_error)?;
    if !metadata.file_type().is_file() || metadata.permissions().mode() & 0o111 == 0 {
        return Err(LauncherError::integrity("CACHE_LAYOUT_INVALID"));
    }
    let root = fs::canonicalize(staging).map_err(staging_error)?;
    let executable = fs::canonicalize(path).map_err(staging_error)?;
    if !executable.starts_with(root) {
        return Err(LauncherError::integrity("CACHE_LAYOUT_INVALID"));
    }
    Ok(())
}

fn os_strings<const N: usize>(values: [&str; N]) -> Vec<OsString> {
    values.into_iter().map(OsString::from).collect()
}

fn status_error(source: std::io::Error) -> LauncherError {
    LauncherError::integrity("STATUS_IO_FAILED").with_source(source)
}

fn staging_error(source: std::io::Error) -> LauncherError {
    LauncherError::integrity("CACHE_LAYOUT_INVALID").with_source(source)
}
