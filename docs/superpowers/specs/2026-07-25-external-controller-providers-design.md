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
   install, update, remove, log into, or provision credentials for them; it may
   check the selected CLI's existing authentication state during preflight.
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
13. Each adapter owns a finite allowlist of versions whose command, output, and
    sandbox contracts Aizim has tested. The initial allowlists contain only
    Codex `0.145.0` and Claude Code `2.1.218`. A version outside the allowlist is
    a hard failure; Aizim neither assumes semver compatibility nor downgrades
    the user's CLI.
14. External provider provenance becomes the user's responsibility. Aizim
    validates and hashes the selected executable for one backend lifetime, but
    it no longer attests that executable through its own npm dependency graph.
15. The already-pushed commits that made CI install both CLIs are corrected by
    ordinary forward commits. Git history is not rewritten or force-pushed.

## 3. Relationship to the npm distribution design

This document supersedes only the agent-provider parts of
`2026-07-23-aizim-npm-rust-distribution-design.md`.

Specifically, it replaces the earlier requirements that:

- Codex is an exact npm dependency;
- an npm installation cannot use an external Codex executable;
- global Codex versions cannot affect an npm-installed Aizim;
- the JavaScript dispatcher resolves packaged agent executables;
- the Rust launcher receives, verifies, and injects agent executable paths;
- distribution manifests record Codex or Claude package versions;
- npm-mode Python requires both executable environment variables;
- sandbox readiness is established against the exact packaged Codex;
- npm runtime security requires never selecting a global Codex;
- acceptance requires using only a package-local Codex `0.145.0`.

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

## 4. Accepted trust-boundary change

The previous npm design placed Codex inside Aizim's distribution trust
boundary: npm selected an exact dependency and platform package, the launcher
canonicalized the injected executable, manifests pinned the expected package
version, and sandbox qualification was specified against that packaged
installation.

This design deliberately moves the provider binary outside that boundary.
After migration:

- the user, their package manager, and their `PATH` or explicit executable
  override are the source of provider provenance;
- Aizim does not verify an npm provenance statement, publisher signature,
  package-lock membership, or pre-recorded digest for that external binary;
- Aizim canonicalizes the executable, enforces file restrictions, checks its
  version against the adapter's tested allowlist, records its SHA-256 digest,
  and rejects path or content changes for that backend lifetime;
- starting a later command or backend establishes a new trust-on-first-use
  snapshot and may accept a deliberately upgraded user installation if its
  version is supported.

This is runtime TOFU, not supply-chain attestation. Image hashing detects
replacement after backend construction; it cannot prove that the initially
selected executable is benign. Because Codex supplies the current Worker and
sandbox mechanism, a malicious external Codex binary is inside those roles'
trusted computing base. Fail-closed sandbox checks protect against missing or
incompatible enforcement, not against a malicious binary already trusted by
the user.

`doctor` exposes the canonical executable path, supported version, and observed
SHA-256 digest so the user can audit what Aizim trusted. Existing
`BackendIdentity` lifecycle evidence continues to record the executable digest.
An explicit `AIZIM_CODEX_EXECUTABLE` is recommended when `PATH` precedence is
not an adequate trust decision.

This trade is accepted because Aizim is an orchestrator rather than an agent
distributor. Security claims after this change must distinguish integrity of
Aizim-owned artifacts from provenance of user-managed provider binaries.

## 5. Goals and non-goals

### 5.1 Goals

- Make agent installation an explicit user responsibility.
- State the external-provider trust boundary without claiming supply-chain
  verification that Aizim no longer performs.
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

### 5.2 Non-goals

- Making Workers provider-selectable.
- Creating a generic sandbox provider abstraction.
- Bundling or automatically provisioning Codex, Claude, or future agents.
- Loading arbitrary third-party Python entry points or npm provider plugins.
- Selecting the Controller provider identity from whichever executable happens
  to be on `PATH`; `PATH` is only an executable lookup after explicit provider
  configuration.
- Falling back between providers after a preflight or runtime failure.
- Relaxing controller command isolation, environment filtering, image hashing,
  version checks, authentication checks, or sandbox enforcement.
- Changing the uv/Python bootstrap, Lean ownership, platform package matrix, or
  formal-truth boundary.

## 6. Role and prerequisite model

| Configured Controller | Controller executable | Worker executable | Sandbox source |
| --- | --- | --- | --- |
| not configured | none | Codex is required before Worker execution | Codex |
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

## 7. User flow

### 7.1 Codex Controller

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

### 7.2 Claude Controller

The user independently installs and authenticates both Codex and Claude:

```sh
aizim controller configure \
  --project /absolute/path/to/lean-project \
  --provider claude
aizim doctor --project /absolute/path/to/lean-project
```

Codex supplies Workers and sandbox enforcement. Claude supplies Controller
planning.

### 7.3 Unconfigured project

`aizim init` may create a project without choosing a Controller. Commands such
as `--version`, `--help`, project initialization, and configuration remain
available. `doctor` reports the missing Controller configuration and
`controller start` fails with `CONTROLLER_NOT_CONFIGURED`.

## 8. Executable resolution

Executable resolution is provider-owned but follows one shared security
contract.

### 8.1 Precedence

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

### 8.2 Validation

An explicit override must be an absolute path. Every resolved executable is
canonicalized and must be an executable regular file under the existing
link-count and symlink restrictions. The adapter runs its version probe and
requires membership in its finite tested-version allowlist before
authentication or planning. An unsupported-version error reports the canonical
path, actual version, and supported versions. The recovery is to upgrade Aizim
to a release that supports the installed CLI or point the executable override
at a side-by-side supported installation; Aizim does not modify a global CLI.
The README and error guidance list the finite supported versions and this
side-by-side recovery path explicitly.

Resolution occurs once per readiness or Controller-start operation. The
canonical paths are passed into the Worker, Controller, and Codex sandbox
factories rather than rediscovered independently. Existing image hashes and
change detection remain binding for the lifetime of the backend.

### 8.3 Distribution environment separation

`AIZIM_CODEX_EXECUTABLE` and `AIZIM_CLAUDE_EXECUTABLE` are removed from the
all-or-nothing `DISTRIBUTION_ENVIRONMENT` set. The npm launcher continues to
inject only launcher-owned distribution metadata such as mode, version,
target, and manifest digests.

Provider executable overrides may be set independently in source or npm mode.
An unset override is normal and falls through to `PATH`.

The Rust launcher preserves the user's `PATH`; it no longer inserts a packaged
Codex directory or passes provider executable arguments.

## 9. Controller provider registry

Aizim gains one internal, immutable registry for Controller providers. This is
an internal source-code extension seam, not a dynamic plugin loader.

Each registry entry owns:

1. a stable provider ID;
2. executable resolution and compatibility probing;
3. Controller backend construction, including provider-specific preflight.

The initial registry contains `codex` and `claude`. CLI help and configuration
validation derive their displayed supported IDs from this registry instead of
duplicating a closed enum in several modules.

The existing `ControllerProvider(StrEnum)` in
`orchestration/control_plane.py` is removed and replaced by a
`ControllerProviderId` value type that validates only the identifier syntax.
The migration also removes every closed provider copy:

- `_CONTROLLER_PROVIDERS` in `state/control_operations.py`;
- both `codex | claude` validators in
  `state/schema_v1_orchestration.py`;
- the `codex | claude` check in `cli/control_projection.py`;
- `argparse` conversion through `ControllerProvider`;
- `ControllerProvider(...)` construction in `controller_supervisor.py`;
- annotations, exports, and tests that require the `StrEnum`.

The orchestration registry, rather than the identifier value type or event
schema, decides whether an identifier is supported by the running build.
`argparse` help may display the current registry IDs, but persisted state
remains a provider identifier rather than an enum member.

The user-facing `aizim controller configure` path checks registry membership
before appending an event. The persistence and replay layer accepts any
well-formed identifier so a newer log can be inspected by a build whose state
schema understands the identifier shape even when its orchestration registry
does not support execution.

The Controller backend protocol remains the stable runtime boundary:

- expose a `BackendIdentity`;
- run provider-specific `preflight`;
- accept a `ControllerContext`;
- return a validated `ControllerDecision`.

The registry does not own Workers. `create_codex_backend` and
`preflight_codex_worker` remain the fixed Worker path.

Adding a future built-in Controller provider requires:

- one adapter/backend module;
- one registry entry with its tested-version allowlist;
- provider-specific executable and preflight tests;
- shared Controller contract tests;
- a provider contract CI lane and user-facing compatibility documentation.

It does not require a Worker provider, sandbox provider, npm dependency, Rust
launcher change, or state-schema enum migration.

## 10. Persisted provider identity

The persisted `provider` value becomes a validated provider identifier rather
than a closed `codex | claude` schema enum.

The serialized form is a lowercase identifier matching:

```text
[a-z][a-z0-9_-]{0,63}
```

It is data only. It is never interpolated into a shell command, executable
path, module name, or import target.

State validation checks the identifier shape. Runtime configuration and
Controller start look it up in the immutable registry.

The two provider validation failures have distinct meanings:

- `CONTROLLER_PROVIDER_INVALID` means the value has the wrong type or does not
  match the identifier syntax;
- `CONTROLLER_PROVIDER_UNSUPPORTED` means the identifier is well formed but the
  running Aizim registry has no adapter for it.

Only `CONTROLLER_PROVIDER_UNSUPPORTED` is new. It does not replace the existing
invalid-value code.

Existing `codex` and `claude` events remain valid and require no data migration.
This split prevents future providers from requiring a state schema revision
while ensuring that unknown values cannot execute arbitrary code.

The broadened event schema is forward-compatible, not downgrade-compatible. A
current implementation that writes only `codex` or `claude` events remains
readable by the preceding build. Once a future build appends an event naming a
new provider, an older build with the closed enum cannot replay that log and
must fail rather than reinterpret it. Configuring a future provider therefore
creates an upgrade-only project-state boundary; reverting the executable does
not erase the historical event.

## 11. Runtime data flow

### 11.1 Resolved runtime snapshot

The "resolve once" rule is implemented explicitly, not left to call-site
discipline. A Controller-start operation creates one immutable
`ResolvedControllerRuntime` containing:

- the validated `ControllerProviderId`;
- one immutable Codex executable descriptor for Worker and sandbox roles;
- one immutable Controller executable descriptor, equal to the Codex
  descriptor when the provider is `codex`.

Each descriptor contains the canonical path, observed version, and SHA-256
digest established by the adapter resolver. Backend construction and every
launch revalidate against that descriptor rather than taking a second TOFU
snapshot.

`ControllerSupervisorDependencies` gains a runtime resolver and passes this
snapshot to its factories. Its production dependencies change in shape:

- `controller_backend` accepts the resolved runtime and model;
- `worker_backend` accepts the snapshot's Codex descriptor;
- `worker_preflight` accepts the same Codex descriptor;
- `create_codex_backend` accepts a descriptor instead of resolving one;
- `preflight_codex_worker` accepts a descriptor instead of resolving one;
- `ClaudeControllerBackend` accepts the Codex sandbox descriptor explicitly
  instead of calling `resolve_codex_executable`;
- the Codex Controller, Worker backend, Worker preflight, and sandbox compiler
  all receive paths from the same snapshot.

Tests may inject the complete snapshot or the resolver through
`ControllerSupervisorDependencies`. Other top-level operations such as
`doctor` and `security-probe` create their own one-per-command snapshot; the
requirement is one discovery per operation, not a process-global cache.

### 11.2 Startup sequence

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

## 12. Doctor and error behavior

`aizim doctor` derives its required provider checks from persisted project
configuration.

Checks that always apply include:

- Aizim runtime and project readiness;
- Lean prerequisites;
- mandatory Codex Worker executable and supported version;
- Codex-backed sandbox readiness;
- existing state, disk, and Python package checks.

### 12.1 Stable check set

Human and JSON output use a stable, role-oriented check set. The provider
portion of `_CHECK_IDS` becomes:

- `controller_configuration`;
- `controller_provider`;
- `controller_executable`;
- `controller_auth`;
- `worker_codex`;
- `sandbox_exec`.

Provider names are details, not dynamic check IDs. The old unconditional
`claude` and `codex` IDs are replaced by these role IDs as an explicit doctor
output compatibility change.

Every check has status `PASS`, `FAIL`, or `SKIP`. `ready` is false when any
check is `FAIL`; `SKIP` is non-failing and means a prerequisite failed or the
role is not applicable.

- An unconfigured project fails `controller_configuration`; downstream
  Controller checks are `SKIP`.
- A malformed or unsupported provider fails `controller_provider`; executable
  and auth checks are `SKIP`.
- A Codex Controller reuses the already-resolved Worker Codex path for
  `controller_executable`.
- A Claude Controller checks Claude through the generic
  `controller_executable` and `controller_auth` roles.
- If `worker_codex` fails, `sandbox_exec` is `SKIP`. If
  `controller_executable` fails, `controller_auth` is `SKIP`; a Claude
  executable failure does not suppress the independent Codex sandbox check.

There is no provider-specific Claude entry when Codex is selected, so a missing
Claude installation cannot affect readiness while the JSON key set stays
stable.

### 12.2 Existing and new error codes

The implementation preserves existing provider and sandbox errors rather than
silently renaming them:

| Failure boundary | Code |
| --- | --- |
| no persisted provider | `CONTROLLER_NOT_CONFIGURED` |
| malformed provider ID | `CONTROLLER_PROVIDER_INVALID` |
| well-formed ID absent from registry | `CONTROLLER_PROVIDER_UNSUPPORTED` (new) |
| mandatory Codex resolution | `CODEX_EXECUTABLE_UNAVAILABLE` |
| selected Claude resolution | `CLAUDE_EXECUTABLE_UNAVAILABLE` |
| invalid Controller executable path | `CONTROLLER_EXECUTABLE_UNAVAILABLE` |
| Codex Worker version outside allowlist | `UNSUPPORTED_CODEX_VERSION` |
| Controller version outside allowlist | `CONTROLLER_VERSION_UNSUPPORTED` |
| Codex Controller login | `CONTROLLER_LOGIN_FAILED` |
| Claude Controller authentication | `CONTROLLER_AUTH_FAILED` |
| Worker sandbox construction | `WORKER_SANDBOX_UNAVAILABLE` |
| Controller sandbox dependency resolution | `CONTROLLER_SANDBOX_UNAVAILABLE` |
| Controller sandbox compilation | `CONTROLLER_SANDBOX_INVALID` |

The generic `WORKER_CODEX_EXECUTABLE_UNAVAILABLE` and `SANDBOX_UNAVAILABLE`
codes proposed in the first draft are not introduced.

The exact CLI wording may add installation guidance, but it must not execute an
installer or imply that Aizim owns the agent installation.

## 13. Sandbox and auxiliary tools

This design does not introduce a sandbox registry or generic sandbox provider.
The existing macOS and Linux adapters remain keyed from the supported Codex
installation:

- macOS continues to require the existing `sandbox-exec` contract;
- Linux continues to validate the Codex-adjacent sandbox mechanism;
- an invalid or unavailable host mechanism fails closed.

The external-install change must not weaken path canonicalization, runtime-root
allowlisting, filtered provider environments, command-tail validation, or
executable image hashing. These controls detect incompatibility and
post-construction replacement; as stated in Section 4, they do not establish
the provenance or benevolence of the initially selected external executable.

Ripgrep resolution continues to prefer `rg` on `PATH`. A compatible
Codex-adjacent `codex-path/rg` may remain a fallback when that external Codex
layout provides it. Failure is reported as a ripgrep requirement, not repaired
by installing an npm agent package.

## 14. npm and Rust changes

The npm distribution boundary removes provider knowledge:

- remove both agent packages from `package.json` and `package-lock.json`;
- remove provider package aliases and versions from platform metadata;
- remove JavaScript modules that resolve packaged Codex or Claude executables;
- remove agent executable fields from distribution manifests;
- remove `--codex-executable` and `--claude-executable` launcher arguments;
- remove provider executable verification and injection from the Rust launcher;
- remove provider-specific package assertions from build, pack, contents, and
  release verification;
- remove `DistributionContext.codex_executable` and
  `DistributionContext.claude_executable`;
- remove the npm-mode branches from `resolve_codex_executable` and
  `resolve_claude_executable`;
- remove the npm-specific branch from `resolve_ripgrep_executable`; its
  external-Codex fallback is mode-independent.

The launcher still verifies Aizim-owned wheels, manifests, uv, Python runtime
artifacts, and platform executables. Removing agent ownership does not weaken
verification of artifacts that remain inside the distribution boundary.

`THIRD_PARTY_NOTICES.md` needs no provider removal:
`scripts/npm/write-notices.mjs` currently emits uv plus non-development Rust
dependencies and contains no Codex, Claude, OpenAI, or Anthropic entry. The
notice-generation test remains in scope to prove the file is unchanged for the
right reason.

### 14.1 Existing npm installations

Upgrading from a build that bundled both CLIs does not delete or modify any
user-managed agent installation or credentials. The upgraded Aizim package
ignores nested legacy agent packages because it no longer resolves them.

Before the first Controller start after upgrade, the user must make a supported
Codex installation available through `AIZIM_CODEX_EXECUTABLE` or `PATH`. A
Claude-configured project must likewise expose Claude. Existing project
configuration and state remain intact. If the old package-local agent was the
only installation, `--version` and `--help` still work, while `doctor` and
Controller start fail with the external-install guidance. Aizim never copies
the old nested executable into the new trust boundary.

## 15. Danus patterns used as a reference

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

## 16. Test and CI design

### 16.1 Unit tests

Unit tests use temporary executable fixtures and explicit environments. They
cover:

- override-before-`PATH` precedence for each built-in provider;
- missing and invalid executable errors;
- independent optional overrides;
- tested-version allowlist success and compatibility failures, including
  actual and supported version details;
- registry lookup and unsupported IDs;
- removal of `ControllerProvider(StrEnum)` and every closed provider copy;
- provider identifier syntax and non-execution;
- no import-time environment capture;
- one runtime snapshot feeding Controller, Worker, preflight, and sandbox;
- no hidden Codex discovery inside the Claude backend;
- stable doctor IDs and `PASS | FAIL | SKIP` behavior;
- canonical path, version, and SHA-256 trust evidence;
- sandbox image and executable change detection;
- absence of fallback between providers.

Tests must not borrow a globally installed Codex, Claude, `bwrap`, or ripgrep.

### 16.2 Python integration tests

Integration tests cover:

- unconfigured project readiness failure;
- Codex Controller plus Codex Worker with no Claude executable;
- Claude Controller plus Codex Worker;
- selected Claude missing while Codex is present;
- mandatory Codex missing while Claude is present;
- unregistered provider state;
- stable role-based `doctor` text and JSON output;
- successful Controller-to-Codex-Worker dispatch with injected backends;
- fail-closed sandbox behavior.

### 16.3 npm and Rust distribution tests

Distribution tests prove:

- the packed Aizim package contains no agent package dependency;
- a clean npm install runs no Codex or Claude lifecycle script;
- `aizim --version` and `aizim --help` work with neither agent installed;
- launcher arguments, manifests, and environment contain no agent executable
  fields;
- no dead npm-mode provider fields or resolver branches remain;
- source and packed builds do not resolve agent npm packages;
- upgrading leaves user-managed agents and credentials untouched;
- third-party notices remain provider-free without a provider-specific rewrite;
- Aizim-owned artifact integrity checks remain intact.

### 16.4 CI lanes

Core Python, npm, Rust, package-install, and Lean integration lanes do not
install Claude merely to satisfy generic tests.

Two provider contract jobs are required:

- `provider-contract-codex` explicitly installs a supported external Codex and
  exercises external resolution, version evidence, Worker/sandbox discovery,
  and the Codex Controller command contract;
- `provider-contract-claude` explicitly installs supported external Codex and
  Claude CLIs and exercises the Claude Controller plus Codex Worker/sandbox
  composition.

Those installations belong to the job setup, not the package dependency graph.
Authentication-dependent model calls remain separately authorized manual
smokes. Core and offline integration tests use fake executables, while the two
required contract jobs prove that the supported real CLI package layouts and
version outputs have not drifted. The jobs run on the native target matrix for
which Aizim claims that provider composition and sandbox layout are supported.

## 17. Acceptance criteria

The implementation is complete only when all of the following are observed:

1. Installing the packed npm artifact with no agent packages succeeds.
2. `aizim --version` and `aizim --help` work with no agent CLI on `PATH`.
3. Readiness evidence identifies the canonical external executable path,
   observed version, and SHA-256 digest without claiming provider provenance.
4. A CLI version outside its adapter's tested allowlist fails with the actual
   and supported versions and side-by-side override guidance.
5. A project cannot start a Controller until a provider is explicitly
   configured.
6. A Codex-configured project does not resolve or check Claude.
7. A Claude-configured project requires both Claude Controller readiness and
   Codex Worker/sandbox readiness.
8. Missing selected dependencies produce the preserved, distinct error codes
   specified in Section 12.2.
9. Unknown provider identifiers do not execute code and fail as unsupported,
   while malformed identifiers remain invalid.
10. One resolved runtime snapshot supplies Controller, Worker, preflight, and
    sandbox paths during each Controller-start operation.
11. `doctor` text and JSON use the fixed role check set and deterministic
    `PASS | FAIL | SKIP` semantics.
12. Codex sandbox enforcement remains fail-closed on supported platforms.
13. Provider tests pass with isolated fake executables and a stripped `PATH`.
14. Both required real-CLI provider contract jobs pass.
15. Upgrading from the bundled-agent build preserves project state and user
    credentials while requiring an explicitly discoverable external Codex.
16. Package, Python, Rust, architecture, and relevant platform test suites pass.
17. CI no longer installs Claude in unrelated Lean or core jobs.
18. The correction lands as forward commits without rewriting pushed history.
