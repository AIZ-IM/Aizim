//! Stable launcher error categories and redacted diagnostics.

use std::error::Error;
use std::fmt;

/// Stable launcher-only error category.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    /// Python runtime or dependency provisioning is unavailable.
    Unavailable,
    /// An internal launcher invariant failed.
    Internal,
    /// A manifest, digest, filesystem, or I/O integrity check failed.
    Integrity,
    /// The installed package configuration is unsupported or mismatched.
    Configuration,
}

impl ErrorKind {
    /// Return the stable BSD `sysexits`-compatible status.
    pub const fn exit_code(self) -> i32 {
        match self {
            Self::Unavailable => 69,
            Self::Internal => 70,
            Self::Integrity => 74,
            Self::Configuration => 78,
        }
    }
}

/// Redacted launcher failure with a stable category and code.
#[derive(Debug)]
pub struct LauncherError {
    /// Stable error category.
    pub kind: ErrorKind,
    /// Stable machine-readable error code.
    pub code: &'static str,
    message: &'static str,
    remediation: &'static str,
    safe_context: Vec<(&'static str, String)>,
    /// Nested source retained for local debugging but omitted from display.
    pub source: Option<Box<dyn Error + Send + Sync>>,
}

impl LauncherError {
    /// Construct an unavailable-runtime failure.
    pub fn unavailable(code: &'static str) -> Self {
        let (message, remediation) = match code {
            "RUNTIME_NOT_PROVISIONED" => (
                "the managed Python runtime is not provisioned",
                "retry after provisioning the packaged runtime",
            ),
            "RUNTIME_PROVISION_FAILED" => (
                "managed Python runtime provisioning failed",
                "check network availability and retry",
            ),
            "RUNTIME_VERSION_MISMATCH" => (
                "the provisioned Aizim version is incompatible",
                "remove the incomplete runtime and retry",
            ),
            _ => (
                "the managed Python runtime is unavailable",
                "retry the operation",
            ),
        };
        Self::new(ErrorKind::Unavailable, code, message, remediation)
    }

    /// Construct an internal-invariant failure.
    pub fn internal(code: &'static str) -> Self {
        Self::new(
            ErrorKind::Internal,
            code,
            "an internal launcher invariant failed",
            "reinstall Aizim and retry",
        )
    }

    /// Construct an artifact or filesystem integrity failure.
    pub fn integrity(code: &'static str) -> Self {
        let message = match code {
            "MANIFEST_IO_FAILED" => "a distribution manifest could not be read safely",
            "ARTIFACT_INTEGRITY_FAILED" => "a packaged artifact failed integrity verification",
            "CODEX_INTEGRITY_FAILED" => "the packaged Codex executable is invalid",
            "CACHE_LAYOUT_INVALID" => "the managed runtime cache layout is unsafe",
            "AIZIM_EXEC_FAILED" => "the installed Aizim entry point could not be executed",
            _ => "a launcher integrity check failed",
        };
        Self::new(
            ErrorKind::Integrity,
            code,
            message,
            "perform a clean npm reinstall and retry",
        )
    }

    /// Construct a package-configuration failure.
    pub fn configuration(code: &'static str) -> Self {
        let message = match code {
            "ARGUMENTS_INVALID" => "launcher arguments are invalid",
            "MANIFEST_INVALID" => "distribution manifest configuration is invalid",
            "DISTRIBUTION_VERSION_MISMATCH" => {
                "distribution manifest version does not match the launcher"
            }
            "PLATFORM_VERSION_MISMATCH" => "platform manifest version does not match the launcher",
            "TARGET_MISMATCH" => "the platform package does not match this host",
            "ARTIFACT_PATH_INVALID" => "a packaged artifact path is invalid",
            "CACHE_ROOT_INVALID" => "the runtime cache root is invalid",
            _ => "the installed package configuration is invalid",
        };
        Self::new(
            ErrorKind::Configuration,
            code,
            message,
            "reinstall matching Aizim npm packages and retry",
        )
    }

    fn new(
        kind: ErrorKind,
        code: &'static str,
        message: &'static str,
        remediation: &'static str,
    ) -> Self {
        Self {
            kind,
            code,
            message,
            remediation,
            safe_context: Vec::new(),
            source: None,
        }
    }

    /// Add a bounded, allowlisted diagnostic context field.
    #[must_use]
    pub fn context(mut self, key: &'static str, value: impl Into<String>) -> Self {
        let value = value.into();
        if matches!(key, "expected" | "observed" | "cache_key") && safe_value(&value) {
            if let Some((_, current)) = self
                .safe_context
                .iter_mut()
                .find(|(current_key, _)| *current_key == key)
            {
                *current = value;
            } else {
                self.safe_context.push((key, value));
            }
        }
        self
    }

    /// Retain a nested source without exposing it in the public diagnostic.
    #[must_use]
    pub fn with_source(mut self, source: impl Error + Send + Sync + 'static) -> Self {
        self.source = Some(Box::new(source));
        self
    }

    /// Return the stable process exit status.
    pub const fn exit_code(&self) -> i32 {
        self.kind.exit_code()
    }
}

fn safe_value(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 80
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

impl fmt::Display for LauncherError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{}: {}", self.code, self.message)?;
        for (key, value) in &self.safe_context {
            write!(formatter, "; {key}={value}")?;
        }
        write!(formatter, "; remediation={}", self.remediation)
    }
}

impl Error for LauncherError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        self.source
            .as_deref()
            .map(|source| source as &(dyn Error + 'static))
    }
}
