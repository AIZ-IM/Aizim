//! Aizim native launcher entry point.

use std::ffi::OsStr;
use std::io::{self, Write};
use std::process::ExitCode;

use aizim_launcher::args::LauncherArgs;
use aizim_launcher::error::{ErrorKind, LauncherError};
use aizim_launcher::manifest::VerifiedDistribution;

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
    let _distribution = VerifiedDistribution::load(&arguments)?;
    Err(LauncherError::unavailable("RUNTIME_NOT_PROVISIONED"))
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
