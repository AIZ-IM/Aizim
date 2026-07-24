//! Aizim native launcher entry point.

use std::ffi::OsStr;
use std::io::{self, Write};
use std::path::PathBuf;
use std::process::ExitCode;

use aizim_launcher::args::LauncherArgs;
use aizim_launcher::cache::{CacheEnvironment, CacheKey, CacheLayout};
use aizim_launcher::error::{ErrorKind, LauncherError};
use aizim_launcher::manifest::VerifiedDistribution;
use aizim_launcher::process::{RealCommandRunner, exec_command};
use aizim_launcher::provision::{ProvisionRequest, build_exec_spec, ensure_runtime};

fn main() -> ExitCode {
    if std::env::args_os().nth(1).as_deref() == Some(OsStr::new("--version")) {
        return write_version();
    }
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => write_error(&error),
    }
}

fn run() -> Result<(), LauncherError> {
    let arguments = LauncherArgs::parse(std::env::args_os().skip(1))?;
    let distribution = VerifiedDistribution::load(&arguments)?;
    let key = CacheKey {
        aizim_version: distribution.distribution.aizim_version.clone(),
        target: distribution.platform.target.clone(),
        wheel_sha256: distribution.distribution.wheel.sha256.clone(),
        python_version: distribution.distribution.python_version.clone(),
    };
    let layout = CacheLayout::new(
        &CacheEnvironment {
            platform: host_platform()?,
            home: std::env::var_os("HOME").map(PathBuf::from),
            xdg_cache_home: std::env::var_os("XDG_CACHE_HOME").map(PathBuf::from),
            override_root: std::env::var_os("AIZIM_CACHE_DIR").map(PathBuf::from),
        },
        &key,
    )?;
    let cwd = std::env::current_dir().map_err(|source| {
        LauncherError::internal("WORKING_DIRECTORY_INVALID").with_source(source)
    })?;
    let request = ProvisionRequest {
        uv: distribution.uv,
        wheel: distribution.wheel,
        runtime_requirements: distribution.runtime_requirements,
        claude_executable: distribution.claude_executable,
        codex_executable: distribution.codex_executable,
        layout,
        key,
        distribution_manifest_sha256: distribution.distribution_manifest_sha256,
        platform_manifest_sha256: distribution.platform_manifest_sha256,
        inherited_environment: std::env::vars_os().collect(),
        temporary_variable: "TMPDIR",
        cwd,
    };
    let mut runner = RealCommandRunner;
    let ready = ensure_runtime(&request, &mut runner, &mut io::stderr().lock())?;
    let command = build_exec_spec(&request, &ready, &arguments.user_args)?;
    Err(exec_command(&command))
}

fn host_platform() -> Result<&'static str, LauncherError> {
    match std::env::consts::OS {
        "macos" => Ok("darwin"),
        "linux" => Ok("linux"),
        _ => Err(LauncherError::configuration("TARGET_MISMATCH")),
    }
}

fn write_version() -> ExitCode {
    if writeln!(
        io::stdout().lock(),
        "aizim-launcher {}",
        aizim_launcher::VERSION
    )
    .is_err()
    {
        return ExitCode::from(74);
    }
    ExitCode::SUCCESS
}

fn write_error(error: &LauncherError) -> ExitCode {
    if writeln!(io::stderr().lock(), "aizim-launcher: {error}").is_err() {
        return ExitCode::from(74);
    }
    ExitCode::from(exit_code(error.kind))
}

const fn exit_code(kind: ErrorKind) -> u8 {
    match kind {
        ErrorKind::Unavailable => 69,
        ErrorKind::Internal => 70,
        ErrorKind::Integrity => 74,
        ErrorKind::Configuration => 78,
    }
}
