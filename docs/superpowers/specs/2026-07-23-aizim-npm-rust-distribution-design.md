# Aizim npm and Rust Distribution

**Status:** Approved architecture, written specification for user review

**Date:** 2026-07-23

**Scope:** Installable npm distribution, Rust bootstrap launcher, source builds
through npm, and qualified macOS and Linux packages

## 1. Purpose

Aizim currently has a uv-native Python development and execution workflow. This
design adds an npm distribution surface so that a user can install the CLI
without first installing Python, uv, or Rust:

```sh
npm install --global @aiz.im/aizim
aizim --version
```

It also adds an npm-owned source-build surface:

```sh
npm install
npm run build
```

The npm distribution does not replace the Python control plane. It packages the
existing Aizim wheel, selects a prebuilt platform package, provisions an
isolated Python 3.12 runtime with a bundled pinned uv executable, and then runs
the same Python CLI.

Rust owns the small native bootstrap and process-lifecycle boundary required by
this distribution. It does not own research orchestration, capability policy,
persistent state, promotion policy, Lean integration, or mathematical truth.

## 2. Confirmed decisions

The following decisions are fixed for this implementation:

1. The npm scope is `@aiz.im`. The account `frankieew` is already an owner of
   that npm organization.
2. The user-facing package is `@aiz.im/aizim`.
3. Platform binaries are distributed as four optional packages:
   - `@aiz.im/aizim-darwin-arm64`
   - `@aiz.im/aizim-darwin-x64`
   - `@aiz.im/aizim-linux-arm64`
   - `@aiz.im/aizim-linux-x64`
4. macOS arm64, macOS x64, Linux arm64, and Linux x64 are the initial target
   matrix.
5. Initial Linux support means glibc Linux. Alpine and other musl-only
   distributions are not supported by this release.
6. End-user installation requires Node.js but does not require Python, uv,
   Rust, or a global Codex installation.
7. Source builds are orchestrated by npm. They require Node.js/npm and the
   pinned Rust toolchain, but not a preinstalled Python or uv executable.
   Repository and release builds pin Node.js `26.5.0`, npm `12.0.1`, and Rust
   `1.97.1`. JavaScript editor diagnostics use TypeScript `6.0.2`, the newest
   release that still provides the tsserver protocol; TypeScript `7.0.2`
   replaces that integration with the native Go toolchain and is not a
   compatible tsserver provider.
8. Installation has no `postinstall` or other lifecycle script that executes
   downloaded native code.
9. Python and its dependencies are provisioned lazily on the first `aizim`
   invocation.
10. The npm runtime uses CPython 3.12. The existing uv-native source workflow
    continues to support the range declared in `pyproject.toml`.
11. uv is pinned to `0.11.31`, matching the Python build backend pin.
12. Codex is an exact npm dependency at `@openai/codex@0.145.0`. An npm
    installation never falls back to a global Codex executable.
13. Lean and elan remain external prerequisites. `aizim doctor` reports their
    readiness; the npm package does not install or bundle a Lean toolchain.
14. The GitHub repository remains private. The npm packages are public so that
    users can install them without npm organization membership.
15. No npm package is published until the user separately authorizes the
    external release action.
16. Distribution-critical and developer-tool pins are refreshed to the latest
    stable compatible releases at implementation time and recorded explicitly;
    they are never silently downgraded to satisfy one local tool.

## 3. Goals and non-goals

### 3.1 Goals

- Make local and global npm installation work through the normal npm CLI.
- Make a clean source checkout build the current host package through
  `npm install && npm run build`.
- Preserve the existing Aizim CLI arguments, stdin, stdout, stderr, exit
  status, and signal behavior.
- Provision a versioned runtime automatically and atomically on first use.
- Pin and verify all distribution-critical artifacts.
- Keep global Python, global uv, and global Codex versions from affecting an
  npm-installed Aizim.
- Preserve the uv-native Python developer workflow.
- Add a real Linux sandbox adapter and require the existing fail-closed
  security gate before Linux is called supported.
- Build and test every declared platform on a native CI runner.
- Produce npm tarballs with explicit file allowlists and third-party notices.

### 3.2 Non-goals

- Rewriting the Python control plane in Rust.
- Moving SQLite state, the gateway, the document broker, orchestration,
  promotion, evaluation, or Lean integration into Rust.
- Adding project-owned C business logic.
- Treating a Rust process as proof of a stronger operating-system identity
  boundary.
- Supporting Windows.
- Supporting Alpine or other musl-only Linux distributions in the first
  release.
- Bundling Lean, elan, a Lean project, or a model credential.
- Providing a fully offline first installation.
- Silently running without the required platform sandbox.
- Changing the formal-truth rule: only Lean-accepted declarations enter
  verified formal state.
- Publishing to npm as part of ordinary build or test commands.

## 4. User experience

### 4.1 Global installation

```sh
npm install --global @aiz.im/aizim
aizim --version
aizim doctor --project /absolute/path/to/lean-project
```

The first invocation may download CPython 3.12 and locked Python dependency
artifacts. It prints a concise bootstrap status to stderr while preserving
stdout for the requested CLI command.

### 4.2 Project-local installation

```sh
npm install @aiz.im/aizim
npx aizim --version
```

The local and global entry points execute the same package and runtime
bootstrap.

### 4.3 Source checkout

```sh
npm install
npm run build
npm test
npm run pack
```

`npm run build` builds only the current host target. Four-platform release
builds run as independent native CI jobs and are assembled by the release
workflow.

The existing workflow remains valid:

```sh
uv sync --frozen
uv run aizim --version
```

## 5. Package topology

```text
@aiz.im/aizim
├── bin/aizim.js
├── manifest/distribution.json
├── vendor/aizim-0.1.0-py3-none-any.whl
├── vendor/runtime-requirements.txt
├── LICENSE
├── THIRD_PARTY_NOTICES.md
├── dependency: @openai/codex@0.145.0
└── optionalDependencies
    ├── @aiz.im/aizim-darwin-arm64@0.1.0
    ├── @aiz.im/aizim-darwin-x64@0.1.0
    ├── @aiz.im/aizim-linux-arm64@0.1.0
    └── @aiz.im/aizim-linux-x64@0.1.0
```

Each platform package contains:

```text
@aiz.im/aizim-<platform>-<arch>
├── internal platform resolver module
├── bin/aizim-launcher
├── manifest/platform.json
├── vendor/uv
├── LICENSE
└── THIRD_PARTY_NOTICES.md
```

The meta package uses exact optional dependency versions, not ranges. npm's
`os`, `cpu`, and, for Linux, `libc` package metadata makes npm install only the
compatible package.

The platform package resolver is an internal contract consumed by
`@aiz.im/aizim`; it is not a supported public JavaScript API.

## 6. Repository layout

The source repository gains this structure:

```text
Aizim/
├── package.json
├── package-lock.json
├── rust-toolchain.toml
├── crates/
│   └── aizim-launcher/
│       ├── Cargo.toml
│       └── src/
├── npm/
│   └── platforms/
│       ├── darwin-arm64/
│       ├── darwin-x64/
│       ├── linux-arm64/
│       └── linux-x64/
└── scripts/
    └── npm/
```

The root `package.json` is both the npm workspace root and the publishable meta
package. Local workspace packages satisfy its platform optional dependencies
during development, so a clean source `npm install` does not depend on
previously published Aizim platform packages.

The existing Python source layout remains unchanged except for narrowly scoped
runtime-distribution and Linux sandbox integration.

## 7. Component responsibilities

### 7.1 JavaScript entry point

`bin/aizim.js` is a small Node.js dispatcher. It:

1. Maps `process.platform` and `process.arch` to one supported package name
   and rejects a Linux host that does not report glibc.
2. Resolves the installed platform package through Node's package resolver.
3. Resolves the exact local `@openai/codex` executable.
4. Resolves the wheel and the two distribution manifests.
5. Starts the Rust launcher with inherited stdio.
6. Forwards termination signals when the wrapper itself receives them.
7. Exits with the launcher's exit status or matching signal status.

It does not provision Python, calculate policy, parse Aizim arguments, or
fallback to a command found on `PATH`.

If the compatible optional package is absent, the wrapper fails before running
anything and explains that the likely causes are an unsupported platform,
unsupported libc, a corrupted installation, or installation with
`--omit=optional`.

The package requires Node.js `>=22.14.0`. Repository and release automation
uses the exact Node.js `26.5.0` and npm `12.0.1` pair. A compatibility job also
exercises the minimum supported Node.js version.

### 7.2 Rust launcher

`aizim-launcher` is a synchronous, narrowly scoped executable. It:

1. Parses only launcher-owned arguments placed before `--`; every argument
   after `--` is passed to the Python CLI unchanged.
2. Loads and validates the distribution and platform manifests.
3. Verifies the wheel, locked requirements artifact, and bundled uv executable
   with SHA-256.
4. Computes the versioned runtime cache key.
5. Takes an advisory exclusive lock for that key.
6. Reuses a complete verified environment or provisions a staging environment.
7. Atomically promotes a successfully provisioned staging directory.
8. Revalidates the ready marker and Python entry point.
9. Uses Unix `exec` to replace itself with the installed Python `aizim`
   executable.

It has no network client of its own. It invokes the bundled uv executable for
Python and package downloads.

It contains no research logic, authorization policy, sandbox policy, database
access, model transport, or Lean logic. It never receives capability secrets.

The launcher is not an authenticated authority helper and has no IPC protocol.
A future helper intended to remove same-UID trust would require a separate
design and adoption gate.

### 7.3 Python runtime

The installed Python wheel remains the implementation of the `aizim` and
`aizim-gateway-sidecar` commands.

When launched from npm, a distribution context supplies:

- the exact resolved Codex executable;
- the npm distribution version;
- the target platform identifier;
- the platform and distribution manifest hashes.

These values are consumed by the trusted composition root. They are not passed
into model-controlled shell environments.

The npm distribution mode requires the injected Codex executable. The Python
runtime validates its absolute path and exact `codex-cli 0.145.0` version and
does not search `PATH` as a fallback. Direct uv development retains the current
explicit host-resolution path.

### 7.4 Sandbox adapters

`MacOSSandboxAdapter` remains the macOS implementation and retains its existing
Seatbelt and Gate B behavior.

A new `LinuxSandboxAdapter` implements the existing `SandboxAdapter` contract.
It delegates platform enforcement to the exact bundled Codex CLI and the same
versioned permission-profile semantics used by Aizim. The design does not
assume that an unqualified Codex implementation detail, such as bubblewrap or
Landlock, is sufficient by itself.

Linux readiness is established only by running the full Aizim security probe
against the pinned packaged Codex executable. The protected filesystem,
environment, Unix socket, TCP, scratch, and read-only view operations must
produce the same expected allow and deny verdicts as the macOS contract.

The current Codex launch assembly contains a macOS-specific validation call.
That responsibility moves behind a platform-neutral adapter validation
boundary so that:

- macOS continues to validate the exact macOS profile;
- Linux validates its own exact launch contract;
- the backend cannot accept a launch spec that was not validated by its
  selected adapter.

If the Linux host or packaged Codex cannot satisfy Gate B, `doctor`,
`security-probe`, and autonomous `run` fail closed. There is no unsandboxed
fallback and no release claim for that target.

## 8. Distribution manifests and integrity

### 8.1 Distribution manifest

`manifest/distribution.json` contains:

- schema version;
- Aizim package version;
- Python requirement `3.12`;
- wheel filename, size, and SHA-256;
- locked runtime-requirements filename and SHA-256;
- expected Codex package version `0.145.0`;
- minimum Node.js version;
- compatible platform manifest schema version.

### 8.2 Platform manifest

`manifest/platform.json` contains:

- schema version;
- Aizim package version;
- npm platform package name;
- Node platform and architecture;
- Rust target triple;
- Linux libc requirement where applicable;
- launcher build version;
- bundled uv version `0.11.31`;
- uv filename, size, and SHA-256;
- compatible distribution manifest schema version.

The four Rust target triples are:

- `aarch64-apple-darwin`
- `x86_64-apple-darwin`
- `aarch64-unknown-linux-gnu`
- `x86_64-unknown-linux-gnu`

The matching uv release archives are:

- `uv-aarch64-apple-darwin.tar.gz`
- `uv-x86_64-apple-darwin.tar.gz`
- `uv-aarch64-unknown-linux-gnu.tar.gz`
- `uv-x86_64-unknown-linux-gnu.tar.gz`

Build scripts download those exact `0.11.31` artifacts over HTTPS and verify
them against committed release checksums before packaging.

### 8.3 Security meaning

npm tarball integrity is the primary transport-integrity layer. Rust manifest
verification additionally detects partial installation, accidental corruption,
and mismatched artifacts before execution.

The manifests and launcher are installed under the same Unix user. Their hashes
are not a signature and do not protect against a malicious process with the
same write authority. This distribution does not claim to remove the current
same-UID trust assumption.

## 9. Runtime provisioning and cache

### 9.1 Cache location

The default roots are:

- macOS: `$HOME/Library/Caches/aizim`
- Linux: `$XDG_CACHE_HOME/aizim`, or `$HOME/.cache/aizim` when
  `XDG_CACHE_HOME` is unset

`AIZIM_CACHE_DIR` may override the root for testing or administration. A
relative override is rejected.

The runtime key includes:

- cache schema version;
- Aizim version;
- target platform;
- wheel SHA-256;
- Python major and minor version.

Different Aizim releases never mutate one another's runtime directories.

### 9.2 Locked Python dependencies

The build derives a runtime requirements artifact from the committed
`uv.lock`. It contains the exact non-development dependency solution and
artifact hashes required by the Aizim wheel.

Provisioning uses the bundled uv executable to:

1. obtain a managed CPython 3.12 interpreter when one is not already present in
   the Aizim-managed cache;
2. create a virtual environment in a same-filesystem staging directory;
3. install the locked dependency set with hash verification;
4. install the bundled Aizim wheel without re-resolving dependencies;
5. verify that the `aizim` and `aizim-gateway-sidecar` entry points exist;
6. run `aizim --version` inside the staging environment;
7. write the ready marker last;
8. atomically rename the staging directory to the final cache key.

The bootstrap does not use a global Python, global uv cache, or global Python
package environment.

Every bootstrap uv command uses `--no-config`, explicit package-index and
managed-Python settings, and a filtered environment. User-level uv and pip
configuration and `UV_*` or `PIP_*` package-selection overrides cannot change
the locked dependency solution. Standard proxy, certificate, locale, and
temporary-directory settings may be preserved, but credential-bearing values
are never copied into diagnostics.

The first successful bootstrap requires network access unless all required
Python and package artifacts already exist in the Aizim-managed cache.
Subsequent launches of the same cache key require no dependency resolution or
download.

### 9.3 Concurrency and recovery

An advisory file lock serializes bootstrap for one cache key. Operating-system
lock release handles process death; a persistent lock file is not itself
treated as an active lock.

A failed or interrupted bootstrap never writes the final ready marker and never
promotes the staging directory. A later invocation removes only stale staging
directories owned by the same cache key and starts again.

An upgrade builds a new cache key and leaves older complete environments
untouched. Runtime startup does not silently delete old versions. Cache
inspection or cleanup can be added later as an explicit operator command.

## 10. Process, signal, and exit behavior

The JavaScript dispatcher starts the Rust launcher with inherited stdin,
stdout, and stderr. The Rust launcher uses inherited handles for bootstrap
commands and then replaces itself with the Python entry point using Unix
`exec`.

After Python starts:

- Aizim receives the original arguments unchanged;
- stdout and stderr are the caller's streams;
- interactive stdin remains interactive;
- Python CLI exit codes are preserved;
- terminal process-group signals reach the active command;
- the JavaScript wrapper mirrors child signal termination when direct
  forwarding is required.

Launcher-only failures use stable BSD `sysexits`-compatible values outside the
existing Aizim CLI range:

| Exit | Meaning |
| --- | --- |
| `69` | Python runtime or dependency provisioning unavailable |
| `70` | Internal launcher or bootstrap invariant failure |
| `74` | Manifest, digest, filesystem, or other I/O integrity failure |
| `78` | Unsupported platform or missing/mismatched npm package configuration |

Once Python begins, the launcher does not remap its exit status.

Errors identify the failing layer, expected and observed versions where safe,
the cache key, and a concrete remediation. They do not print capability
material, model credentials, registry tokens, authenticated URLs, or the full
environment.

## 11. Build system

### 11.1 Root commands

The root npm scripts provide these stable interfaces:

```text
npm run build       build the wheel, launcher, and current-host packages
npm test            run Node, Rust, Python, and package-contract tests
npm run pack        create verified current-host and meta-package tarballs
npm run check       run formatting, lint, type, version, and manifest checks
```

Internal scripts may expose narrower commands, but documentation and CI use the
stable root interfaces.

### 11.2 Build order

`npm run build`:

1. checks the supported host and required Rust toolchain;
2. checks all manifest versions for exact equality;
3. bootstraps the pinned uv build tool into a repository-local build cache;
4. builds the Aizim wheel with `uv build --wheel`;
5. exports the locked runtime dependency artifact;
6. builds `aizim-launcher` with `cargo build --release --locked`;
7. obtains and verifies the matching uv release archive;
8. assembles the current platform package;
9. assembles the meta package;
10. validates manifests, hashes, executable bits, licenses, and file allowlists;
11. records a build summary containing artifact paths and SHA-256 values.

Build output is confined to ignored build directories. It does not edit source
versions or tracked manifests.

`npm run pack` refuses stale output. It rechecks that each packed artifact was
built from the current source inputs and then uses `npm pack --json` to produce
the tarballs.

### 11.3 Version synchronization

The initial version is `0.1.0`. These values must be identical:

- `pyproject.toml`
- root `package.json`
- all four platform `package.json` files
- Rust crate package version
- exact platform versions in `optionalDependencies`
- distribution and platform manifests

A version check fails the build on any mismatch. It does not rewrite files
automatically.

### 11.4 Reproducibility inputs

The repository commits:

- `uv.lock`
- `package-lock.json`
- `Cargo.lock`
- `rust-toolchain.toml`
- `.node-version` containing `26.5.0`
- root `packageManager` metadata selecting `npm@12.0.1`
- uv release URLs and SHA-256 values
- package file allowlists
- third-party license notices

No release build uses floating package versions, Rust dependencies, toolchain
channels, GitHub Actions tags, or uv download URLs.

## 12. Testing and qualification

### 12.1 Node tests

Node's built-in test runner covers:

- all supported platform mappings;
- unsupported platform and architecture errors;
- a missing optional package;
- an installation made with `--omit=optional`;
- local Codex resolution and rejection of a global fallback;
- launcher argument separation at `--`;
- stdio inheritance;
- normal exit-code propagation;
- signal termination propagation;
- manifest path selection.

### 12.2 Rust tests

Rust tests cover:

- strict manifest parsing and schema compatibility;
- platform and version mismatches;
- digest and file-size verification;
- cache-key determinism;
- absolute cache override validation;
- advisory locking and concurrent bootstrap;
- interrupted staging recovery;
- ready-marker validation;
- command construction with hostile-looking user arguments;
- launcher-only exit classification;
- rejection of unexpected paths and artifact types.

### 12.3 Python tests

Python tests cover:

- npm distribution-context parsing;
- exact injected Codex resolution;
- no global Codex fallback in npm mode;
- unchanged direct uv development behavior;
- platform-neutral sandbox validation dispatch;
- Linux launch-contract construction;
- Linux host readiness failures;
- `doctor`, `security-probe`, and autonomous-run fail-closed behavior;
- preservation of the existing macOS launch contract.

### 12.4 Package integration tests

For each target, CI creates both the platform tarball and meta tarball, then
installs them together into a fresh temporary consumer. This allows the exact
unpublished platform version to satisfy the meta package's optional dependency.

The consumer tests:

1. local package installation;
2. `npx aizim --version`;
3. isolated first-run Python provisioning;
4. a second run using the completed cache;
5. `aizim doctor`;
6. installation into a temporary global npm prefix;
7. invocation of the global `aizim` command;
8. package removal without damage to unrelated cache paths;
9. the missing-optional-package negative path.

Tests use private temporary `HOME`, npm prefix, and Aizim cache directories.
They do not modify the developer's global npm, Python, uv, Codex, or Aizim
state.

### 12.5 Sandbox and Aizim acceptance

Packaging is not sufficient evidence for platform support.

Each declared target must execute:

- `aizim doctor` ending in `READY`;
- the full no-model security probe ending in `SECURITY GATE PASS`;
- the deterministic shared fake-backend smoke run;
- the existing acceptance checker ending in `AIZIM RUN PASS`.

macOS arm64 and x64 must retain the existing macOS sandbox contract. Linux
arm64 and x64 must pass the same operation-level Gate B expectations through
`LinuxSandboxAdapter`.

The credentialed real-Codex run remains a separately authorized manual gate.
No workflow receives a model credential merely to test npm packaging.

## 13. CI design

Pull requests and main-branch pushes run:

1. the existing Python, Lean, lint, and type-check workflows;
2. Node tests and package metadata checks;
3. Rust formatting, Clippy with warnings denied, and tests;
4. native build and package integration jobs for all four targets;
5. macOS and Linux Gate B jobs on their matching native hosts;
6. tarball file-allowlist, license, version, and digest verification.

Each native job uploads:

- its platform tarball;
- its platform manifest;
- SHA-256 values;
- test and Gate B results;
- the exact full Git commit SHA.

The aggregate job rejects duplicate targets, missing targets, version
mismatches, digest mismatches, or evidence from another commit.

GitHub Actions dependencies are pinned to immutable full commit SHAs with
human-readable version comments.

## 14. npm release design

### 14.1 Release order

A version tag matching every package manifest starts a protected release
workflow:

1. rerun the four native release builds without restoring build caches;
2. verify all platform evidence and artifact hashes at the tag commit;
3. assemble and verify the meta package;
4. publish the four platform packages first;
5. query npm until all four exact versions and dist-integrity values are
   visible;
6. publish `@aiz.im/aizim` last;
7. install the registry package on each real platform and repeat the version,
   doctor, Gate B, and deterministic-run checks.

If any platform publication fails, the meta package is not published. npm
versions are immutable, so a partial release is completed with the identical
verified tarballs or superseded by a new version; existing versions are never
overwritten.

### 14.2 Access and authentication

All five scoped packages set public publish access. The private GitHub
repository remains the source repository.

The first release requires a separately authorized interactive publication
because npm Trusted Publishing can only be configured after each package
exists.

After the first release, each package is configured with the same exact
GitHub-hosted release workflow as its npm Trusted Publisher. The workflow uses
short-lived OIDC identity, a protected GitHub Environment, read-only repository
permissions except for `id-token: write`, and no long-lived npm write token.

npm currently permits Trusted Publishing from a private GitHub repository, but
does not generate npm provenance attestations for a private source repository.
This known limitation is documented rather than represented as successful
provenance.

### 14.3 Publication authorization

Build, test, and pack commands never publish. CI release jobs remain
non-runnable until the npm environment and publisher configuration are
explicitly enabled.

Creating the packages, publishing a version, configuring Trusted Publishers,
or changing npm access is an external write and requires explicit user
authorization at the time of action.

## 15. Security properties

- npm installation executes no lifecycle script.
- The meta package resolves only exact package versions.
- The npm runtime never selects a global Codex executable.
- uv, wheel, requirements, and manifests are versioned and hashed.
- Runtime construction is locked, staged, verified, and atomically promoted.
- Model-controlled processes do not receive npm distribution metadata,
  bootstrap environment variables, registry credentials, or cache write
  access.
- Linux autonomous execution is denied unless its real sandbox probe passes.
- No package contains model credentials, npm credentials, capability tokens,
  user state, or generated `.aizim/` data.
- Package file allowlists prevent accidental publication of the repository,
  tests, private documents, or local configuration.
- MIT license text and required third-party notices ship in every package that
  contains redistributed artifacts.

These properties improve distribution integrity and lifecycle ownership. They
do not establish a distinct Unix identity, defend against a malicious same-UID
writer, or change Lean's authority.

## 16. Compatibility and migration

The npm distribution is additive:

- existing `uv run aizim` behavior remains supported;
- the same Python wheel backs uv and npm execution;
- existing CLI command syntax and exit codes remain unchanged after bootstrap;
- existing macOS sandbox evidence remains binding;
- new Linux support is gated independently;
- persisted `.aizim/` state is not migrated by the launcher;
- uninstalling npm packages does not delete user projects or Aizim runtime
  caches.

README and the foundation runbook gain separate uv-source and npm-install
instructions so that one path is never mistaken for the other.

## 17. Acceptance criteria

The implementation is complete only when all of the following are observed:

1. A clean source checkout on each of the four targets succeeds with
   `npm install && npm run build`.
2. On each target, `npm test` passes the Node, Rust, Python, packaging, and
   matching host sandbox suites.
3. `npm run pack` produces exactly one current-platform tarball and one meta
   tarball with approved contents and matching `0.1.0` versions.
4. A fresh local consumer can install those tarballs and run
   `npx aizim --version`.
5. A fresh temporary global prefix can install those tarballs and run
   `aizim --version`.
6. The first invocation provisions Python 3.12 without using global Python or
   uv.
7. The second invocation reuses the complete versioned environment.
8. An npm-installed Aizim uses only its exact local Codex `0.145.0`.
9. macOS arm64 and x64 finish `READY`, `SECURITY GATE PASS`, and
   `AIZIM RUN PASS`.
10. Linux arm64 and x64 finish `READY`, `SECURITY GATE PASS`, and
    `AIZIM RUN PASS`.
11. Missing optional packages, failed integrity checks, bootstrap interruption,
    and unavailable sandbox enforcement fail closed with the specified exit
    categories.
12. The existing uv-native test and acceptance workflows remain green.
13. The public registry installation succeeds on all four targets after a
    separately authorized publication.

Until item 13 is authorized and observed, the implementation may be described
as release-ready and locally installable, but not as publicly available from
npm.
