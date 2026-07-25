//! Closed parsing for launcher-owned arguments.

use std::ffi::{OsStr, OsString};
use std::path::PathBuf;

use crate::error::LauncherError;

/// Parsed launcher-owned paths and untouched Python CLI arguments.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LauncherArgs {
    /// Absolute distribution-manifest path.
    pub distribution_manifest: PathBuf,
    /// Absolute platform-manifest path.
    pub platform_manifest: PathBuf,
    /// Arguments after the required separator, preserved as OS strings.
    pub user_args: Vec<OsString>,
}

impl LauncherArgs {
    /// Parse the closed internal flag set followed by a required `--`.
    pub fn parse<I>(arguments: I) -> Result<Self, LauncherError>
    where
        I: IntoIterator<Item = OsString>,
    {
        let mut distribution_manifest = None;
        let mut platform_manifest = None;
        let mut user_args = Vec::new();
        let mut iterator = arguments.into_iter();
        let mut separated = false;

        while let Some(argument) = iterator.next() {
            if argument == OsStr::new("--") {
                user_args.extend(iterator);
                separated = true;
                break;
            }
            let Some(value) = iterator.next() else {
                return Err(invalid_arguments());
            };
            if value.is_empty() || value == OsStr::new("--") {
                return Err(invalid_arguments());
            }
            let path = PathBuf::from(value);
            if !path.is_absolute() {
                return Err(invalid_arguments());
            }
            match argument.to_str() {
                Some("--distribution-manifest") if distribution_manifest.is_none() => {
                    distribution_manifest = Some(path);
                }
                Some("--platform-manifest") if platform_manifest.is_none() => {
                    platform_manifest = Some(path);
                }
                _ => return Err(invalid_arguments()),
            }
        }

        if !separated {
            return Err(invalid_arguments());
        }
        Ok(Self {
            distribution_manifest: distribution_manifest.ok_or_else(invalid_arguments)?,
            platform_manifest: platform_manifest.ok_or_else(invalid_arguments)?,
            user_args,
        })
    }
}

fn invalid_arguments() -> LauncherError {
    LauncherError::configuration("ARGUMENTS_INVALID")
}
