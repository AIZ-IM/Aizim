//! Integration contract for versioned cache lifecycle ownership.

use std::error::Error;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Barrier, mpsc};
use std::thread;
use std::time::Duration;

use aizim_launcher::cache::{
    CACHE_SCHEMA_VERSION, CacheEnvironment, CacheKey, CacheLayout, ReadyMarker, RuntimeLock,
};
use tempfile::TempDir;

type TestResult<T = ()> = Result<T, Box<dyn Error + Send + Sync>>;

fn test_error(message: impl Into<String>) -> Box<dyn Error + Send + Sync> {
    Box::new(io::Error::other(message.into()))
}

fn canonical(path: &Path) -> TestResult<PathBuf> {
    Ok(fs::canonicalize(path)?)
}

fn key() -> CacheKey {
    CacheKey {
        aizim_version: "0.1.0".to_owned(),
        target: "darwin-arm64".to_owned(),
        wheel_sha256: "a".repeat(64),
        python_version: "3.14.6".to_owned(),
    }
}

fn environment(root: &Path) -> CacheEnvironment {
    CacheEnvironment {
        platform: "darwin",
        home: None,
        xdg_cache_home: None,
        override_root: Some(root.to_path_buf()),
    }
}

fn layout(temporary: &TempDir) -> TestResult<CacheLayout> {
    Ok(CacheLayout::new(
        &environment(&canonical(temporary.path())?),
        &key(),
    )?)
}

fn make_executable(path: &Path) -> TestResult {
    use std::os::unix::fs::PermissionsExt;

    fs::write(path, b"test")?;
    let mut permissions = fs::metadata(path)?.permissions();
    permissions.set_mode(0o755);
    fs::set_permissions(path, permissions)?;
    Ok(())
}

fn ready_marker() -> ReadyMarker {
    ReadyMarker {
        schema_version: CACHE_SCHEMA_VERSION,
        aizim_version: "0.1.0".to_owned(),
        target: "darwin-arm64".to_owned(),
        wheel_sha256: "a".repeat(64),
        python_version: "3.14.6".to_owned(),
        aizim_entrypoint: "venv/bin/aizim".to_owned(),
        sidecar_entrypoint: "venv/bin/aizim-gateway-sidecar".to_owned(),
    }
}

fn complete_staging(layout: &CacheLayout) -> TestResult<PathBuf> {
    let staging = layout.create_staging()?;
    let binaries = staging.join("venv").join("bin");
    fs::create_dir_all(&binaries)?;
    make_executable(&binaries.join("aizim"))?;
    make_executable(&binaries.join("aizim-gateway-sidecar"))?;
    layout.write_ready(&staging, &ready_marker())?;
    Ok(staging)
}

#[test]
fn computes_platform_cache_roots_and_override() -> TestResult {
    let cases = [
        (
            CacheEnvironment {
                platform: "darwin",
                home: Some(PathBuf::from("/Users/tester")),
                xdg_cache_home: None,
                override_root: None,
            },
            PathBuf::from("/Users/tester/Library/Caches/aizim"),
        ),
        (
            CacheEnvironment {
                platform: "linux",
                home: Some(PathBuf::from("/home/tester")),
                xdg_cache_home: Some(PathBuf::from("/cache")),
                override_root: None,
            },
            PathBuf::from("/cache/aizim"),
        ),
        (
            CacheEnvironment {
                platform: "linux",
                home: Some(PathBuf::from("/home/tester")),
                xdg_cache_home: None,
                override_root: None,
            },
            PathBuf::from("/home/tester/.cache/aizim"),
        ),
        (
            CacheEnvironment {
                platform: "linux",
                home: None,
                xdg_cache_home: None,
                override_root: Some(PathBuf::from("/custom/cache")),
            },
            PathBuf::from("/custom/cache"),
        ),
    ];

    for (input, expected) in cases {
        assert_eq!(input.root_path()?, expected);
    }
    Ok(())
}

#[test]
fn rejects_relative_missing_symlink_and_non_directory_roots() -> TestResult {
    let relative = CacheEnvironment {
        platform: "linux",
        home: None,
        xdg_cache_home: None,
        override_root: Some(PathBuf::from("relative")),
    };
    assert!(relative.root_path().is_err());

    let missing_home = CacheEnvironment {
        platform: "darwin",
        home: None,
        xdg_cache_home: None,
        override_root: None,
    };
    assert!(missing_home.root_path().is_err());

    let temporary = TempDir::new()?;
    let canonical_root = canonical(temporary.path())?;
    let real_directory = canonical_root.join("real");
    fs::create_dir(&real_directory)?;
    let linked_directory = canonical_root.join("linked");
    std::os::unix::fs::symlink(&real_directory, &linked_directory)?;
    assert!(CacheLayout::new(&environment(&linked_directory), &key()).is_err());

    let file_root = canonical_root.join("file");
    fs::write(&file_root, b"not a directory")?;
    assert!(CacheLayout::new(&environment(&file_root), &key()).is_err());
    Ok(())
}

#[test]
fn cache_path_changes_for_every_identity_input_and_schema() -> TestResult {
    let temporary = TempDir::new()?;
    let root = canonical(temporary.path())?;
    let base = key();
    let baseline = CacheLayout::with_schema(&environment(&root), &base, 1)?.runtime_root;
    let variants = [
        CacheKey {
            aizim_version: "0.2.0".to_owned(),
            ..base.clone()
        },
        CacheKey {
            target: "darwin-x64".to_owned(),
            ..base.clone()
        },
        CacheKey {
            wheel_sha256: "b".repeat(64),
            ..base.clone()
        },
        CacheKey {
            python_version: "3.14.5".to_owned(),
            ..base.clone()
        },
    ];
    for variant in variants {
        assert_ne!(
            CacheLayout::with_schema(&environment(&root), &variant, 1)?.runtime_root,
            baseline
        );
    }
    assert_ne!(
        CacheLayout::with_schema(&environment(&root), &base, 2)?.runtime_root,
        baseline
    );
    assert_eq!(
        CacheLayout::with_schema(&environment(&root), &base, 1)?.runtime_root,
        baseline
    );
    Ok(())
}

#[test]
fn exclusive_lock_is_os_backed_and_persistent_file_is_reusable() -> TestResult {
    let temporary = TempDir::new()?;
    let layout = layout(&temporary)?;
    let first = RuntimeLock::acquire(&layout.lock_path)?;
    let barrier = Arc::new(Barrier::new(2));
    let child_barrier = Arc::clone(&barrier);
    let child_lock_path = layout.lock_path.clone();
    let (sender, receiver) = mpsc::channel();
    let handle = thread::spawn(move || -> TestResult {
        child_barrier.wait();
        sender.send("attempting")?;
        let second = RuntimeLock::acquire(&child_lock_path)?;
        sender.send("acquired")?;
        drop(second);
        Ok(())
    });

    barrier.wait();
    assert_eq!(receiver.recv_timeout(Duration::from_secs(1))?, "attempting");
    assert!(receiver.recv_timeout(Duration::from_millis(100)).is_err());
    drop(first);
    assert_eq!(receiver.recv_timeout(Duration::from_secs(1))?, "acquired");
    handle
        .join()
        .map_err(|_| test_error("lock test thread panicked"))??;

    let persistent = RuntimeLock::acquire(&layout.lock_path)?;
    drop(persistent);
    assert!(layout.lock_path.is_file());
    Ok(())
}

#[test]
fn ready_runtime_is_reused_without_mutation_and_invalid_marker_is_not() -> TestResult {
    let temporary = TempDir::new()?;
    let layout = layout(&temporary)?;
    let staging = complete_staging(&layout)?;
    let promoted = layout.promote(&staging, &key())?;
    let marker_path = layout.runtime_root.join("READY.json");
    let before = fs::metadata(&marker_path)?.modified()?;

    let reused = layout
        .ready_runtime(&key())?
        .ok_or_else(|| test_error("ready runtime was not reused"))?;
    assert_eq!(reused.aizim, promoted.aizim);
    assert_eq!(fs::metadata(&marker_path)?.modified()?, before);

    fs::write(&marker_path, b"{}")?;
    assert!(layout.ready_runtime(&key())?.is_none());
    Ok(())
}

#[test]
fn promotion_never_overwrites_a_complete_environment() -> TestResult {
    let temporary = TempDir::new()?;
    let layout = layout(&temporary)?;
    let first_staging = complete_staging(&layout)?;
    let first = layout.promote(&first_staging, &key())?;
    let first_contents = fs::read(&first.aizim)?;

    let second_staging = complete_staging(&layout)?;
    fs::write(
        second_staging.join("venv").join("bin").join("aizim"),
        b"changed",
    )?;
    let reused = layout.promote(&second_staging, &key())?;
    assert_eq!(reused.aizim, first.aizim);
    assert_eq!(fs::read(&first.aizim)?, first_contents);
    assert!(!second_staging.exists());
    Ok(())
}

#[test]
fn staging_cleanup_is_scoped_to_the_current_leaf_prefix() -> TestResult {
    let temporary = TempDir::new()?;
    let layout = layout(&temporary)?;
    let stale = layout.create_staging()?;
    let parent = stale
        .parent()
        .ok_or_else(|| test_error("staging directory has no parent"))?;
    let unrelated = parent.join(".other.staging-1-1");
    let older_complete = parent.join("older-complete");
    fs::create_dir(&unrelated)?;
    fs::create_dir(&older_complete)?;

    layout.cleanup_staging()?;
    assert!(!stale.exists());
    assert!(unrelated.is_dir());
    assert!(older_complete.is_dir());
    Ok(())
}
