# External Controller Providers with Codex Workers

**Status:** Approved architecture, written specification for user review

**Date:** 2026-07-25

**Scope:** External agent CLI ownership, explicit Controller provider selection,
Codex-only Workers, Codex-backed sandboxing, provider diagnostics, npm
distribution boundaries, and CI coverage

## 1. Purpose

Aizim is an agent orchestrator. It does not own installation of the agents it
orchestrates. Users install and authenticate the agent CLIs they choose, while
Aizim selects them through explicit project configuration and validates them at
the point where their role is needed.

The roles are intentionally asymmetric in this phase:

- the Controller is selectable: Codex, Claude, and future built-in providers;
- Workers are Codex-only;
- sandbox enforcement continues to use the supported Codex installation;
- the Aizim npm distribution installs neither Codex nor Claude.

This corrects the current package and runtime behavior, which makes both
`@openai/codex` and `@anthropic-ai/claude-code` hard npm dependencies and
requires both executables in every npm distribution environment.

## 2. Confirmed decisions

The following decisions are fixed for this implementation:

1. Agent CLIs are user-managed external prerequisites. Aizim does not download,
   install, update, remove, or authenticate them.
2. Neither `@openai/codex` nor `@anthropic-ai/claude-code` is a dependency,
   optional dependency, or peer dependency of `@aiz.im/aizim`.
3. Controller provider selection is explicit and persisted per project. There
   is no default provider and no automatic provider detection.
4. The existing explicit command remains the configuration surface:

   ```sh
   aizim controller configure \
     --project /absolute/path/to/lean-project \
     --provider codex
   ```

5. The Controller may be Codex or Claude in the initial registry. Future
   Controller providers are added through the same internal adapter contract.
6. Workers remain Codex-only. Worker provider selection is not introduced by
   this change.
7. Sandbox selection is not generalized. Aizim continues to compile its
   sandbox using the supported Codex installation and fails closed when that
   mechanism is unavailable.
8. Codex is therefore required for Controller execution regardless of the
   selected Controller provider: it supplies the Worker and sandbox runtime.
9. Claude is required only when the project explicitly selects the Claude
   Controller.
10. `AIZIM_CODEX_EXECUTABLE` and `AIZIM_CLAUDE_EXECUTABLE` are optional,
    independent executable overrides. They are not launcher-injected
    distribution requirements.
11. Executable resolution happens at command invocation time, not module import
    time, so tests and long-lived processes do not capture stale ambient state.
12. Missing, unsupported, or unauthenticated selected providers fail explicitly.
    Aizim never installs a replacement, changes the configured provider, or
    falls back to another provider.
13. The initial compatibility checks retain the currently supported CLI
    versions, Codex `0.145.0` and Claude Code `2.1.218`. External ownership
    changes installation responsibility, not the adapter's command/version
    compatibility contract.
14. The already-pushed commits that made CI install both CLIs are corrected by
    ordinary forward commits. Git history is not rewritten or force-pushed.

## 3. Relationship to the npm distribution design

This document supersedes only the agent-provider parts of
`2026-07-23-aizim-npm-rust-distribution-design.md`.

Specifically, it replaces the earlier requirements that:

- Codex is an exact npm dependency;
- an npm installation cannot use an external Codex executable;
- the JavaScript dispatcher resolves packaged agent executables;
- the Rust launcher receives, verifies, and injects agent executable paths;
- distribution manifests record Codex or Claude package versions;
- npm-mode Python requires both executable environment variables.

The following distribution decisions remain unchanged:

- npm owns the Aizim launcher and platform package selection;
- the launcher provisions the locked uv/Python runtime;
- Python remains the control and research plane;
- Lean and elan remain external prerequisites;
- supported platform packages and glibc-only Linux scope remain unchanged;
- sandbox enforcement remains fail-closed;
- package publication still requires separate authorization.

The resulting install boundary is:

```text
@aiz.im/aizim
├── Aizim JavaScript entry point
├── native platform launcher
├── pinned uv/Python bootstrap artifacts
├── Aizim Python wheel and locked Python dependencies
└── no agent CLI packages
```

Installing the package and running distribution-only commands must not require
an agent:

```sh
npm install --global @aiz.im/aizim
aizim --version
aizim --help
```

Starting a configured Controller or reaching project readiness does require the
external executables implied by the project's role configuration.

## 4. Goals and non-goals

### 4.1 Goals

- Make agent installation an explicit user responsibility.
- Keep Controller selection explicit, persistent, and provider-neutral.
- Keep Workers and sandbox behavior Codex-specific for this phase.
- Check only the selected Controller provider in addition to mandatory Codex.
- Make adding a future built-in Controller provider local to an adapter and its
  tests, without changing Worker or sandbox architecture.
- Preserve fail-closed executable, version, image, authentication, and sandbox
  validation.
- Make package installation and core distribution smoke tests independent of
  agent lifecycle scripts.
- Keep provider tests deterministic by injecting isolated fake executables.

### 4.2 Non-goals

- Making Workers provider-selectable.
- Creating a generic sandbox provider abstraction.
- Bundling or automatically provisioning Codex, Claude, or future agents.
- Loading arbitrary third-party Python entry points or npm provider plugins.
- Selecting a provider from whichever executable happens to be on `PATH`.
- Falling back between providers after a preflight or runtime failure.
- Relaxing controller command isolation, environment filtering, image hashing,
  version checks, authentication checks, or sandbox enforcement.
- Changing the uv/Python bootstrap, Lean ownership, platform package matrix, or
  formal-truth boundary.

## 5. Role and prerequisite model

| Configured Controller | Controller executable | Worker executable | Sandbox source |
| --- | --- | --- | --- |
| not configured | none | Codex is still the future Worker prerequisite | Codex |
| `codex` | Codex | Codex | the same supported Codex installation |
| `claude` | Claude | Codex | the supported Codex installation |
| future provider | that provider's executable | Codex | the supported Codex installation |

One resolved Codex executable is the mandatory runtime anchor. When the
Controller is also Codex, the Controller and Workers use that same canonical
installation. When the Controller is Claude, Aizim resolves Claude separately
but passes the already-resolved Codex installation to the existing
Codex-specific sandbox path. The Claude backend must not perform a second,
hidden Codex discovery.

This is role composition, not a claim that Claude itself depends on Codex.
Aizim requires Codex because the current Worker and sandbox roles require it.

## 6. User flow

### 6.1 Codex Controller

The user installs and authenticates a compatible Codex CLI independently, then
configures the project:

```sh
aizim init /absolute/path/to/lean-project
aizim controller configure \
  --project /absolute/path/to/lean-project \
  --provider codex
aizim doctor --project /absolute/path/to/lean-project
```

No Claude installation is required or checked.

### 6.2 Claude Controller

The user independently installs and authenticates both Codex and Claude:

```sh
aizim controller configure \
  --project /absolute/path/to/lean-project \
  --provider claude
aizim doctor --project /absolute/path/to/lean-project
```

Codex supplies Workers and sandbox enforcement. Claude supplies Controller
planning.

### 6.3 Unconfigured project

`aizim init` may create a project without choosing a Controller. Commands such
as `--version`, `--help`, project initialization, and configuration remain
available. `doctor` reports the missing Controller configuration and
`controller start` fails with `CONTROLLER_NOT_CONFIGURED`.

There is no interactive guess, install-time choice, `PATH`-based choice, or
implicit Codex default.

## 7. Executable resolution

Executable resolution is provider-owned but follows one shared security
contract.

### 7.1 Precedence

Codex:

1. non-empty `AIZIM_CODEX_EXECUTABLE`;
2. `codex` found on the inherited `PATH`;
3. `CODEX_EXECUTABLE_UNAVAILABLE`.

Claude:

1. non-empty `AIZIM_CLAUDE_EXECUTABLE`;
2. `claude` found on the inherited `PATH`;
3. `CLAUDE_EXECUTABLE_UNAVAILABLE`.

There is no repository-local wrapper, npm package lookup, platform-package
lookup, or bare unresolved command fallback.

### 7.2 Validation

An explicit override must be an absolute path. Every resolved executable is
canonicalized and must be an executable regular file under the existing
link-count and symlink restrictions. The adapter runs its version probe and
rejects an unsupported CLI version before authentication or planning.

Resolution occurs once per readiness or Controller-start operation. The
canonical paths are passed into the Worker, Controller, and Codex sandbox
factories rather than rediscovered independently. Existing image hashes and
change detection remain binding for the lifetime of the backend.

### 7.3 Distribution environment separation

`AIZIM_CODEX_EXECUTABLE` and `AIZIM_CLAUDE_EXECUTABLE` are removed from the
all-or-nothing `DISTRIBUTION_ENVIRONMENT` set. The npm launcher continues to
inject only launcher-owned distribution metadata such as mode, version,
target, and manifest digests.

Provider executable overrides may be set independently in source or npm mode.
An unset override is normal and falls through to `PATH`.

The Rust launcher preserves the user's `PATH`; it no longer inserts a packaged
Codex directory or passes provider executable arguments.

## 8. Controller provider registry

Aizim gains one internal, immutable registry for Controller providers. This is
an internal source-code extension seam, not a dynamic plugin loader.

Each registry entry owns:

1. a stable provider ID;
2. executable resolution and compatibility probing;
3. Controller backend construction, including provider-specific preflight.

The initial registry contains `codex` and `claude`. CLI help and configuration
validation derive their displayed supported IDs from this registry instead of
duplicating a closed enum in several modules.

The Controller backend protocol remains the stable runtime boundary:

- expose a `BackendIdentity`;
- run provider-specific `preflight`;
- accept a `ControllerContext`;
- return a validated `ControllerDecision`.

The registry does not own Workers. `create_codex_backend` and
`preflight_codex_worker` remain the fixed Worker path.

Adding a future built-in Controller provider requires:

- one adapter/backend module;
- one registry entry;
- provider-specific executable and preflight tests;
- shared Controller contract tests.

It does not require a Worker provider, sandbox provider, npm dependency, Rust
launcher change, or state-schema enum migration.

## 9. Persisted provider identity

The persisted `provider` value becomes a validated provider identifier rather
than a closed `codex | claude` schema enum.

The serialized form is a lowercase identifier matching:

```text
[a-z][a-z0-9_-]{0,63}
```

It is data only. It is never interpolated into a shell command, executable
path, module name, or import target.

State validation checks the identifier shape. Runtime configuration and
Controller start look it up in the immutable registry. An identifier with no
registered adapter produces `CONTROLLER_PROVIDER_UNSUPPORTED`.

Existing `codex` and `claude` events remain valid and require no data migration.
This split prevents future providers from requiring a state schema revision
while ensuring that unknown values cannot execute arbitrary code.

## 10. Runtime data flow

Controller startup follows this sequence:

1. Validate the project layout and state ownership.
2. Read the persisted primary Controller configuration.
3. Reject missing or malformed provider configuration.
4. Resolve and validate the mandatory external Codex executable.
5. Validate the Codex-backed sandbox host.
6. Look up the selected Controller adapter.
7. If the selected provider is not Codex, resolve and validate its executable.
8. Construct the selected Controller backend with canonical executable paths.
9. Run Controller preflight, then Codex Worker preflight.
10. Start the Controller runtime and dispatch all execution tasks to the Codex
    Worker backend.

No step mutates provider configuration. A failure before the Controller starts
records no false running state. A runtime failure never causes another provider
to be selected.

For a Claude Controller, the mandatory Codex path is supplied explicitly to
the existing sandbox compiler. The Claude backend does not call the ambient
Codex resolver itself.

## 11. Doctor and error behavior

`aizim doctor` derives its required provider checks from persisted project
configuration.

Checks that always apply include:

- Aizim runtime and project readiness;
- Lean prerequisites;
- mandatory Codex Worker executable and supported version;
- Codex-backed sandbox readiness;
- existing state, disk, and Python package checks.

Controller checks are conditional:

- unconfigured: fail `controller_configuration`;
- `codex`: the mandatory Codex check also satisfies the Controller executable
  requirement, followed by Controller authentication preflight;
- `claude`: additionally check Claude executable, supported version, and
  authentication;
- unknown registry ID: fail `controller_provider` as unsupported.

An unselected provider is omitted from readiness evaluation. Missing Claude
cannot make a Codex-configured project fail.

Human and JSON output distinguish at least these failure classes:

| Failure | Machine-facing code or check |
| --- | --- |
| no persisted provider | `CONTROLLER_NOT_CONFIGURED` |
| provider not registered | `CONTROLLER_PROVIDER_UNSUPPORTED` |
| mandatory Codex missing | `WORKER_CODEX_EXECUTABLE_UNAVAILABLE` |
| selected Controller executable missing | `CONTROLLER_EXECUTABLE_UNAVAILABLE` |
| selected CLI version unsupported | `CONTROLLER_VERSION_UNSUPPORTED` |
| selected provider authentication unavailable | `CONTROLLER_AUTH_FAILED` |
| Codex sandbox unavailable | `SANDBOX_UNAVAILABLE` |

The exact CLI wording may add installation guidance, but it must not execute an
installer or imply that Aizim owns the agent installation.

## 12. Sandbox and auxiliary tools

This design does not introduce a sandbox registry or generic sandbox provider.
The existing macOS and Linux adapters remain keyed from the supported Codex
installation:

- macOS continues to require the existing `sandbox-exec` contract;
- Linux continues to validate the Codex-adjacent sandbox mechanism;
- an invalid or unavailable host mechanism fails closed.

The external-install change must not weaken path canonicalization, runtime-root
allowlisting, filtered provider environments, command-tail validation, or
executable image hashing.

Ripgrep resolution continues to prefer `rg` on `PATH`. A compatible
Codex-adjacent `codex-path/rg` may remain a fallback when that external Codex
layout provides it. Failure is reported as a ripgrep requirement, not repaired
by installing an npm agent package.

## 13. npm and Rust changes

The npm distribution boundary removes provider knowledge:

- remove both agent packages from `package.json` and `package-lock.json`;
- remove provider package aliases and versions from platform metadata;
- remove JavaScript modules that resolve packaged Codex or Claude executables;
- remove agent executable fields from distribution manifests;
- remove `--codex-executable` and `--claude-executable` launcher arguments;
- remove provider executable verification and injection from the Rust launcher;
- remove provider-specific package assertions from build, pack, contents, and
  release verification;
- update third-party notices and generated package assets accordingly.

The launcher still verifies Aizim-owned wheels, manifests, uv, Python runtime
artifacts, and platform executables. Removing agent ownership does not weaken
verification of artifacts that remain inside the distribution boundary.

## 14. Danus patterns used as a reference

Danus provides three useful implementation patterns:

1. `danus/codex.py` centralizes Worker Codex executable, environment, model, and
   command construction instead of duplicating it across execution sites.
2. `danus/strategy` keeps the main reasoning transport separate from Codex
   Workers and checks Claude only inside the selected Claude path.
3. Its tests inject fake Codex/Claude executables or runners, keeping provider
   behavior deterministic and offline.

Aizim adopts those boundary patterns, not Danus's complete implementation.
The following Danus choices are explicitly rejected here:

- a default provider;
- falling unknown provider values back to a default;
- a repository-provisioned `bin/codex` wrapper;
- a bare unresolved executable fallback;
- Worker use of `--dangerously-bypass-approvals-and-sandbox`.

Aizim retains explicit configuration, canonical executable validation, and
fail-closed sandbox enforcement.

## 15. Test and CI design

### 15.1 Unit tests

Unit tests use temporary executable fixtures and explicit environments. They
cover:

- override-before-`PATH` precedence for each built-in provider;
- missing and invalid executable errors;
- independent optional overrides;
- exact version compatibility failures;
- registry lookup and unsupported IDs;
- provider identifier syntax and non-execution;
- no import-time environment capture;
- no hidden Codex discovery inside the Claude backend;
- sandbox image and executable change detection;
- absence of fallback between providers.

Tests must not borrow a globally installed Codex, Claude, `bwrap`, or ripgrep.

### 15.2 Python integration tests

Integration tests cover:

- unconfigured project readiness failure;
- Codex Controller plus Codex Worker with no Claude executable;
- Claude Controller plus Codex Worker;
- selected Claude missing while Codex is present;
- mandatory Codex missing while Claude is present;
- unregistered provider state;
- conditional `doctor` text and JSON output;
- successful Controller-to-Codex-Worker dispatch with injected backends;
- fail-closed sandbox behavior.

### 15.3 npm and Rust distribution tests

Distribution tests prove:

- the packed Aizim package contains no agent package dependency;
- a clean npm install runs no Codex or Claude lifecycle script;
- `aizim --version` and `aizim --help` work with neither agent installed;
- launcher arguments, manifests, and environment contain no agent executable
  fields;
- source and packed builds do not resolve agent npm packages;
- Aizim-owned artifact integrity checks remain intact.

### 15.4 CI lanes

Core Python, npm, Rust, package-install, and Lean integration lanes do not
install Claude merely to satisfy generic tests.

Provider-specific integration jobs may install supported external CLIs
explicitly as CI tools:

- the Codex lane installs Codex;
- the Claude Controller lane installs both Codex and Claude because Workers
  remain Codex-only.

Those installations belong to the job setup, not the package dependency graph.
Authentication-dependent model calls remain separately authorized manual
smokes; normal CI uses fake executables and does not require credentials.

## 16. Acceptance criteria

The implementation is complete only when all of the following are observed:

1. Installing the packed npm artifact with no agent packages succeeds.
2. `aizim --version` and `aizim --help` work with no agent CLI on `PATH`.
3. A project cannot start a Controller until a provider is explicitly
   configured.
4. A Codex-configured project does not resolve or check Claude.
5. A Claude-configured project requires both Claude Controller readiness and
   Codex Worker/sandbox readiness.
6. Missing selected dependencies produce distinct, actionable failures.
7. Unknown provider identifiers do not execute code and fail as unsupported.
8. Codex sandbox enforcement remains fail-closed on supported platforms.
9. Provider tests pass with isolated fake executables and a stripped `PATH`.
10. Package, Python, Rust, architecture, and relevant platform test suites pass.
11. CI no longer installs Claude in unrelated Lean or core jobs.
12. The correction lands as forward commits without rewriting pushed history.
