//! Aizim native launcher entry point.

use std::ffi::OsStr;
use std::io::{self, Write};
use std::process::ExitCode;

fn main() -> ExitCode {
    if std::env::args_os().nth(1).as_deref() == Some(OsStr::new("--version")) {
        return write_version();
    }
    write_unavailable()
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

fn write_unavailable() -> ExitCode {
    if writeln!(io::stderr().lock(), "aizim-launcher: LAUNCHER_NOT_READY").is_err() {
        return ExitCode::from(74);
    }
    ExitCode::from(70)
}
