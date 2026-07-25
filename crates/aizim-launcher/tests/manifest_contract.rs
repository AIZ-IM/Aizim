//! Integration contract for the native manifest trust boundary.

use std::error::Error;
use std::ffi::OsString;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use aizim_launcher::args::LauncherArgs;
use aizim_launcher::error::{ErrorKind, LauncherError};
use aizim_launcher::manifest::VerifiedDistribution;
use serde_json::{Value, json};
use tempfile::TempDir;

const TEST_SHA256: &str = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08";
type TestResult<T = ()> = Result<T, Box<dyn Error>>;

struct Fixture {
    temporary: TempDir,
    args: LauncherArgs,
    distribution_json: Value,
    platform_json: Value,
    wheel: PathBuf,
    requirements: PathBuf,
    uv: PathBuf,
}

fn test_error(message: impl Into<String>) -> Box<dyn Error> {
    Box::new(io::Error::other(message.into()))
}

fn parent(path: &Path) -> TestResult<&Path> {
    path.parent()
        .ok_or_else(|| test_error("test fixture path has no parent"))
}

fn field_mut<'a>(value: &'a mut Value, fields: &[&str]) -> TestResult<&'a mut Value> {
    let mut current = value;
    for field in fields {
        current = current
            .get_mut(field)
            .ok_or_else(|| test_error(format!("missing fixture field {field}")))?;
    }
    Ok(current)
}

fn expected_failure<T>(result: Result<T, LauncherError>) -> TestResult<LauncherError> {
    result
        .err()
        .ok_or_else(|| test_error("expected launcher operation to fail"))
}

fn host_target() -> TestResult<(
    &'static str,
    &'static str,
    &'static str,
    &'static str,
    Value,
)> {
    match (std::env::consts::OS, std::env::consts::ARCH) {
        ("macos", "aarch64") => Ok((
            "darwin-arm64",
            "darwin",
            "arm64",
            "aarch64-apple-darwin",
            Value::Null,
        )),
        ("linux", "aarch64") => Ok((
            "linux-arm64",
            "linux",
            "arm64",
            "aarch64-unknown-linux-gnu",
            json!("glibc"),
        )),
        ("linux", "x86_64") => Ok((
            "linux-x64",
            "linux",
            "x64",
            "x86_64-unknown-linux-gnu",
            json!("glibc"),
        )),
        (os, arch) => Err(test_error(format!("unsupported test host {os}/{arch}"))),
    }
}

fn write_json(path: &Path, value: &Value) -> TestResult {
    fs::write(path, serde_json::to_vec(value)?)?;
    Ok(())
}

fn fixture() -> TestResult<Fixture> {
    let temporary = TempDir::new()?;
    let meta_root = temporary.path().join("meta");
    let platform_root = temporary.path().join("platform");
    let wheel = meta_root
        .join("vendor")
        .join("aizim-0.1.0-py3-none-any.whl");
    let requirements = meta_root.join("vendor").join("runtime-requirements.txt");
    let uv = platform_root.join("vendor").join("uv");
    let distribution_manifest = meta_root.join("manifest").join("distribution.json");
    let platform_manifest = platform_root.join("manifest").join("platform.json");

    fs::create_dir_all(parent(&wheel)?)?;
    fs::create_dir_all(parent(&uv)?)?;
    fs::create_dir_all(parent(&distribution_manifest)?)?;
    fs::create_dir_all(parent(&platform_manifest)?)?;
    for path in [&wheel, &requirements, &uv] {
        fs::write(path, b"test")?;
    }
    make_executable(&uv)?;

    let (target, node_platform, node_arch, rust_target, libc) = host_target()?;
    let distribution_json = json!({
        "schema_version": 3,
        "aizim_version": "0.1.0",
        "python_version": "3.14.6",
        "wheel": {
            "path": "vendor/aizim-0.1.0-py3-none-any.whl",
            "size": 4,
            "sha256": TEST_SHA256
        },
        "runtime_requirements": {
            "path": "vendor/runtime-requirements.txt",
            "size": 4,
            "sha256": TEST_SHA256
        },
        "minimum_node_version": "22.22.2",
        "platform_schema_version": 1
    });
    let platform_json = json!({
        "schema_version": 1,
        "aizim_version": "0.1.0",
        "package_name": format!("@aiz.im/aizim-{target}"),
        "target": target,
        "node_platform": node_platform,
        "node_arch": node_arch,
        "rust_target": rust_target,
        "libc": libc,
        "launcher_version": "0.1.0",
        "uv_version": "0.11.31",
        "uv": {
            "path": "vendor/uv",
            "size": 4,
            "sha256": TEST_SHA256
        },
        "distribution_schema_version": 3
    });
    write_json(&distribution_manifest, &distribution_json)?;
    write_json(&platform_manifest, &platform_json)?;

    Ok(Fixture {
        temporary,
        args: LauncherArgs {
            distribution_manifest,
            platform_manifest,
            user_args: Vec::new(),
        },
        distribution_json,
        platform_json,
        wheel,
        requirements,
        uv,
    })
}

#[cfg(unix)]
fn make_executable(path: &Path) -> TestResult {
    use std::os::unix::fs::PermissionsExt;

    let mut permissions = fs::metadata(path)?.permissions();
    permissions.set_mode(0o755);
    fs::set_permissions(path, permissions)?;
    Ok(())
}

#[test]
fn parses_closed_internal_arguments_and_preserves_user_bytes() -> TestResult {
    #[cfg(unix)]
    use std::os::unix::ffi::{OsStrExt, OsStringExt};

    let fixture = fixture()?;
    #[cfg(unix)]
    let opaque = OsString::from_vec(vec![b'x', 0xff, b'y']);
    #[cfg(not(unix))]
    let opaque = OsString::from("opaque");
    let parsed = LauncherArgs::parse([
        OsString::from("--distribution-manifest"),
        fixture.args.distribution_manifest.into_os_string(),
        OsString::from("--platform-manifest"),
        fixture.args.platform_manifest.into_os_string(),
        OsString::from("--"),
        opaque.clone(),
        OsString::from("--hostile"),
    ])?;

    let first = parsed
        .user_args
        .first()
        .ok_or_else(|| test_error("missing first user argument"))?;
    let second = parsed
        .user_args
        .get(1)
        .ok_or_else(|| test_error("missing second user argument"))?;
    #[cfg(unix)]
    assert_eq!(first.as_bytes(), opaque.as_bytes());
    assert_eq!(second, &OsString::from("--hostile"));
    Ok(())
}

#[test]
fn rejects_missing_duplicate_unknown_and_relative_internal_arguments() -> TestResult {
    let fixture = fixture()?;
    let valid = vec![
        OsString::from("--distribution-manifest"),
        fixture.args.distribution_manifest.clone().into_os_string(),
        OsString::from("--platform-manifest"),
        fixture.args.platform_manifest.clone().into_os_string(),
    ];
    let no_separator = valid.clone();
    let duplicate = [
        valid.clone(),
        vec![
            OsString::from("--platform-manifest"),
            fixture.args.platform_manifest.clone().into_os_string(),
            OsString::from("--"),
        ],
    ]
    .concat();
    let unknown = [
        vec![OsString::from("--unknown")],
        valid,
        vec![OsString::from("--")],
    ]
    .concat();
    let relative = vec![
        OsString::from("--distribution-manifest"),
        OsString::from("relative.json"),
        OsString::from("--platform-manifest"),
        fixture.args.platform_manifest.into_os_string(),
        OsString::from("--"),
    ];

    for input in [no_separator, duplicate, unknown, relative] {
        let error = expected_failure(LauncherArgs::parse(input))?;
        assert_eq!(error.kind, ErrorKind::Configuration);
        assert_eq!(error.exit_code(), 78);
    }
    Ok(())
}

#[test]
fn verifies_valid_manifests_and_returns_canonical_artifacts() -> TestResult {
    let fixture = fixture()?;
    let verified = VerifiedDistribution::load(&fixture.args)?;

    assert_eq!(verified.wheel, fs::canonicalize(fixture.wheel)?);
    assert_eq!(
        verified.runtime_requirements,
        fs::canonicalize(fixture.requirements)?
    );
    assert_eq!(verified.uv, fs::canonicalize(fixture.uv)?);
    Ok(())
}

#[cfg(unix)]
#[test]
fn rejects_a_non_executable_bundled_uv_with_a_neutral_code() -> TestResult {
    use std::os::unix::fs::PermissionsExt;

    let fixture = fixture()?;
    let mut permissions = fs::metadata(&fixture.uv)?.permissions();
    permissions.set_mode(0o644);
    fs::set_permissions(&fixture.uv, permissions)?;

    let error = expected_failure(VerifiedDistribution::load(&fixture.args))?;
    assert_eq!(error.kind, ErrorKind::Integrity);
    assert_eq!(error.code, "EXECUTABLE_INTEGRITY_FAILED");
    assert!(!error.to_string().contains("Codex"));
    Ok(())
}

#[test]
fn rejects_unknown_manifest_fields() -> TestResult {
    let mut fixture = fixture()?;
    fixture
        .distribution_json
        .as_object_mut()
        .ok_or_else(|| test_error("distribution fixture is not an object"))?
        .insert("unexpected".to_owned(), json!(true));
    write_json(
        &fixture.args.distribution_manifest,
        &fixture.distribution_json,
    )?;

    let error = expected_failure(VerifiedDistribution::load(&fixture.args))?;
    assert_eq!(error.kind, ErrorKind::Configuration);
    Ok(())
}

#[test]
fn rejects_schema_version_and_closed_target_mismatches() -> TestResult {
    for (document, field, value) in [
        ("distribution", "schema_version", json!(2)),
        ("distribution", "aizim_version", json!("9.9.9")),
        ("platform", "distribution_schema_version", json!(2)),
        ("platform", "package_name", json!("@aiz.im/wrong")),
        ("platform", "node_platform", json!("win32")),
        ("platform", "node_arch", json!("ia32")),
        ("platform", "rust_target", json!("wrong-target")),
        ("platform", "libc", json!("musl")),
    ] {
        let mut fixture = fixture()?;
        let manifest = if document == "distribution" {
            &mut fixture.distribution_json
        } else {
            &mut fixture.platform_json
        };
        *field_mut(manifest, &[field])? = value;
        let path = if document == "distribution" {
            &fixture.args.distribution_manifest
        } else {
            &fixture.args.platform_manifest
        };
        write_json(path, manifest)?;

        let error = expected_failure(VerifiedDistribution::load(&fixture.args))?;
        assert_eq!(error.kind, ErrorKind::Configuration, "{document}.{field}");
        assert_eq!(error.exit_code(), 78);
    }
    Ok(())
}

#[test]
fn version_mismatch_diagnostic_is_safe_and_actionable() -> TestResult {
    let mut fixture = fixture()?;
    *field_mut(&mut fixture.distribution_json, &["aizim_version"])? = json!("9.9.9");
    write_json(
        &fixture.args.distribution_manifest,
        &fixture.distribution_json,
    )?;

    let error = expected_failure(VerifiedDistribution::load(&fixture.args))?;
    let diagnostic = error.to_string();
    assert!(diagnostic.contains("DISTRIBUTION_VERSION_MISMATCH"));
    assert!(diagnostic.contains("expected=0.1.0"));
    assert!(diagnostic.contains("observed=9.9.9"));
    assert!(diagnostic.contains("remediation="));
    assert!(!diagnostic.contains(fixture.temporary.path().to_string_lossy().as_ref()));
    assert!(!diagnostic.contains("source"));
    Ok(())
}

#[test]
fn rejects_absolute_and_parent_artifact_paths_as_configuration() -> TestResult {
    for artifact_path in ["/tmp/aizim.whl", "../aizim.whl"] {
        let mut fixture = fixture()?;
        *field_mut(&mut fixture.distribution_json, &["wheel", "path"])? = json!(artifact_path);
        write_json(
            &fixture.args.distribution_manifest,
            &fixture.distribution_json,
        )?;

        let error = expected_failure(VerifiedDistribution::load(&fixture.args))?;
        assert_eq!(error.kind, ErrorKind::Configuration);
        assert_eq!(error.exit_code(), 78);
    }
    Ok(())
}

#[test]
fn rejects_size_hash_and_non_regular_artifacts_as_integrity_failures() -> TestResult {
    for mutation in ["size", "hash", "directory"] {
        let mut fixture = fixture()?;
        if mutation == "size" {
            *field_mut(&mut fixture.distribution_json, &["wheel", "size"])? = json!(5);
        } else if mutation == "hash" {
            *field_mut(&mut fixture.distribution_json, &["wheel", "sha256"])? =
                json!("0".repeat(64));
        } else {
            let directory = parent(&fixture.wheel)?.join("directory");
            fs::create_dir(&directory)?;
            *field_mut(&mut fixture.distribution_json, &["wheel", "path"])? =
                json!("vendor/directory");
        }
        write_json(
            &fixture.args.distribution_manifest,
            &fixture.distribution_json,
        )?;

        let error = expected_failure(VerifiedDistribution::load(&fixture.args))?;
        assert_eq!(error.kind, ErrorKind::Integrity, "{mutation}");
        assert_eq!(error.exit_code(), 74);
    }
    Ok(())
}

#[cfg(unix)]
#[test]
fn rejects_an_artifact_symlink_escape() -> TestResult {
    use std::os::unix::fs::symlink;

    let mut fixture = fixture()?;
    let outside = fixture.temporary.path().join("outside");
    fs::write(&outside, b"test")?;
    let linked = parent(&fixture.wheel)?.join("linked");
    symlink(&outside, &linked)?;
    *field_mut(&mut fixture.distribution_json, &["wheel", "path"])? = json!("vendor/linked");
    write_json(
        &fixture.args.distribution_manifest,
        &fixture.distribution_json,
    )?;

    let error = expected_failure(VerifiedDistribution::load(&fixture.args))?;
    assert_eq!(error.kind, ErrorKind::Integrity);
    assert_eq!(error.exit_code(), 74);
    Ok(())
}
