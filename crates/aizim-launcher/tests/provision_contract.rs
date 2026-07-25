//! Integration contract for uv provisioning and final process replacement.

use std::error::Error;
use std::ffi::{OsStr, OsString};
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::process::ExitStatus;

use aizim_launcher::cache::{CacheEnvironment, CacheKey, CacheLayout};
use aizim_launcher::error::{ErrorKind, LauncherError};
use aizim_launcher::process::{CommandRunner, CommandSpec};
use aizim_launcher::provision::{ProvisionRequest, build_exec_spec, ensure_runtime};
use tempfile::TempDir;

type TestResult<T = ()> = Result<T, Box<dyn Error>>;

struct RequestFixture {
    _temporary: TempDir,
    request: ProvisionRequest,
}

#[derive(Debug, Default)]
struct FakeRunner {
    specs: Vec<CommandSpec>,
    fail_at: Option<usize>,
    wrong_version: bool,
    venv: Option<PathBuf>,
}

impl CommandRunner for FakeRunner {
    fn run(&mut self, spec: &CommandSpec) -> Result<ExitStatus, LauncherError> {
        use std::os::unix::process::ExitStatusExt;

        let index = self.specs.len();
        self.specs.push(spec.clone());
        if self.fail_at == Some(index) {
            return Ok(ExitStatus::from_raw(1 << 8));
        }
        if index == 1 {
            self.venv = spec.args.last().map(PathBuf::from);
        }
        if index == 3 {
            let venv = self
                .venv
                .as_deref()
                .ok_or_else(|| LauncherError::internal("TEST_VENV_MISSING"))?;
            let binaries = venv.join("bin");
            fs::create_dir_all(&binaries).map_err(test_launcher_error)?;
            let script = format!(
                "#!/bin/sh\nexec '{}' \"$@\"\n",
                binaries.join("python").display()
            );
            make_executable_with_content(&binaries.join("aizim"), script.as_bytes())
                .map_err(test_launcher_error)?;
            make_executable_with_content(
                &binaries.join("aizim-gateway-sidecar"),
                script.as_bytes(),
            )
            .map_err(test_launcher_error)?;
        }
        if index == 4 && self.wrong_version {
            return Err(LauncherError::unavailable("RUNTIME_VERSION_MISMATCH"));
        }
        Ok(ExitStatus::from_raw(0))
    }
}

fn test_error(message: impl Into<String>) -> Box<dyn Error> {
    Box::new(io::Error::other(message.into()))
}

fn test_launcher_error(source: io::Error) -> LauncherError {
    LauncherError::internal("TEST_FIXTURE_FAILED").with_source(source)
}

fn make_executable(path: &Path) -> io::Result<()> {
    make_executable_with_content(path, b"test")
}

fn make_executable_with_content(path: &Path, content: &[u8]) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;

    fs::write(path, content)?;
    let mut permissions = fs::metadata(path)?.permissions();
    permissions.set_mode(0o755);
    fs::set_permissions(path, permissions)
}

fn fixture() -> TestResult<RequestFixture> {
    let temporary = TempDir::new()?;
    let root = fs::canonicalize(temporary.path())?;
    let artifact_root = root.join("artifacts");
    fs::create_dir(&artifact_root)?;
    let uv = artifact_root.join("uv");
    let wheel = artifact_root.join("aizim.whl");
    let requirements = artifact_root.join("runtime-requirements.txt");
    make_executable(&uv)?;
    fs::write(&wheel, b"wheel")?;
    fs::write(&requirements, b"requirements")?;

    let key = CacheKey {
        aizim_version: "0.1.0".to_owned(),
        target: "darwin-arm64".to_owned(),
        wheel_sha256: "a".repeat(64),
        python_version: "3.14.6".to_owned(),
    };
    let layout = CacheLayout::new(
        &CacheEnvironment {
            platform: "darwin",
            home: None,
            xdg_cache_home: None,
            override_root: Some(root.join("cache")),
        },
        &key,
    )?;
    let request = ProvisionRequest {
        uv,
        wheel,
        runtime_requirements: requirements,
        layout,
        key,
        distribution_manifest_sha256: "d".repeat(64),
        platform_manifest_sha256: "e".repeat(64),
        inherited_environment: vec![
            (OsString::from("PATH"), OsString::from("/usr/bin")),
            (
                OsString::from("HTTPS_PROXY"),
                OsString::from("https://secret@example.test"),
            ),
            (OsString::from("UV_INDEX_URL"), OsString::from("secret")),
            (OsString::from("PIP_INDEX_URL"), OsString::from("secret")),
            (OsString::from("PYTHONPATH"), OsString::from("/injected")),
            (OsString::from("PYTHONHOME"), OsString::from("/injected")),
            (OsString::from("VIRTUAL_ENV"), OsString::from("/injected")),
            (
                OsString::from("AIZIM_DISTRIBUTION_VERSION"),
                OsString::from("wrong"),
            ),
            (
                OsString::from("AIZIM_CLAUDE_EXECUTABLE"),
                OsString::from("/inherited/claude"),
            ),
            (
                OsString::from("AIZIM_CODEX_EXECUTABLE"),
                OsString::from("/inherited/codex"),
            ),
        ],
        temporary_variable: "TMPDIR",
        cwd: root,
    };
    Ok(RequestFixture {
        _temporary: temporary,
        request,
    })
}

fn strings(values: &[OsString]) -> Vec<String> {
    values
        .iter()
        .map(|value| value.to_string_lossy().into_owned())
        .collect()
}

fn environment_value<'a>(spec: &'a CommandSpec, name: &str) -> Option<&'a OsStr> {
    spec.env
        .iter()
        .find(|(key, _)| key == OsStr::new(name))
        .map(|(_, value)| value.as_os_str())
}

fn expected_failure<T>(result: Result<T, LauncherError>) -> TestResult<LauncherError> {
    result
        .err()
        .ok_or_else(|| test_error("expected provisioning to fail"))
}

fn expected_arguments(fixture: &RequestFixture, staging: &Path) -> [Vec<String>; 5] {
    let python = staging
        .join("venv/bin/python")
        .to_string_lossy()
        .into_owned();
    [
        vec![
            "--no-config".to_owned(),
            "python".to_owned(),
            "install".to_owned(),
            "--install-dir".to_owned(),
            fixture
                .request
                .layout
                .managed_python_root
                .to_string_lossy()
                .into_owned(),
            "3.14.6".to_owned(),
        ],
        vec![
            "--no-config".to_owned(),
            "venv".to_owned(),
            "--managed-python".to_owned(),
            "--python".to_owned(),
            "3.14.6".to_owned(),
            staging.join("venv").to_string_lossy().into_owned(),
        ],
        vec![
            "--no-config".to_owned(),
            "pip".to_owned(),
            "install".to_owned(),
            "--python".to_owned(),
            python.clone(),
            "--require-hashes".to_owned(),
            "--no-deps".to_owned(),
            "--default-index".to_owned(),
            "https://pypi.org/simple".to_owned(),
            "-r".to_owned(),
            fixture
                .request
                .runtime_requirements
                .to_string_lossy()
                .into_owned(),
        ],
        vec![
            "--no-config".to_owned(),
            "pip".to_owned(),
            "install".to_owned(),
            "--python".to_owned(),
            python,
            "--no-deps".to_owned(),
            fixture.request.wheel.to_string_lossy().into_owned(),
        ],
        vec!["--version".to_owned()],
    ]
}

fn assert_uv_environment(fixture: &RequestFixture, spec: &CommandSpec) {
    let layout = &fixture.request.layout;
    assert_eq!(spec.program, fixture.request.uv);
    assert_eq!(
        environment_value(spec, "UV_CACHE_DIR"),
        Some(layout.uv_cache_root.as_os_str())
    );
    assert_eq!(
        environment_value(spec, "UV_PYTHON_INSTALL_DIR"),
        Some(layout.managed_python_root.as_os_str())
    );
    assert_eq!(
        environment_value(spec, "UV_NO_PROGRESS"),
        Some(OsStr::new("1"))
    );
    assert!(environment_value(spec, "UV_INDEX_URL").is_none());
    assert!(environment_value(spec, "PIP_INDEX_URL").is_none());
    assert!(environment_value(spec, "PYTHONPATH").is_none());
    assert_eq!(
        environment_value(spec, "HTTPS_PROXY"),
        Some(OsStr::new("https://secret@example.test"))
    );
}

#[test]
fn cold_bootstrap_uses_the_exact_command_sequence_and_clean_environment() -> TestResult {
    let fixture = fixture()?;
    let mut runner = FakeRunner::default();
    let mut status = Vec::new();
    ensure_runtime(&fixture.request, &mut runner, &mut status)?;

    assert_eq!(runner.specs.len(), 5);
    let staging_venv = runner
        .specs
        .get(1)
        .and_then(|spec| spec.args.last())
        .map(PathBuf::from)
        .ok_or_else(|| test_error("venv command has no staging path"))?;
    let staging = staging_venv
        .parent()
        .ok_or_else(|| test_error("staging venv has no parent"))?;
    for (spec, expected_args) in runner
        .specs
        .iter()
        .zip(expected_arguments(&fixture, staging))
    {
        assert_eq!(strings(&spec.args), expected_args);
        assert!(spec.env_clear);
    }
    for spec in runner.specs.iter().take(4) {
        assert_uv_environment(&fixture, spec);
    }
    assert!(
        fixture
            .request
            .layout
            .runtime_root
            .join("READY.json")
            .is_file()
    );
    let output = String::from_utf8(status)?;
    let fingerprint = fixture.request.key.fingerprint(1);
    assert_eq!(
        output,
        format!(
            "aizim: provisioning managed Python 3.14.6 runtime ({fingerprint})\n\
             aizim: runtime ready ({fingerprint})\n"
        )
    );
    assert!(!output.contains("secret"));
    Ok(())
}

#[test]
fn warm_runtime_runs_no_commands_and_emits_no_bootstrap_status() -> TestResult {
    let fixture = fixture()?;
    let mut cold_runner = FakeRunner::default();
    ensure_runtime(&fixture.request, &mut cold_runner, &mut Vec::new())?;

    let mut warm_runner = FakeRunner::default();
    let mut status = Vec::new();
    ensure_runtime(&fixture.request, &mut warm_runner, &mut status)?;
    assert!(warm_runner.specs.is_empty());
    assert!(status.is_empty());
    Ok(())
}

#[test]
fn promoted_console_scripts_reference_the_final_runtime() -> TestResult {
    let fixture = fixture()?;
    let mut runner = FakeRunner::default();
    ensure_runtime(&fixture.request, &mut runner, &mut Vec::new())?;

    let staging = runner
        .specs
        .get(1)
        .and_then(|spec| spec.args.last())
        .map(PathBuf::from)
        .and_then(|venv| venv.parent().map(Path::to_path_buf))
        .ok_or_else(|| test_error("venv command has no staging path"))?;
    for name in ["aizim", "aizim-gateway-sidecar"] {
        let script = fs::read(
            fixture
                .request
                .layout
                .runtime_root
                .join("venv/bin")
                .join(name),
        )?;
        assert!(
            !script
                .windows(staging.as_os_str().len())
                .any(|window| { window == staging.as_os_str().to_string_lossy().as_bytes() })
        );
        assert!(
            script
                .windows(fixture.request.layout.runtime_root.as_os_str().len())
                .any(|window| {
                    window
                        == fixture
                            .request
                            .layout
                            .runtime_root
                            .as_os_str()
                            .to_string_lossy()
                            .as_bytes()
                })
        );
    }
    Ok(())
}

#[test]
fn every_command_failure_leaves_no_final_or_staging_runtime() -> TestResult {
    for fail_at in 0..5 {
        let fixture = fixture()?;
        let mut runner = FakeRunner {
            fail_at: Some(fail_at),
            ..FakeRunner::default()
        };
        let error = expected_failure(ensure_runtime(
            &fixture.request,
            &mut runner,
            &mut Vec::new(),
        ))?;
        assert_eq!(error.kind, ErrorKind::Unavailable);
        assert!(!fixture.request.layout.runtime_root.exists());
        let runtime_parent = fixture
            .request
            .layout
            .runtime_root
            .parent()
            .ok_or_else(|| test_error("runtime has no parent"))?;
        assert_eq!(fs::read_dir(runtime_parent)?.count(), 1);
    }
    Ok(())
}

#[test]
fn wrong_version_is_unavailable_and_not_promoted() -> TestResult {
    let fixture = fixture()?;
    let mut runner = FakeRunner {
        wrong_version: true,
        ..FakeRunner::default()
    };
    let error = expected_failure(ensure_runtime(
        &fixture.request,
        &mut runner,
        &mut Vec::new(),
    ))?;
    assert_eq!(error.kind, ErrorKind::Unavailable);
    assert_eq!(error.exit_code(), 69);
    assert!(!fixture.request.layout.runtime_root.exists());
    assert!(!error.to_string().contains("secret"));
    Ok(())
}

#[cfg(unix)]
#[test]
fn final_exec_preserves_opaque_args_and_sets_the_closed_distribution_context() -> TestResult {
    use std::os::unix::ffi::{OsStrExt, OsStringExt};

    let fixture = fixture()?;
    let mut runner = FakeRunner::default();
    let ready = ensure_runtime(&fixture.request, &mut runner, &mut Vec::new())?;
    let opaque = OsString::from_vec(vec![b'x', 0xff, b'y']);
    let spec = build_exec_spec(
        &fixture.request,
        &ready,
        &[opaque.clone(), OsString::from("--hostile")],
    )?;

    assert_eq!(spec.program, ready.aizim);
    assert_eq!(
        spec.args
            .first()
            .ok_or_else(|| test_error("missing opaque argument"))?
            .as_bytes(),
        opaque.as_bytes()
    );
    assert_eq!(
        environment_value(&spec, "AIZIM_DISTRIBUTION_MODE"),
        Some(OsStr::new("npm"))
    );
    assert_eq!(
        environment_value(&spec, "AIZIM_DISTRIBUTION_VERSION"),
        Some(OsStr::new("0.1.0"))
    );
    assert_eq!(
        environment_value(&spec, "AIZIM_DISTRIBUTION_TARGET"),
        Some(OsStr::new("darwin-arm64"))
    );
    assert_eq!(
        environment_value(&spec, "AIZIM_CLAUDE_EXECUTABLE"),
        Some(OsStr::new("/inherited/claude"))
    );
    assert_eq!(
        environment_value(&spec, "AIZIM_CODEX_EXECUTABLE"),
        Some(OsStr::new("/inherited/codex"))
    );
    assert_eq!(
        environment_value(&spec, "AIZIM_DISTRIBUTION_MANIFEST_SHA256"),
        Some(OsStr::new(&"d".repeat(64)))
    );
    assert_eq!(
        environment_value(&spec, "AIZIM_PLATFORM_MANIFEST_SHA256"),
        Some(OsStr::new(&"e".repeat(64)))
    );
    for forbidden in [
        "UV_INDEX_URL",
        "PIP_INDEX_URL",
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
    ] {
        assert!(environment_value(&spec, forbidden).is_none());
    }
    assert_eq!(
        environment_value(&spec, "PATH"),
        Some(OsStr::new("/usr/bin"))
    );
    Ok(())
}

#[test]
fn final_exec_does_not_require_bundled_provider_artifacts() -> TestResult {
    let fixture = fixture()?;
    let mut runner = FakeRunner::default();
    let ready = ensure_runtime(&fixture.request, &mut runner, &mut Vec::new())?;

    let spec = build_exec_spec(&fixture.request, &ready, &[])?;
    assert_eq!(spec.program, ready.aizim);
    Ok(())
}
