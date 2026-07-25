# External Controller Providers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Aizim consume explicitly selected, user-managed Controller CLIs while keeping Workers and sandbox enforcement Codex-only, removing all agent packages from the npm/Rust distribution boundary, and preserving fail-closed runtime evidence.

**Architecture:** A syntax-only `ControllerProviderId` is shared by state and orchestration. An immutable built-in registry maps supported IDs to Controller adapters. Each top-level operation creates one `ResolvedControllerRuntime` containing canonical executable path, tested version, and SHA-256 descriptors; the supervisor injects that snapshot into Controller, Worker, preflight, and Codex sandbox construction. The npm/Rust launcher continues to attest only Aizim-owned artifacts and passes the user's provider overrides and `PATH` through unchanged.

**Tech Stack:** Python 3.12-3.14 with uv, pytest, ty, and ruff; Node.js ESM with `node:test` and npm 12; Rust 1.97 with Cargo; GitHub Actions; Lean 4.32.1 integration fixtures.

## Global Constraints

- The approved design is
  `docs/superpowers/specs/2026-07-25-external-controller-providers-design.md`.
- Aizim must have no dependency, optional dependency, or peer dependency on
  `@openai/codex` or `@anthropic-ai/claude-code`.
- Controller selection is explicit and persisted. There is no default,
  autodetection, provider fallback, or automatic install.
- The built-in Controller registry initially contains only `codex` and
  `claude`, but state accepts every identifier matching
  `[a-z][a-z0-9_-]{0,63}`.
- Workers and sandbox enforcement remain Codex-only. Do not introduce a Worker
  provider or sandbox-provider registry.
- `AIZIM_CODEX_EXECUTABLE` and `AIZIM_CLAUDE_EXECUTABLE` are independent,
  optional overrides. A non-empty override wins over `PATH`; an empty or absent
  override falls through to `PATH`.
- The initial tested allowlists are exactly `codex-cli 0.145.0` and
  `2.1.218 (Claude Code)`. Unsupported errors must include the canonical path,
  observed version, supported versions, and side-by-side override guidance.
- Runtime executable evidence is TOFU for one operation/backend lifetime, not
  supply-chain attestation. Every descriptor contains a canonical path,
  observed version, and SHA-256, and every launch revalidates it.
- Preserve existing failure codes. Add only
  `CONTROLLER_PROVIDER_UNSUPPORTED`; do not add generic
  `WORKER_CODEX_EXECUTABLE_UNAVAILABLE` or `SANDBOX_UNAVAILABLE`.
- `doctor` uses fixed role IDs and `PASS | FAIL | SKIP`; provider names appear
  only in details.
- The Rust launcher must continue to verify manifests, wheel, locked
  requirements, uv, and the managed Python runtime.
- Use temporary fake executables in offline tests. Tests must not accidentally
  borrow globally installed Codex, Claude, `rg`, or `bwrap`.
- Use `uv` for Python dependency and test commands.
- Preserve the pre-existing untracked `.omo/` directory. Never stage or delete
  it.
- Correct the already-pushed bundled-provider commits with forward commits.
  Do not rewrite or force-push history.
- Follow red-green-refactor in every task: add or update a focused test, run it
  and observe the intended failure, make the smallest implementation change,
  rerun it, then commit.

## File Structure

### New files

- `src/aizim/domain/controller_provider.py`
  - syntax-only `ControllerProviderId` and shared identifier validator.
- `src/aizim/runtime/provider_executables.py`
  - immutable executable descriptors, external resolution, tested-version
    probes, descriptor revalidation, and Codex-adjacent runtime asset lookup.
- `src/aizim/orchestration/controller_providers.py`
  - immutable built-in Controller adapter registry and
    `ResolvedControllerRuntime`.
- `tests/unit/test_controller_provider_registry.py`
  - identifier, registry, unsupported-provider, and data-only isolation
    coverage.
- `tests/unit/test_provider_executables.py`
  - override/PATH precedence, version evidence, SHA-256, and Codex-adjacent
    layout coverage.
- `tests/integration/test_provider_contract.py`
  - no-credential real-CLI contract driver used by provider CI jobs.
- `tests/fixtures/npm-bundled-agent-upgrade/`
  - inert local packages reproducing the preceding Aizim meta package's hard
    Codex/Claude dependency shape for an offline upgrade test.
- `.github/workflows/provider-contract.yml`
  - required Codex and Claude provider contract jobs on the supported native
    matrix.

### Modified Python files

- `src/aizim/domain/__init__.py`
- `src/aizim/orchestration/control_plane.py`
- `src/aizim/orchestration/controller_lifecycle.py`
- `src/aizim/orchestration/controller_supervisor.py`
- `src/aizim/orchestration/controller_process.py`
- `src/aizim/orchestration/codex_controller.py`
- `src/aizim/orchestration/claude_controller.py`
- `src/aizim/orchestration/codex_worker.py`
- `src/aizim/orchestration/runner.py`
- `src/aizim/agents/codex_backend.py`
- `src/aizim/agents/linux_sandbox.py`
- `src/aizim/cli/main.py`
- `src/aizim/cli/control_command.py`
- `src/aizim/cli/control_projection.py`
- `src/aizim/cli/doctor_command.py`
- `src/aizim/runtime/distribution.py`
- `src/aizim/security_gate.py`
- `src/aizim/state/control_operations.py`
- `src/aizim/state/schema_v1_orchestration.py`
- `src/aizim/state/operations.py`
- provider, state, supervisor, doctor, sandbox, architecture, and security tests
  under `tests/unit`, `tests/integration`, and `tests/security`.

### Modified npm and Rust files

- `package.json`
- `package-lock.json`
- `lib/assets.mjs`
- `lib/platform.mjs`
- `lib/launch.mjs`
- `scripts/npm/assemble.mjs`
- `scripts/npm/build.mjs`
- `scripts/npm/pack.mjs`
- `scripts/npm/install-smoke.mjs`
- `scripts/npm/registry-smoke.mjs`
- `scripts/npm/verify-ci-evidence.mjs`
- `scripts/npm/write-ci-evidence.mjs`
- affected tests under `tests/npm`.
- `crates/aizim-launcher/src/args.rs`
- `crates/aizim-launcher/src/manifest.rs`
- `crates/aizim-launcher/src/main.rs`
- `crates/aizim-launcher/src/provision.rs`
- `crates/aizim-launcher/tests/manifest_contract.rs`
- `crates/aizim-launcher/tests/provision_contract.rs`

### Deleted files

- `lib/codex.mjs`
- `lib/claude.mjs`
- `tests/npm/codex-resolution.test.mjs`
- `tests/npm/claude-resolution.test.mjs`

These files are recoverable from Git history; their packaged-agent
responsibility is intentionally removed rather than left unreachable.

### Documentation and CI files

- `README.md`
- `npm/README.md`
- `docs/operations/npm-distribution.md`
- `docs/operations/foundation-runbook.md`
- `pyproject.toml`
- `.github/workflows/ci.yml`
- `.github/workflows/lean-integration.yml`
- `.github/workflows/npm-registry-smoke.yml`
- `tests/npm/ci-evidence.test.mjs`
- `tests/npm/release-tools.test.mjs`

---

## Task 1: Replace the closed provider enum with a syntax-only identifier

**Files:**

- Create: `src/aizim/domain/controller_provider.py`
- Modify: `src/aizim/domain/__init__.py`
- Modify: `src/aizim/orchestration/control_plane.py`
- Modify: `src/aizim/orchestration/controller_lifecycle.py`
- Modify: `src/aizim/state/control_operations.py`
- Modify: `src/aizim/state/schema_v1_orchestration.py`
- Modify: `src/aizim/cli/control_projection.py`
- Modify: provider imports in `tests/unit/test_control_plane_replay.py`,
  `tests/unit/test_controller_execution_replay.py`,
  `tests/unit/test_controller_execution_transitions.py`,
  `tests/integration/test_controller_supervisor.py`,
  `tests/integration/test_controller_supervisor_guards.py`, and
  `tests/integration/test_cli_controller_start.py`
- Test: `tests/unit/test_controller_provider_registry.py`
- Test: `tests/integration/test_cli_control_projection.py`

- [ ] Add failing tests proving `codex`, `claude`, and `future_provider-1`
  create valid IDs, while malformed values fail with
  `CONTROLLER_PROVIDER_INVALID`.

  The value type must have this exact contract:

  ```python
  _CONTROLLER_PROVIDER_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")

  @dataclass(frozen=True, slots=True)
  class ControllerProviderId:
      value: str

      def __post_init__(self) -> None:
          if (
              type(self.value) is not str
              or _CONTROLLER_PROVIDER_ID.fullmatch(self.value) is None
          ):
              raise ValueError("CONTROLLER_PROVIDER_INVALID")
  ```

- [ ] Run the focused tests and observe failure because
  `ControllerProvider(StrEnum)` is still closed:

  ```sh
  uv run pytest \
    tests/unit/test_controller_provider_registry.py \
    tests/integration/test_cli_control_projection.py -q
  ```

- [ ] Implement and export `ControllerProviderId`; make
  `control_plane.configure_controller` accept it and serialize `.value`.
  `controller_lifecycle.start_controller` must type-check the value type and
  persist `.value`.

- [ ] Replace `_CONTROLLER_PROVIDERS` and both schema enums with the shared
  syntax validator. Make `control_projection.controller_document` accept any
  well-formed ID without importing or executing a registry adapter.

- [ ] Add a replay test that appends a well-formed unregistered provider event
  and proves projection/replay succeeds, plus malformed-event tests that still
  fail closed.

- [ ] Run the focused tests, then the state/control cluster:

  ```sh
  uv run pytest \
    tests/unit/test_controller_provider_registry.py \
    tests/unit/test_control_plane_replay.py \
    tests/unit/test_controller_execution_replay.py \
    tests/unit/test_controller_execution_transitions.py \
    tests/integration/test_cli_control.py \
    tests/integration/test_cli_control_projection.py -q
  ```

  Expected: all selected tests pass; no `ControllerProvider` import remains.

- [ ] Commit:

  ```sh
  git add src/aizim/domain src/aizim/orchestration/control_plane.py \
    src/aizim/orchestration/controller_lifecycle.py \
    src/aizim/state src/aizim/cli/control_projection.py tests
  git commit -m "Open controller provider identity"
  ```

## Task 2: Resolve and attest external provider executables

**Files:**

- Create: `src/aizim/runtime/provider_executables.py`
- Modify: `src/aizim/runtime/distribution.py`
- Modify: `src/aizim/orchestration/controller_process.py`
- Modify: `src/aizim/agents/linux_sandbox.py`
- Modify: `src/aizim/lean/mcp_client.py` only if its call signature changes
- Test: `tests/unit/test_provider_executables.py`
- Test: `tests/unit/test_distribution_context.py`
- Test: `tests/unit/test_platform_sandbox.py`
- Test: `tests/security/test_linux_sandbox.py`
- Test: `tests/security/test_macos_sandbox.py`

- [ ] Add failing resolver tests for:

  - non-empty override before `PATH`;
  - empty override falling through to `PATH`;
  - independent Codex and Claude overrides;
  - absolute/canonical/executable validation;
  - no import-time environment capture;
  - a descriptor with canonical path, exact version, and SHA-256;
  - unsupported-version detail containing actual, supported, path, and
    `AIZIM_*_EXECUTABLE` recovery guidance;
  - image replacement after descriptor creation;
  - external npm `codex.js` layout locating adjacent `rg` and Linux `bwrap`
    without resolving an Aizim npm dependency.

- [ ] Run and observe the old npm-mode resolution assumptions fail:

  ```sh
  uv run pytest \
    tests/unit/test_provider_executables.py \
    tests/unit/test_distribution_context.py \
    tests/unit/test_platform_sandbox.py -q
  ```

- [ ] Add the immutable evidence type and finite allowlists:

  ```python
  CODEX_VERSIONS = frozenset({"codex-cli 0.145.0"})
  CLAUDE_VERSIONS = frozenset({"2.1.218 (Claude Code)"})

  @dataclass(frozen=True, slots=True)
  class ResolvedExecutable:
      path: Path
      version: str
      sha256: str
  ```

  Provider version probes must use the existing bounded host-command surface,
  never a shell, and must hash after the version probe. Revalidation checks the
  same canonical path, version, and digest.

- [ ] Remove provider keys from `DISTRIBUTION_ENVIRONMENT` and provider fields
  from `DistributionContext`. Change both path resolvers to:

  ```python
  override = environ.get("AIZIM_CODEX_EXECUTABLE")
  candidate = override if override else shutil.which("codex", path=environ.get("PATH"))
  ```

  Use the equivalent Claude names and preserve the existing missing-executable
  codes.

- [ ] Make ripgrep fallback mode-independent. It may inspect the external
  Codex installation adjacent to the already-resolved executable, but it may
  not query an Aizim npm manifest or add a provider directory to the launcher
  `PATH`.

- [ ] Move `codex_runtime_root` into the provider-runtime module. Teach the
  Linux sandbox adapter to validate `bwrap` next to either a native Codex
  executable or the native payload of a canonical external `codex.js`
  installation. Keep Codex as the sandbox command and do not add a sandbox
  provider abstraction.

- [ ] Run the resolver and platform tests:

  ```sh
  uv run pytest \
    tests/unit/test_provider_executables.py \
    tests/unit/test_distribution_context.py \
    tests/unit/test_platform_sandbox.py \
    tests/security/test_linux_sandbox.py \
    tests/security/test_macos_sandbox.py -q
  ```

- [ ] Commit:

  ```sh
  git add src/aizim/runtime src/aizim/orchestration/controller_process.py \
    src/aizim/agents/linux_sandbox.py src/aizim/lean \
    tests/unit tests/security
  git commit -m "Resolve external provider executables"
  ```

## Task 3: Add the immutable Controller adapter registry

**Files:**

- Create: `src/aizim/orchestration/controller_providers.py`
- Modify: `src/aizim/orchestration/control_plane.py`
- Modify: `src/aizim/cli/main.py`
- Modify: `src/aizim/cli/control_command.py`
- Modify: `src/aizim/state/operations.py`
- Test: `tests/unit/test_controller_provider_registry.py`
- Test: `tests/integration/test_cli_control.py`
- Test: `tests/security/test_controller_provider_isolation.py`

- [ ] Add failing tests proving:

  - registry IDs are exactly `("claude", "codex")` in sorted display order;
  - malformed IDs map to `CONTROLLER_PROVIDER_INVALID`;
  - well-formed absent IDs map to `CONTROLLER_PROVIDER_UNSUPPORTED`;
  - a provider ID remains data and is never treated as a path, module, or
    command;
  - `controller configure` persists only registry-supported IDs.

- [ ] Run:

  ```sh
  uv run pytest \
    tests/unit/test_controller_provider_registry.py \
    tests/integration/test_cli_control.py \
    tests/security/test_controller_provider_isolation.py -q
  ```

  Expected: unsupported-provider cases fail before the registry exists.

- [ ] Implement these immutable registry shapes:

  ```python
  @dataclass(frozen=True, slots=True)
  class ResolvedControllerRuntime:
      provider: ControllerProviderId
      codex: ResolvedExecutable
      controller: ResolvedExecutable

  @dataclass(frozen=True, slots=True)
  class ControllerAdapter:
      provider: ControllerProviderId
      resolve: Callable[[Mapping[str, str]], ResolvedExecutable]
      backend: Callable[
          [ResolvedControllerRuntime, Path, str | None, Mapping[str, str]],
          ControllerBackend,
      ]
  ```

  Store entries in a private `MappingProxyType`. Expose only sorted IDs,
  explicit lookup, runtime resolution, and backend construction.

- [ ] Resolve mandatory Codex first. Look up the selected adapter next. Reuse
  the Codex descriptor when the Controller ID is `codex`; otherwise resolve
  only the selected Controller executable. Do not touch Claude for a Codex
  project.

- [ ] Change argparse `--provider` to accept a string and display the registry
  IDs through `metavar`; keep invalid-versus-unsupported handling inside the
  command so both have stable codes. Add
  `CONTROLLER_PROVIDER_UNSUPPORTED` to CLI/RPC safe messages.

- [ ] Run the focused tests and scan away the enum:

  ```sh
  uv run pytest \
    tests/unit/test_controller_provider_registry.py \
    tests/integration/test_cli_control.py \
    tests/security/test_controller_provider_isolation.py -q
  rg -n 'ControllerProvider|choices=tuple\\(ControllerProvider\\)|\\{"codex", "claude"\\}' \
    src/aizim tests
  ```

  Expected: tests pass and the scan has no closed-enum implementation hit.

- [ ] Commit:

  ```sh
  git add src/aizim/orchestration src/aizim/cli src/aizim/state \
    tests/unit/test_controller_provider_registry.py \
    tests/integration/test_cli_control.py \
    tests/security/test_controller_provider_isolation.py
  git commit -m "Register built-in controller providers"
  ```

## Task 4: Inject one runtime snapshot through Controller and Worker paths

**Files:**

- Modify: `src/aizim/orchestration/codex_controller.py`
- Modify: `src/aizim/orchestration/claude_controller.py`
- Modify: `src/aizim/orchestration/codex_worker.py`
- Modify: `src/aizim/agents/codex_backend.py`
- Modify: `src/aizim/orchestration/controller_supervisor.py`
- Modify: `src/aizim/orchestration/runner.py`
- Modify: `src/aizim/security_gate.py`
- Test: `tests/unit/test_codex_controller.py`
- Test: `tests/unit/test_claude_controller.py`
- Test: `tests/unit/test_codex_workspace_backend.py`
- Test: `tests/integration/test_controller_supervisor.py`
- Test: `tests/integration/test_controller_supervisor_guards.py`
- Test: `tests/integration/test_cli_controller_start.py`
- Test: `tests/unit/test_run_exit_codes.py`
- Test: `tests/security/test_controller_provider_isolation.py`
- Test: `tests/security/test_authority_gate.py`

- [ ] Add failing tests that inject one runtime object and assert object/value
  identity reaches Controller construction, Worker construction, Worker
  preflight, and sandbox compilation. Monkeypatch ambient resolvers to raise so
  hidden rediscovery is observable.

- [ ] Change production constructor signatures to:

  ```python
  CodexControllerBackend(
      executable: ResolvedExecutable,
      model: str | None,
      project_root: Path,
      parent_environment: Mapping[str, str],
      ...
  )

  ClaudeControllerBackend(
      executable: ResolvedExecutable,
      sandbox_codex: ResolvedExecutable,
      model: str | None,
      project_root: Path,
      parent_environment: Mapping[str, str],
      ...
  )

  create_codex_backend(
      executable: ResolvedExecutable,
      parent_environment: Mapping[str, str] | None = None,
  )

  preflight_codex_worker(
      project_root: Path,
      executable: ResolvedExecutable,
      parent_environment: Mapping[str, str] | None = None,
  )
  ```

  Constructors must compare current path/version/hash with the supplied
  descriptor before accepting it. Launch paths continue to reject later image
  changes with existing codes.

- [ ] Change supervisor dependencies to:

  ```python
  @dataclass(frozen=True, slots=True)
  class ControllerSupervisorDependencies:
      resolve_runtime: Callable[
          [ControllerProviderId], ResolvedControllerRuntime
      ]
      controller_backend: Callable[
          [ResolvedControllerRuntime, str | None], ControllerBackend
      ]
      worker_backend: Callable[[ResolvedExecutable], AgentBackend]
      worker_preflight: Callable[
          [ResolvedExecutable], Awaitable[None]
      ]
      ...
  ```

  The supervisor parses a syntax-only provider ID, resolves once, constructs
  the selected Controller, preflights the same Codex descriptor, and creates
  the Worker from that descriptor.

- [ ] Make unsupported persisted IDs fail with
  `CONTROLLER_PROVIDER_UNSUPPORTED` before a Controller start event. Ensure no
  preflight failure records a false running state.

- [ ] Make `security-probe` create one Codex descriptor and revalidate that
  descriptor after its probe rather than resolving Codex twice.

- [ ] Update the direct autonomous Codex path in
  `orchestration/runner.py`. One `aizim run --backend codex` operation resolves
  one Codex descriptor from the invocation environment, passes it to
  `create_codex_backend`, and never calls the path resolver from inside the
  Worker factory. Add a unit seam in `tests/unit/test_run_exit_codes.py` that
  injects a descriptor, makes ambient resolution raise after the first call,
  and proves the runner supplies that descriptor to the backend factory.

- [ ] Add an isolated `tests/security/test_authority_gate.py` regression that
  injects one descriptor, replaces the executable after probe construction,
  and observes the existing image-change failure before successful gate
  evidence can be recorded.

- [ ] Run:

  ```sh
  uv run pytest \
    tests/unit/test_codex_controller.py \
    tests/unit/test_claude_controller.py \
    tests/unit/test_codex_workspace_backend.py \
    tests/integration/test_controller_supervisor.py \
    tests/integration/test_controller_supervisor_guards.py \
    tests/integration/test_cli_controller_start.py \
    tests/unit/test_run_exit_codes.py \
    tests/security/test_controller_provider_isolation.py \
    tests/security/test_authority_gate.py -q
  ```

- [ ] Commit:

  ```sh
  git add src/aizim/agents src/aizim/orchestration src/aizim/security_gate.py \
    tests/unit tests/integration tests/security
  git commit -m "Inject resolved controller runtime"
  ```

## Task 5: Make doctor role-based and provider-aware

**Files:**

- Modify: `src/aizim/cli/doctor_command.py`
- Modify: `src/aizim/cli/state_client.py` only if a small projection helper is
  needed
- Test: `tests/integration/test_cli_doctor.py`
- Test: `tests/npm/install-smoke.test.mjs`

- [ ] Replace provider test expectations with this exact stable order:

  ```python
  (
      "controller_configuration",
      "controller_provider",
      "controller_executable",
      "controller_auth",
      "worker_codex",
      "sandbox_exec",
  )
  ```

  Keep the existing non-provider check IDs before and after this block.

- [ ] Add failing JSON and human-output cases for:

  - unconfigured project;
  - Codex selected with no Claude on `PATH`;
  - Claude selected with both executables;
  - selected Claude missing while Codex passes;
  - Codex missing while Claude exists;
  - malformed and well-formed unsupported provider state;
  - controller executable failure causing auth `SKIP`;
  - worker Codex failure causing sandbox `SKIP`;
  - details containing canonical path, version, and SHA-256.

- [ ] Run and observe the old unconditional `claude`/`codex` IDs fail:

  ```sh
  uv run pytest tests/integration/test_cli_doctor.py -q
  node --test tests/npm/install-smoke.test.mjs
  ```

- [ ] Load the persisted Controller projection once. Resolve the mandatory
  Codex descriptor once, independently evaluate the selected Controller, and
  use `DoctorCheck(..., "SKIP", ...)` for downstream or inapplicable checks.
  `ready` remains false only when a check is `FAIL`.

- [ ] For a Codex Controller, reuse the Worker descriptor as
  `controller_executable`; for Claude, resolve only Claude. Run
  provider-specific preflight for `controller_auth` without a model call.

- [ ] Ensure the invalid-layout fallback emits the full stable `_CHECK_IDS`
  tuple and never leaks environment values or raw provider output.

- [ ] Run:

  ```sh
  uv run pytest tests/integration/test_cli_doctor.py -q
  node --test tests/npm/install-smoke.test.mjs
  ```

- [ ] Commit:

  ```sh
  git add src/aizim/cli/doctor_command.py src/aizim/cli/state_client.py \
    tests/integration/test_cli_doctor.py tests/npm/install-smoke.test.mjs
  git commit -m "Report provider readiness by role"
  ```

## Task 6: Remove provider ownership from npm assets and package metadata

**Files:**

- Modify: `package.json`
- Modify: `package-lock.json`
- Modify: `lib/assets.mjs`
- Modify: `lib/platform.mjs`
- Modify: `lib/launch.mjs`
- Modify: `scripts/npm/assemble.mjs`
- Modify: `scripts/npm/build.mjs`
- Modify: `scripts/npm/pack.mjs`
- Delete: `lib/codex.mjs`
- Delete: `lib/claude.mjs`
- Delete: `tests/npm/codex-resolution.test.mjs`
- Delete: `tests/npm/claude-resolution.test.mjs`
- Modify: `tests/npm/package-contract.test.mjs`
- Modify: `tests/npm/package-contents.test.mjs`
- Modify: `tests/npm/assets.test.mjs`
- Modify: `tests/npm/launch.test.mjs`
- Modify: `tests/npm/build-tools.test.mjs`

- [ ] First change npm tests to assert:

  - no `dependencies`, `peerDependencies`, or provider
    `optionalDependencies`;
  - platform records contain only Aizim target metadata;
  - resolved assets contain only launcher/manifests/vendor root;
  - launcher argv contains only both manifest flags, one separator, and user
    arguments;
  - staged and packed allowlists omit provider resolver modules;
  - distribution manifest schema is `3` and has no provider version fields.

- [ ] Run and observe the bundled-provider assertions fail:

  ```sh
  node --test \
    tests/npm/package-contract.test.mjs \
    tests/npm/package-contents.test.mjs \
    tests/npm/assets.test.mjs \
    tests/npm/launch.test.mjs \
    tests/npm/build-tools.test.mjs
  ```

- [ ] Remove provider dependencies from `package.json`, then regenerate only
  lock metadata without lifecycle scripts:

  ```sh
  npm install --package-lock-only --ignore-scripts
  ```

  Inspect the diff and prove the lock no longer contains
  `@openai/codex`, `@anthropic-ai/claude-code`, or their native packages.

- [ ] Remove provider aliases/versions from `platform.mjs`, provider imports
  and fields from `assets.mjs`, launcher flags from `launch.mjs`, provider
  copies from `assemble.mjs`, and provider assertions from build/pack.

- [ ] Delete the four obsolete resolver source/test files with `apply_patch`.
  Do not replace them with dead compatibility modules.

- [ ] Run:

  ```sh
  node --test tests/npm/*.test.mjs
  rg -n '@openai/codex|@anthropic-ai/claude-code|claudeExecutable|codexExecutable' \
    package.json package-lock.json lib scripts/npm/assemble.mjs \
    scripts/npm/build.mjs scripts/npm/pack.mjs tests/npm
  ```

  Expected: Node tests pass; scan hits only deliberate external-provider CI or
  documentation evidence outside these package-ownership files.

- [ ] Commit:

  ```sh
  git add package.json package-lock.json lib scripts/npm tests/npm
  git commit -m "Remove agent packages from npm distribution"
  ```

## Task 7: Remove provider ownership from the Rust launcher

**Files:**

- Modify: `crates/aizim-launcher/src/args.rs`
- Modify: `crates/aizim-launcher/src/manifest.rs`
- Modify: `crates/aizim-launcher/src/main.rs`
- Modify: `crates/aizim-launcher/src/provision.rs`
- Modify: `crates/aizim-launcher/tests/manifest_contract.rs`
- Modify: `crates/aizim-launcher/tests/provision_contract.rs`

- [ ] Change Rust fixtures first so launcher arguments accept only:

  ```text
  --distribution-manifest ABSOLUTE
  --platform-manifest ABSOLUTE
  --
  USER_ARGS...
  ```

  Manifest fixtures use distribution schema `3` and platform
  `distribution_schema_version: 3`, with no provider fields.

- [ ] Add a provision test proving inherited
  `AIZIM_CODEX_EXECUTABLE`, `AIZIM_CLAUDE_EXECUTABLE`, and `PATH` survive the
  launcher, while launcher-owned `AIZIM_DISTRIBUTION_*` values are replaced by
  verified values.

- [ ] Run and observe failures:

  ```sh
  cargo test --workspace --locked
  ```

- [ ] Remove provider paths from `LauncherArgs`, `VerifiedDistribution`,
  `ProvisionRequest`, and `main.rs`. Remove provider executable verification
  and injected provider environment variables.

- [ ] Delete `runtime_path`; preserve inherited `PATH` exactly. Keep clearing
  hostile uv/pip/Python and launcher-owned distribution injection variables.

- [ ] Keep all Aizim-owned artifact verification and update the strict schema
  numbers consistently.

- [ ] Run:

  ```sh
  cargo fmt --all --check
  cargo clippy --workspace --all-targets --locked -- -D warnings
  cargo test --workspace --locked
  ```

- [ ] Commit:

  ```sh
  git add crates/aizim-launcher
  git commit -m "Narrow launcher artifact boundary"
  ```

## Task 8: Rewrite package-install and registry smoke evidence

**Files:**

- Modify: `scripts/npm/install-smoke.mjs`
- Modify: `scripts/npm/registry-smoke.mjs`
- Modify: `scripts/npm/verify-ci-evidence.mjs`
- Modify: `scripts/npm/write-ci-evidence.mjs`
- Modify: `tests/npm/install-smoke.test.mjs`
- Modify: `tests/npm/ci-evidence.test.mjs`
- Modify: `tests/npm/release-tools.test.mjs`
- Create: `tests/fixtures/npm-bundled-agent-upgrade/aizim/package.json`
- Create: `tests/fixtures/npm-bundled-agent-upgrade/codex/package.json`
- Create: `tests/fixtures/npm-bundled-agent-upgrade/claude/package.json`

- [ ] Replace install evidence names for bundled agents with:

  ```javascript
  "provider_free_local_install",
  "provider_free_global_install",
  "provider_free_version",
  "provider_free_help",
  "provider_free_doctor_fails_closed",
  "no_agent_dependencies",
  ```

  Retain Python bootstrap, cache reuse, uninstall preservation, deterministic
  fake run/controller loop, missing-platform exit 78, and Aizim-owned integrity
  failure exit 74.

- [ ] Add failing unit tests proving the minimal consumer `PATH` has neither
  provider, install succeeds without lifecycle scripts, `--version` and
  `--help` succeed, and unconfigured `doctor --json` exits 3 with the stable
  role check set.

- [ ] Add an offline upgrade fixture that reproduces the preceding meta
  package's exact hard dependencies on `@openai/codex@0.145.0` and
  `@anthropic-ai/claude-code@2.1.218` using inert local packages, so the core
  lane does not download or execute either real provider. The upgrade scenario
  must:

  1. install the legacy fixture into a private consumer;
  2. initialize a Lean project and record the state database digest;
  3. write credential sentinels under the consumer's private
     `HOME/.codex/` and `HOME/.claude/` and record their digests;
  4. install the newly packed Aizim meta/platform tarballs over the legacy
     package with `--ignore-scripts`;
  5. prove project state and both credential sentinels are byte-identical;
  6. prove the new package metadata has no agent dependency and does not
     resolve the legacy nested fake executables;
  7. run `--version` and `--help` successfully with a provider-free `PATH`;
  8. configure a Codex Controller and prove `doctor --json` fails
     `worker_codex` until a supported external Codex is explicitly available.

  Record this as a separate exact evidence key
  `bundled_agent_upgrade_preserved_state_credentials`.

- [ ] Remove packaged Claude discovery, bundled provider doctor success, and
  security-gate success from the core install smoke. Provider/sandbox reality
  belongs to the dedicated provider jobs in Task 10.

- [ ] Update public-registry smoke to validate provider-free install,
  `--version`, `--help`, expected doctor failure, runtime cache reuse, Lean
  build, and the deterministic fake run. Remove its assumption that the
  published Aizim package supplies Codex.

- [ ] Update exact evidence validators and tests; do not weaken them to accept
  arbitrary keys.

- [ ] Run:

  ```sh
  node --test \
    tests/npm/install-smoke.test.mjs \
    tests/npm/ci-evidence.test.mjs \
    tests/npm/release-tools.test.mjs
  node scripts/npm/install-smoke.mjs
  ```

- [ ] Commit:

  ```sh
  git add scripts/npm/install-smoke.mjs scripts/npm/registry-smoke.mjs \
    scripts/npm/verify-ci-evidence.mjs scripts/npm/write-ci-evidence.mjs \
    tests/npm tests/fixtures/npm-bundled-agent-upgrade
  git commit -m "Verify provider-free package installs"
  ```

## Task 9: Document the external prerequisite and upgrade contract

**Files:**

- Modify: `README.md`
- Modify: `npm/README.md`
- Modify: `docs/operations/npm-distribution.md`
- Modify: `docs/operations/foundation-runbook.md`
- Modify: `pyproject.toml`
- Modify:
  `docs/superpowers/specs/2026-07-25-external-controller-providers-design.md`
  only to remove the duplicated “implicit Codex default” sentence
- Test: `tests/npm/package-contract.test.mjs`
- Test: `tests/npm/package-contents.test.mjs`

- [ ] Add documentation contract assertions for:

  - Aizim installs neither agent CLI;
  - supported Codex and Claude versions;
  - Codex always required for Worker/sandbox readiness;
  - Claude required only for a Claude Controller;
  - override-before-`PATH` precedence;
  - actual/supported version failure and side-by-side override recovery;
  - runtime TOFU and user-owned provider provenance;
  - upgrade from bundled-agent builds keeps project state/credentials but no
    longer uses nested legacy providers.

- [ ] Remove statements that npm supplies or isolates Codex/Claude. Update
  sandbox pytest marker descriptions from “packaged Codex” to “supported
  external Codex”.

- [ ] Keep `THIRD_PARTY_NOTICES.md` byte-identical and run its existing
  generation/check path; no provider notice entry exists to remove.

- [ ] Run:

  ```sh
  node --test \
    tests/npm/package-contract.test.mjs \
    tests/npm/package-contents.test.mjs
  node scripts/npm/write-notices.mjs
  git diff --exit-code -- THIRD_PARTY_NOTICES.md
  rg -n 'package-local (Codex|Claude)|supplies Codex|bundled Codex' \
    README.md npm/README.md docs/operations pyproject.toml
  ```

  Expected: tests and notice check pass; scan has no obsolete ownership claim.

- [ ] Commit:

  ```sh
  git add README.md npm/README.md docs/operations pyproject.toml \
    docs/superpowers/specs/2026-07-25-external-controller-providers-design.md \
    tests/npm
  git commit -m "Document external agent prerequisites"
  ```

## Task 10: Add required real-CLI provider contract lanes

**Files:**

- Create: `tests/integration/test_provider_contract.py`
- Create: `.github/workflows/provider-contract.yml`
- Modify: `.github/workflows/lean-integration.yml`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/npm-registry-smoke.yml`
- Modify: `tests/npm/ci-evidence.test.mjs`

- [ ] Create a no-credential integration contract, guarded by an explicit
  environment flag, that:

  - resolves real external CLI descriptors;
  - checks canonical path/version/SHA-256 evidence;
  - validates Codex sandbox host discovery;
  - constructs the selected Controller backend;
  - preflights the Codex Worker sandbox without a model call;
  - proves Claude composition uses the same Codex descriptor and does not call
    Claude auth or a model.

- [ ] Add workflow contract tests first. Require two job IDs:
  `provider-contract-codex` and `provider-contract-claude`. Each job uses the
  supported native matrix:

  ```yaml
  matrix:
    include:
      - target: darwin-arm64
        runner: macos-15
      - target: linux-arm64
        runner: ubuntu-24.04-arm
      - target: linux-x64
        runner: ubuntu-24.04
  ```

- [ ] In `provider-contract-codex`, install
  `@openai/codex@0.145.0` explicitly in job setup. In
  `provider-contract-claude`, explicitly install both
  `@openai/codex@0.145.0` and
  `@anthropic-ai/claude-code@2.1.218`, with the required Claude lifecycle
  permission. Assert exact `--version` output before running the contract.

- [ ] Enable Linux user namespaces exactly as the native and Lean jobs do.
  Install locked Python dependencies with uv, then run only the real contract
  and relevant offline provider-isolation tests with
  `AIZIM_PROVIDER_CONTRACT=1`. Do not require API credentials or make a model
  call.

- [ ] Remove Claude from `.github/workflows/lean-integration.yml`. Keep its
  explicit Codex installation because that workflow exercises the real
  Codex-backed sandbox/security gate.

- [ ] Keep core/native/package jobs provider-free. Update the workflow tests so
  provider package installation is accepted only in provider-contract jobs and
  the Codex-specific Lean/security lane.

- [ ] Run:

  ```sh
  node --test tests/npm/ci-evidence.test.mjs
  actionlint
  uv run pytest tests/integration/test_provider_contract.py -q
  ```

  The local provider contract may report the deliberately unsupported installed
  Claude version as a tested hard failure. The CI lane is the authoritative
  supported-version success because it installs `2.1.218` explicitly.

- [ ] Commit:

  ```sh
  git add .github/workflows tests/integration/test_provider_contract.py \
    tests/npm/ci-evidence.test.mjs
  git commit -m "Add external provider contract CI"
  ```

## Task 11: Run aggregate verification and manual QA

**Files:**

- Modify only defects found by verification, in the owning task's files.

- [ ] Confirm no stale closed-provider or packaged-provider implementation
  remains:

  ```sh
  rg -n 'ControllerProvider|_CONTROLLER_PROVIDERS|claudeExecutable|codexExecutable|--claude-executable|--codex-executable' \
    src tests lib scripts/npm crates/aizim-launcher
  rg -n '@openai/codex|@anthropic-ai/claude-code' \
    package.json package-lock.json lib scripts/npm crates/aizim-launcher
  ```

  Expected: the first scan has no implementation hit; the second has no
  distribution dependency hit.

- [ ] Run Python static and offline test gates:

  ```sh
  uv run ruff check .
  uv run ty check
  uv run pytest \
    -m "not lean_integration and not linux_sandbox and not macos_sandbox and not manual_real_codex and not manual_real_controller" \
    -q
  ```

- [ ] Run platform sandbox and Lean integration tests for the current macOS
  host:

  ```sh
  uv run pytest -m "macos_sandbox or lean_integration" \
    --ignore=tests/e2e/test_real_codex_smoke.py -q
  ```

- [ ] Run Node/Rust/package gates:

  ```sh
  node --test tests/npm/*.test.mjs
  cargo fmt --all --check
  cargo clippy --workspace --all-targets --locked -- -D warnings
  cargo test --workspace --locked
  npm run build
  npm test
  npm run pack
  npm run check
  actionlint
  ```

- [ ] Manually QA the packed artifact in a private temporary consumer with a
  stripped provider-free `PATH`:

  ```sh
  node scripts/npm/install-smoke.mjs
  ```

  Observe local/global installation, `aizim --version`, `aizim --help`,
  deterministic unconfigured doctor failure, managed runtime reuse, fake run,
  and Aizim-owned manifest corruption rejection.

- [ ] Manually QA the current real Codex resolution and sandbox:

  ```sh
  AIZIM_PROVIDER_CONTRACT=1 \
    uv run pytest tests/integration/test_provider_contract.py \
    -k codex -q -s
  ```

  Record canonical path, `codex-cli 0.145.0`, SHA-256, and sandbox validation.
  For the locally installed Claude version, run the unsupported-version test
  and observe actual version, supported `2.1.218`, and override guidance. Do not
  perform an authenticated model call.

- [ ] Check the exact diff and worktree:

  ```sh
  git diff --check
  git status --short
  git log --oneline origin/main..HEAD
  ```

  Expected: only intended tracked changes plus the pre-existing `?? .omo/`.

## Task 12: Independent implementation review, push, and CI closure

**Files:**

- Add only evidence artifacts required by the repository's existing review
  workflow; do not stage `.omo/`.

- [ ] Run the required post-implementation review and runtime-debugging audit
  against the exact full commit SHA. Fix every blocking finding with a forward
  commit, rerun affected tests, and rerun both audits at the new SHA.

- [ ] Perform a requirement-by-requirement audit against all 18 acceptance
  criteria in the approved design. Record the exact command/test/CI evidence
  for each criterion and confirm no criterion relies only on source inspection.

- [ ] Push ordinary forward commits:

  ```sh
  git push origin main
  ```

- [ ] Monitor all GitHub checks at the pushed SHA. Required green surfaces
  include core Python, all three native package targets, minimum Node,
  aggregate evidence, Lean integration, `provider-contract-codex`, and
  `provider-contract-claude`.

- [ ] If CI fails, inspect the exact failing log, reproduce or isolate locally,
  add a regression test where appropriate, fix forward, rerun local affected
  gates, push, and monitor the replacement run.

- [ ] Stop only when the pushed SHA is green, the worktree contains no
  unintended tracked change, `.omo/` is untouched, and the 18-point audit has
  concrete evidence.
