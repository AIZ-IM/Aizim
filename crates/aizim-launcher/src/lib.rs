//! Native distribution launcher primitives for Aizim.

/// Launcher-owned argument parsing.
pub mod args;
/// Stable, redacted launcher failures.
pub mod error;
/// Streaming digest and package-root integrity checks.
pub mod integrity;
/// Strict package manifests and verified artifact paths.
pub mod manifest;

/// Version shared by the launcher binary and distribution manifests.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
