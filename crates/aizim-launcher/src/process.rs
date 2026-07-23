//! Shell-free child command and final Unix process specifications.

use std::ffi::OsString;
use std::path::PathBuf;
use std::process::{Command, ExitStatus, Stdio};

use crate::error::LauncherError;

/// One shell-free process invocation with an explicit environment.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandSpec {
    /// Absolute executable path.
    pub program: PathBuf,
    /// Ordered opaque process arguments.
    pub args: Vec<OsString>,
    /// Whether to remove the inherited process environment first.
    pub env_clear: bool,
    /// Explicit environment entries.
    pub env: Vec<(OsString, OsString)>,
    /// Absolute child working directory.
    pub cwd: PathBuf,
    /// Exact trimmed stdout required for a validation command.
    pub expected_stdout: Option<String>,
}

/// Injectable command execution boundary used by runtime provisioning.
pub trait CommandRunner {
    /// Run one command and return its unmodified exit status.
    fn run(&mut self, spec: &CommandSpec) -> Result<ExitStatus, LauncherError>;
}

/// Real shell-free command runner with inherited terminal streams.
#[derive(Debug, Default)]
pub struct RealCommandRunner;

impl CommandRunner for RealCommandRunner {
    fn run(&mut self, spec: &CommandSpec) -> Result<ExitStatus, LauncherError> {
        let mut command = command_from_spec(spec);
        if let Some(expected) = &spec.expected_stdout {
            let output = command
                .stdin(Stdio::inherit())
                .stdout(Stdio::piped())
                .stderr(Stdio::inherit())
                .output()
                .map_err(provision_error)?;
            if output.status.success()
                && String::from_utf8(output.stdout)
                    .ok()
                    .is_none_or(|stdout| stdout.trim_end() != expected)
            {
                return Err(LauncherError::unavailable("RUNTIME_VERSION_MISMATCH"));
            }
            return Ok(output.status);
        }
        command
            .stdin(Stdio::inherit())
            .stdout(Stdio::inherit())
            .stderr(Stdio::inherit())
            .status()
            .map_err(provision_error)
    }
}

/// Replace the launcher with the final Aizim process.
pub fn exec_command(spec: &CommandSpec) -> LauncherError {
    use std::os::unix::process::CommandExt;

    let source = command_from_spec(spec)
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .exec();
    LauncherError::integrity("AIZIM_EXEC_FAILED").with_source(source)
}

fn command_from_spec(spec: &CommandSpec) -> Command {
    let mut command = Command::new(&spec.program);
    command.args(&spec.args).current_dir(&spec.cwd);
    if spec.env_clear {
        command.env_clear();
    }
    command.envs(spec.env.iter().cloned());
    command
}

fn provision_error(source: std::io::Error) -> LauncherError {
    LauncherError::unavailable("RUNTIME_PROVISION_FAILED").with_source(source)
}
