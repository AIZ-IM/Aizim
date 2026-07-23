//! Native distribution launcher primitives for Aizim.

/// Launcher-owned argument parsing.
pub mod args;
/// Versioned cache lifecycle and atomic runtime promotion.
pub mod cache;
/// Stable, redacted launcher failures.
pub mod error;
/// Streaming digest and package-root integrity checks.
pub mod integrity;
/// Strict package manifests and verified artifact paths.
pub mod manifest;
/// Shell-free command and final Unix process specifications.
pub mod process;
/// Bundled-uv runtime provisioning.
pub mod provision;

/// Version shared by the launcher binary and distribution manifests.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
