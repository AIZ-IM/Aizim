# Aizim npm and Rust Distribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship Aizim as `@aiz.im/aizim` with four native platform packages, a
Rust bootstrap launcher, automatic isolated Python 3.14.6 provisioning, a clean
`npm install && npm run build` source workflow, and qualified macOS and Linux
sandbox execution.

**Architecture:** A small JavaScript entry point selects an exact platform
package and the native executable from `@openai/codex@0.145.0`. A Rust launcher
verifies packaged artifacts, atomically provisions a uv-managed runtime, then
execs the existing Python CLI. Python remains the control plane, platform
sandbox adapters remain the fail-closed authority boundary, and Lean remains
the sole formal-truth boundary.

**Tech Stack:** Node.js 26.5.0 for repository builds with a runtime floor of
22.22.2, npm 12.0.1, Rust 1.97.1 edition 2024, serde 1.0.229,
serde_json 1.0.151, sha2 0.11.0, fs2 0.4.3, tempfile 3.27.0, Python
3.14.6 for npm runtimes, uv 0.11.31, Codex CLI 0.145.0, Lean 4.32.1,
pytest 9.1.1, Ruff 0.15.22, ty 0.0.63, and TypeScript 6.0.2 as the newest
tsserver-compatible JavaScript diagnostics provider.

## Global Constraints

1. The canonical specification is
   `docs/superpowers/specs/2026-07-23-aizim-npm-rust-distribution-design.md`
   with its approved baseline at commit
   `09fab5fe99878b29951a4471a3aa2ff657a90050` and the user's subsequent
   latest-compatible-version refresh. Stop and revise the specification if
   implementation pressure conflicts with it.
2. The user explicitly requested no worktree. Execute in the current worktree
   and preserve concurrent user changes.
3. Keep `.omc/`, `docs/future-native-language-boundary.md`, and the existing
   `docs/superpowers/plans/2026-07-20-aizim-slices-1-2.md` untouched and
   untracked. Never stage them. This new plan is the only authorized addition
   under `docs/superpowers/plans/`.
4. Use test-driven development for every behavioral task: add the named
   failing test, run it and observe the stated failure, implement the smallest
   complete behavior, then rerun the narrow and aggregate checks.
5. Preserve Python as the control/research plane, Rust as the distribution and
   lifecycle launcher, C only through established dependencies, and Lean as
   the sole formal-truth boundary.
6. Do not rewrite state, gateway, broker, orchestration, promotion, evaluation,
   or Lean integration in Rust.
7. The npm install path must execute no lifecycle script. Root and platform
   packages may not define `preinstall`, `install`, `postinstall`, `prepare`,
   or `prepublish` scripts.
8. Pin `@openai/codex` exactly to `0.145.0`; npm mode must resolve its local
   native executable and must never fall back to global `PATH`.
9. Pin uv exactly to `0.11.31`; verify the official archive SHA-256 before
   extracting or executing it.
10. The four Aizim packages are exactly
    `@aiz.im/aizim-darwin-arm64`,
    `@aiz.im/aizim-darwin-x64`,
    `@aiz.im/aizim-linux-arm64`, and
    `@aiz.im/aizim-linux-x64`, all at the same version as
    `@aiz.im/aizim`.
11. Initial Linux support is glibc only. Windows and musl-only Linux are
    unsupported and must fail with exit 78 before native execution.
12. Keep the existing uv-native Python path working for Python 3.12–3.14.
    npm mode provisions managed CPython 3.14.6 only.
13. Linux is supported only after real `READY`, `SECURITY GATE PASS`, and
    `AIZIM RUN PASS` evidence on both Linux architectures. There is no
    unsandboxed fallback.
14. Keep all model credentials, npm credentials, raw capabilities, authenticated
    URLs, and full environments out of logs, manifests, subprocess arguments,
    durable events, and package tarballs.
15. Preserve the existing state RPC contract SHA
    `f513efd625dd5abbe4f942e89024a571f5c4617fe6abcff4dbe4e51f8faebfcc`.
    This work does not modify state RPC shapes.
16. Keep production Python files within the project's current 250
    nonblank/noncomment line limit. Keep new Node and Rust files focused by
    responsibility rather than creating one large launcher or build script.
17. Build output belongs only under ignored `build/npm/`, `dist/npm/`, and
    Cargo `target/` directories. Build commands do not rewrite tracked
    versions or manifests.
18. Every task ends with its narrow tests, relevant aggregate checks,
    `git diff --check`, and one focused commit. Stage only the paths listed in
    that task. Do not push unless the user separately asks.
19. GitHub Actions dependencies use immutable full commit SHAs. Native jobs use
    `macos-15`, `macos-15-intel`, `ubuntu-24.04-arm`, and `ubuntu-24.04`.
20. Build, test, pack, and release-candidate workflows never publish. Creating
    or changing npm packages, access, trusted publishers, or registry versions
    requires a fresh explicit authorization at Tasks 15 and 16.
21. Use Danus's live worker layout only as a lifecycle-design reference:
    `../Danus/danus/execution/layout.py` centralizes task, role, PID, lock,
    stop, status, log, and local-memory paths in one typed `WorkerLayout`, and
    resolves environment overrides at call time. Preserve the same useful
    properties in Aizim: one path authority, per-invocation environment
    resolution, explicit lifecycle evidence, and artifact-preserving cleanup.
    Do not copy Danus control files into the npm bootstrap cache, create a
    second worker-state authority, or move Aizim worker orchestration into
    Rust. Aizim's StateService, event log, leases, and registered artifacts
    remain authoritative.

## Delivery Milestones

| Milestone | Tasks | Observable result |
| --- | --- | --- |
| A — Package contract | 1–3 | npm selects the correct Aizim and Codex native packages and propagates process behavior |
| B — Native runtime | 4–6 | Rust verifies, locks, provisions, recovers, and execs the Python CLI |
| C — Build and pack | 7–8 | `npm run build` and `npm run pack` produce verified current-host tarballs |
| D — Python and sandbox | 9–11 | npm-local Codex injection and real macOS/Linux fail-closed adapters |
| E — Installation and CI | 12–13 | fresh local/global installs and four native CI jobs pass |
| F — Release readiness | 14 | documented, disabled-by-default release candidate is ready |
| G — Public availability | 15–16 | separately authorized npm publication and OIDC follow-up |

---

### Task 1: Establish the npm and Cargo workspace contracts

**Acceptance criteria:** Specification sections 5, 6, 11.3, and 11.4.

**Files:**

- Create: `package.json`
- Create: `package-lock.json`
- Create: `.node-version`
- Create: `rust-toolchain.toml`
- Create: `Cargo.toml`
- Create: `Cargo.lock`
- Create: `crates/aizim-launcher/Cargo.toml`
- Create: `crates/aizim-launcher/src/lib.rs`
- Create: `crates/aizim-launcher/src/main.rs`
- Create: `npm/platforms/darwin-arm64/package.json`
- Create: `npm/platforms/darwin-arm64/package.publish.json`
- Create: `npm/platforms/darwin-x64/package.json`
- Create: `npm/platforms/darwin-x64/package.publish.json`
- Create: `npm/platforms/linux-arm64/package.json`
- Create: `npm/platforms/linux-arm64/package.publish.json`
- Create: `npm/platforms/linux-x64/package.json`
- Create: `npm/platforms/linux-x64/package.publish.json`
- Create: `tests/npm/package-contract.test.mjs`
- Modify: `.gitignore:1-17`

**Interfaces:**

- Consumes: Python version `0.1.0` from `pyproject.toml`; npm organization
  `@aiz.im`; exact tool versions from the specification.
- Produces: npm workspace metadata, separate public platform-package metadata,
  Cargo workspace metadata, exact target package names, `npm@12.0.1` lock
  state, and Rust crate `aizim_launcher`.

- [ ] **Step 1: Write the failing package-contract test.**

Create `tests/npm/package-contract.test.mjs` with this contract:

```js
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const root = new URL("../../", import.meta.url);
const readJson = (path) =>
  JSON.parse(readFileSync(new URL(path, root), "utf8"));
const pyproject = () =>
  readFileSync(new URL("pyproject.toml", root), "utf8");
const cargo = () => readFileSync(new URL("Cargo.toml", root), "utf8");

const platforms = [
  ["darwin-arm64", ["darwin"], ["arm64"], undefined],
  ["darwin-x64", ["darwin"], ["x64"], undefined],
  ["linux-arm64", ["linux"], ["arm64"], ["glibc"]],
  ["linux-x64", ["linux"], ["x64"], ["glibc"]],
];

test("all distribution versions and exact dependencies agree", () => {
  const meta = readJson("package.json");
  assert.equal(meta.name, "@aiz.im/aizim");
  assert.equal(meta.version, "0.1.0");
  assert.equal(meta.packageManager, "npm@12.0.1");
  assert.equal(meta.engines.node, ">=22.22.2");
  assert.equal(meta.dependencies["@openai/codex"], "0.145.0");
  assert.equal(meta.devDependencies.typescript, "6.0.2");
  assert.deepEqual(meta.workspaces, ["npm/platforms/*"]);
  assert.match(pyproject(), /^version = "0\.1\.0"$/m);
  assert.match(cargo(), /^version = "0\.1\.0"$/m);

  for (const [target, os, cpu, libc] of platforms) {
    const name = `@aiz.im/aizim-${target}`;
    const source = readJson(`npm/platforms/${target}/package.json`);
    const publish = readJson(
      `npm/platforms/${target}/package.publish.json`,
    );
    assert.equal(source.name, name);
    assert.equal(source.version, meta.version);
    assert.equal(meta.optionalDependencies[name], meta.version);
    assert.equal(source.private, true);
    assert.equal("os" in source, false);
    assert.equal("cpu" in source, false);
    assert.equal("libc" in source, false);
    assert.equal(publish.name, name);
    assert.equal(publish.version, meta.version);
    assert.equal(publish.private, undefined);
    assert.deepEqual(publish.os, os);
    assert.deepEqual(publish.cpu, cpu);
    assert.deepEqual(publish.libc, libc);
    assert.equal(publish.publishConfig.access, "public");
  }
});

test("installation has no lifecycle execution hook", () => {
  const forbidden = new Set([
    "preinstall",
    "install",
    "postinstall",
    "prepare",
    "prepublish",
  ]);
  for (const file of [
    "package.json",
    ...platforms.map(([target]) => `npm/platforms/${target}/package.json`),
    ...platforms.map(
      ([target]) => `npm/platforms/${target}/package.publish.json`,
    ),
  ]) {
    const scripts = readJson(file).scripts ?? {};
    assert.equal(
      Object.keys(scripts).some((name) => forbidden.has(name)),
      false,
      file,
    );
  }
});
```

- [ ] **Step 2: Run the test and observe the missing npm manifest.**

Run:

```bash
node --test tests/npm/package-contract.test.mjs
```

Expected: FAIL with `ENOENT` for `package.json`.

- [ ] **Step 3: Add the exact root npm manifest.**

Create `package.json` with this metadata and no lifecycle hook:

```json
{
  "name": "@aiz.im/aizim",
  "version": "0.1.0",
  "description": "Formal-native autonomous mathematical research with Lean 4",
  "license": "MIT",
  "author": "Frankie Wang",
  "type": "module",
  "bin": {
    "aizim": "bin/aizim.js"
  },
  "files": [
    "bin/",
    "lib/",
    "manifest/",
    "vendor/",
    "LICENSE",
    "README.md",
    "THIRD_PARTY_NOTICES.md"
  ],
  "engines": {
    "node": ">=22.22.2"
  },
  "packageManager": "npm@12.0.1",
  "repository": {
    "type": "git",
    "url": "git+https://github.com/AIZ-IM/Aizim.git"
  },
  "publishConfig": {
    "access": "public"
  },
  "workspaces": [
    "npm/platforms/*"
  ],
  "scripts": {
    "build": "node scripts/npm/build.mjs",
    "test": "node scripts/npm/test.mjs",
    "pack": "node scripts/npm/pack.mjs",
    "check": "node scripts/npm/check.mjs",
    "test:node": "node --test tests/npm/*.test.mjs",
    "test:rust": "cargo test --workspace --locked",
    "test:python": "node scripts/npm/test-python.mjs"
  },
  "dependencies": {
    "@openai/codex": "0.145.0"
  },
  "devDependencies": {
    "typescript": "6.0.2"
  },
  "optionalDependencies": {
    "@aiz.im/aizim-darwin-arm64": "0.1.0",
    "@aiz.im/aizim-darwin-x64": "0.1.0",
    "@aiz.im/aizim-linux-arm64": "0.1.0",
    "@aiz.im/aizim-linux-x64": "0.1.0"
  }
}
```

- [ ] **Step 4: Add private workspace and public package manifests.**

Each `package.json` is a private, platform-neutral npm workspace manifest.
It uses the target package name, `version: "0.1.0"`, `private: true`,
`license: "MIT"`, and `main: "index.cjs"`, but deliberately omits `os`,
`cpu`, `libc`, and `publishConfig`. npm otherwise rejects a clean checkout
with `EBADPLATFORM` while processing incompatible workspace edges.

Each adjacent `package.publish.json` is the public package manifest. It uses
`version: "0.1.0"`, `license: "MIT"`, `main: "index.cjs"`,
`publishConfig.access: "public"`, and this exact file allowlist:

```json
[
  "index.cjs",
  "bin/",
  "manifest/",
  "vendor/",
  "LICENSE",
  "README.md",
  "THIRD_PARTY_NOTICES.md"
]
```

Apply these exact target-specific fields:

| Public manifest | `name` | `os` | `cpu` | `libc` |
| --- | --- | --- | --- | --- |
| `npm/platforms/darwin-arm64/package.publish.json` | `@aiz.im/aizim-darwin-arm64` | `["darwin"]` | `["arm64"]` | omit |
| `npm/platforms/darwin-x64/package.publish.json` | `@aiz.im/aizim-darwin-x64` | `["darwin"]` | `["x64"]` | omit |
| `npm/platforms/linux-arm64/package.publish.json` | `@aiz.im/aizim-linux-arm64` | `["linux"]` | `["arm64"]` | `["glibc"]` |
| `npm/platforms/linux-x64/package.publish.json` | `@aiz.im/aizim-linux-x64` | `["linux"]` | `["x64"]` | `["glibc"]` |

The non-target fields in every file are:

```json
{
  "description": "Native Aizim launcher and uv runtime for one supported platform",
  "license": "MIT",
  "author": "Frankie Wang",
  "main": "index.cjs",
  "files": [
    "index.cjs",
    "bin/",
    "manifest/",
    "vendor/",
    "LICENSE",
    "README.md",
    "THIRD_PARTY_NOTICES.md"
  ],
  "publishConfig": {
    "access": "public"
  }
}
```

- [ ] **Step 5: Add the exact Rust workspace and toolchain.**

Create `.node-version` containing `26.5.0` and
`rust-toolchain.toml` containing:

```toml
[toolchain]
channel = "1.97.1"
profile = "minimal"
components = ["clippy", "rust-analyzer", "rustfmt"]
```

Create the root `Cargo.toml`:

```toml
[workspace]
members = ["crates/aizim-launcher"]
resolver = "3"

[workspace.package]
version = "0.1.0"
edition = "2024"
rust-version = "1.97.1"
license = "MIT"
authors = ["Frankie Wang"]

[workspace.dependencies]
fs2 = "=0.4.3"
serde = { version = "=1.0.229", features = ["derive"] }
serde_json = "=1.0.151"
sha2 = "=0.11.0"
tempfile = "=3.27.0"
```

Create `crates/aizim-launcher/Cargo.toml`:

```toml
[package]
name = "aizim-launcher"
version.workspace = true
edition.workspace = true
rust-version.workspace = true
license.workspace = true
authors.workspace = true

[lib]
name = "aizim_launcher"
path = "src/lib.rs"

[[bin]]
name = "aizim-launcher"
path = "src/main.rs"

[dependencies]
fs2.workspace = true
serde.workspace = true
serde_json.workspace = true
sha2.workspace = true

[dev-dependencies]
tempfile.workspace = true
```

The temporary crate surface is:

```rust
// crates/aizim-launcher/src/lib.rs
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
```

```rust
// crates/aizim-launcher/src/main.rs
fn main() {
    if std::env::args_os().nth(1).as_deref() == Some(std::ffi::OsStr::new("--version")) {
        println!("aizim-launcher {}", aizim_launcher::VERSION);
        return;
    }
    eprintln!("aizim-launcher: LAUNCHER_NOT_READY");
    std::process::exit(70);
}
```

- [ ] **Step 6: Ignore only generated npm and Rust output.**

Append these anchored entries to `.gitignore`:

```gitignore
/target/
/build/npm/
/dist/npm/
```

Do not ignore `package-lock.json`, `Cargo.lock`, source package manifests, or
either superpowers document.

- [ ] **Step 7: Generate locks without executing install scripts and pass the contract.**

Run:

```bash
npm install --ignore-scripts
cargo generate-lockfile
node --test tests/npm/package-contract.test.mjs
cargo test --workspace --locked
git diff --check
```

Expected: npm reports no lifecycle execution; both test commands PASS.

- [ ] **Step 8: Commit the workspace contract.**

```bash
git add .gitignore .node-version package.json package-lock.json \
  rust-toolchain.toml Cargo.toml Cargo.lock crates/aizim-launcher \
  npm/platforms tests/npm/package-contract.test.mjs
git diff --cached --check
git commit -m "Define the npm and Rust workspaces"
```

---

### Task 2: Resolve Aizim and Codex native targets in Node

**Acceptance criteria:** Specification sections 5 and 7.1; no global Codex
fallback.

**Files:**

- Create: `lib/errors.mjs`
- Create: `lib/platform.mjs`
- Create: `lib/codex.mjs`
- Create: `tests/npm/platform-resolution.test.mjs`
- Create: `tests/npm/codex-resolution.test.mjs`

**Interfaces:**

- Consumes: exact package names from Task 1; Node
  `process.platform`, `process.arch`, and `process.report`.
- Produces:
  `detectTarget({ platform, arch, report }) -> Target`,
  `resolveCodexExecutable(target, requireFromMeta) -> string`, and
  `DistributionError.code`.

- [ ] **Step 1: Write target-detection failures and all four success cases.**

Use this test table in `tests/npm/platform-resolution.test.mjs`:

```js
import assert from "node:assert/strict";
import test from "node:test";

import { DistributionError } from "../../lib/errors.mjs";
import { detectTarget } from "../../lib/platform.mjs";

const glibc = { header: { glibcVersionRuntime: "2.39" } };

for (const [platform, arch, report, id] of [
  ["darwin", "arm64", undefined, "darwin-arm64"],
  ["darwin", "x64", undefined, "darwin-x64"],
  ["linux", "arm64", glibc, "linux-arm64"],
  ["linux", "x64", glibc, "linux-x64"],
]) {
  test(`maps ${platform} ${arch} to ${id}`, () => {
    assert.equal(detectTarget({ platform, arch, report }).id, id);
  });
}

for (const input of [
  { platform: "win32", arch: "x64" },
  { platform: "darwin", arch: "ia32" },
  { platform: "linux", arch: "x64", report: { header: {} } },
]) {
  test(`rejects ${JSON.stringify(input)}`, () => {
    assert.throws(
      () => detectTarget(input),
      (error) =>
        error instanceof DistributionError &&
        error.code === "UNSUPPORTED_PLATFORM",
    );
  });
}
```

- [ ] **Step 2: Run the target tests and observe missing modules.**

Run:

```bash
node --test tests/npm/platform-resolution.test.mjs
```

Expected: FAIL with `ERR_MODULE_NOT_FOUND`.

- [ ] **Step 3: Implement the closed target table.**

`lib/errors.mjs`:

```js
export class DistributionError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "DistributionError";
    this.code = code;
  }
}
```

`lib/platform.mjs` exports immutable records with these fields:

```js
import { DistributionError } from "./errors.mjs";

const targets = new Map([
  ["darwin:arm64", {
    id: "darwin-arm64",
    packageName: "@aiz.im/aizim-darwin-arm64",
    rustTarget: "aarch64-apple-darwin",
    codexAlias: "@openai/codex-darwin-arm64",
    codexVersion: "0.145.0-darwin-arm64",
    codexTriple: "aarch64-apple-darwin",
  }],
  ["darwin:x64", {
    id: "darwin-x64",
    packageName: "@aiz.im/aizim-darwin-x64",
    rustTarget: "x86_64-apple-darwin",
    codexAlias: "@openai/codex-darwin-x64",
    codexVersion: "0.145.0-darwin-x64",
    codexTriple: "x86_64-apple-darwin",
  }],
  ["linux:arm64", {
    id: "linux-arm64",
    packageName: "@aiz.im/aizim-linux-arm64",
    rustTarget: "aarch64-unknown-linux-gnu",
    codexAlias: "@openai/codex-linux-arm64",
    codexVersion: "0.145.0-linux-arm64",
    codexTriple: "aarch64-unknown-linux-musl",
  }],
  ["linux:x64", {
    id: "linux-x64",
    packageName: "@aiz.im/aizim-linux-x64",
    rustTarget: "x86_64-unknown-linux-gnu",
    codexAlias: "@openai/codex-linux-x64",
    codexVersion: "0.145.0-linux-x64",
    codexTriple: "x86_64-unknown-linux-musl",
  }],
]);

export function detectTarget({
  platform = process.platform,
  arch = process.arch,
  report = platform === "linux" ? process.report.getReport() : undefined,
} = {}) {
  if (platform === "linux" && !report?.header?.glibcVersionRuntime) {
    throw new DistributionError(
      "UNSUPPORTED_PLATFORM",
      "Linux packages require glibc",
    );
  }
  const target = targets.get(`${platform}:${arch}`);
  if (!target) {
    throw new DistributionError(
      "UNSUPPORTED_PLATFORM",
      `unsupported platform ${platform}/${arch}`,
    );
  }
  return Object.freeze({ ...target });
}
```

- [ ] **Step 4: Write Codex alias-layout tests with a temporary npm tree.**

`tests/npm/codex-resolution.test.mjs` creates:

```text
node_modules/@openai/codex/package.json
node_modules/@openai/codex/node_modules/@openai/codex-darwin-arm64/package.json
node_modules/@openai/codex/node_modules/@openai/codex-darwin-arm64/
  vendor/aarch64-apple-darwin/bin/codex
```

Use `createRequire(new URL("package.json", fixtureRoot))` and assert:

```js
const executable = resolveCodexExecutable(
  detectTarget({ platform: "darwin", arch: "arm64" }),
  fixtureRequire,
);
assert.equal(executable, realpathSync(nativeBinary));
```

Add negative cases for meta version `0.145.0`, alias version
`0.145.0-darwin-x64`, a missing alias, a missing executable, and a
non-executable file. Every case must raise `DistributionError` with
`CODEX_PACKAGE_INVALID`; no case may consult `PATH`.

- [ ] **Step 5: Implement exact nested alias resolution.**

`lib/codex.mjs` implements this flow:

```js
import { constants, accessSync, readFileSync, realpathSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";

import { DistributionError } from "./errors.mjs";

const readJson = (path) => JSON.parse(readFileSync(path, "utf8"));

export function resolveCodexExecutable(
  target,
  requireFromMeta = createRequire(import.meta.url),
) {
  try {
    const metaPath = requireFromMeta.resolve("@openai/codex/package.json");
    const meta = readJson(metaPath);
    if (meta.version !== "0.145.0") {
      throw new Error("meta version mismatch");
    }
    const codexRequire = createRequire(metaPath);
    const nativePackagePath = codexRequire.resolve(
      `${target.codexAlias}/package.json`,
    );
    const nativePackage = readJson(nativePackagePath);
    if (nativePackage.version !== target.codexVersion) {
      throw new Error("native version mismatch");
    }
    const executable = realpathSync(
      join(
        dirname(nativePackagePath),
        "vendor",
        target.codexTriple,
        "bin",
        "codex",
      ),
    );
    accessSync(executable, constants.X_OK);
    return executable;
  } catch (error) {
    throw new DistributionError(
      "CODEX_PACKAGE_INVALID",
      "local Codex 0.145.0 native package is unavailable",
      { cause: error },
    );
  }
}
```

Update `DistributionError` to accept an optional `options` argument and pass it
to `super(message, options)`.

- [ ] **Step 6: Run the complete Node resolver suite.**

Run:

```bash
node --test tests/npm/platform-resolution.test.mjs \
  tests/npm/codex-resolution.test.mjs
git diff --check
```

Expected: all tests PASS.

- [ ] **Step 7: Commit the resolver.**

```bash
git add lib tests/npm/platform-resolution.test.mjs \
  tests/npm/codex-resolution.test.mjs
git diff --cached --check
git commit -m "Resolve native npm distribution targets"
```

---

### Task 3: Dispatch the npm CLI with exact process semantics

**Acceptance criteria:** Specification sections 4, 7.1, and 10.

**Files:**

- Create: `bin/aizim.js`
- Create: `lib/assets.mjs`
- Create: `lib/launch.mjs`
- Create: `npm/platforms/darwin-arm64/index.cjs`
- Create: `npm/platforms/darwin-x64/index.cjs`
- Create: `npm/platforms/linux-arm64/index.cjs`
- Create: `npm/platforms/linux-x64/index.cjs`
- Create: `tests/npm/launch.test.mjs`
- Create: `tests/npm/assets.test.mjs`

**Interfaces:**

- Consumes: `Target` and Codex executable from Task 2.
- Produces:
  `resolveAssets(target, requireFromMeta, metaRoot) -> DistributionAssets`,
  `launchAizim(assets, argv, processLike, spawnImpl) -> Promise<number>`, and
  the public npm `aizim` bin.

- [ ] **Step 1: Write asset-resolution tests.**

The fixture platform module exports:

```js
module.exports = Object.freeze({
  launcher: "/fixture/platform/bin/aizim-launcher",
  platformManifest: "/fixture/platform/manifest/platform.json",
});
```

Tests assert that `resolveAssets` returns absolute paths for the launcher,
platform manifest, meta `manifest/distribution.json`,
meta wheel/requirements root, and local native Codex. Missing platform
packages must raise `PLATFORM_PACKAGE_MISSING`; missing files must raise
`DISTRIBUTION_INCOMPLETE`.

- [ ] **Step 2: Run the asset test and observe missing modules.**

Run:

```bash
node --test tests/npm/assets.test.mjs
```

Expected: FAIL with `ERR_MODULE_NOT_FOUND`.

- [ ] **Step 3: Add the same closed export to every platform package.**

Each of the four `index.cjs` files contains exactly:

```js
"use strict";

const path = require("node:path");

module.exports = Object.freeze({
  launcher: path.join(__dirname, "bin", "aizim-launcher"),
  platformManifest: path.join(__dirname, "manifest", "platform.json"),
});
```

`lib/assets.mjs` uses `requireFromMeta(target.packageName)`, resolves the
two meta paths relative to `metaRoot`, calls `resolveCodexExecutable`, verifies
all paths are absolute regular files, and returns a frozen record. It does not
search `PATH`. `PLATFORM_PACKAGE_MISSING` uses this fixed safe remediation:

```text
compatible Aizim native package is missing; use macOS or glibc Linux on arm64/x64 and reinstall without --omit=optional
```

Malformed or missing files inside a resolved package use
`DISTRIBUTION_INCOMPLETE` and recommend a clean reinstall.

- [ ] **Step 4: Write launch tests with an injected child-process stub.**

Cover these exact outcomes in `tests/npm/launch.test.mjs`:

- launcher receives internal flags followed by one `--` and the unchanged
  hostile-looking user arguments;
- `stdio` is `"inherit"`;
- child exit 0 yields 0;
- child exit 69 yields 69;
- missing exit code yields 70;
- `SIGINT`, `SIGTERM`, and `SIGHUP` are forwarded once;
- child signal termination removes forwarding handlers before re-signalling
  the wrapper;
- an asset-resolution error prints one safe `aizim: CODE: message` line and
  exits 78.

Use a fake `EventEmitter` child and an injected `spawnImpl` rather than a live
subprocess for the signal unit tests.

- [ ] **Step 5: Implement the launcher process contract.**

`lib/launch.mjs` constructs:

```js
const internalArgs = [
  "--distribution-manifest",
  assets.distributionManifest,
  "--platform-manifest",
  assets.platformManifest,
  "--codex-executable",
  assets.codexExecutable,
  "--",
  ...argv,
];
```

It calls:

```js
const child = spawnImpl(assets.launcher, internalArgs, {
  stdio: "inherit",
  env: processLike.env,
});
```

Register only `SIGINT`, `SIGTERM`, and `SIGHUP`. On child exit, remove all
handlers. If a signal caused exit, call `processLike.kill(processLike.pid,
signal)` after removing handlers; otherwise resolve the numeric code or 70.
Reject spawn errors as `LAUNCHER_UNAVAILABLE`.

`bin/aizim.js` is executable, starts with `#!/usr/bin/env node`, and imports
only `../lib/platform.mjs`, `../lib/assets.mjs`, `../lib/launch.mjs`, and
`../lib/errors.mjs`. It detects the target, resolves assets relative to its
package root, awaits `launchAizim`, and sets `process.exitCode`. It catches
only `DistributionError`, writes a single safe line to stderr, and maps
unsupported/missing package errors to 78 and internal wrapper errors to 70.

- [ ] **Step 6: Run all Node tests and verify the executable bit.**

Run:

```bash
chmod 0755 bin/aizim.js
npm run test:node
test -x bin/aizim.js
git diff --check
```

Expected: all Node tests PASS and the executable check exits 0.

- [ ] **Step 7: Commit the dispatcher.**

```bash
git add bin/aizim.js lib/assets.mjs lib/launch.mjs \
  npm/platforms/*/index.cjs tests/npm
git diff --cached --check
git commit -m "Dispatch the npm Aizim executable"
```

---

### Task 4: Parse and verify distribution manifests in Rust

**Acceptance criteria:** Specification sections 8 and 10 launcher-only error
categories.

**Files:**

- Create: `crates/aizim-launcher/src/args.rs`
- Create: `crates/aizim-launcher/src/error.rs`
- Create: `crates/aizim-launcher/src/integrity.rs`
- Create: `crates/aizim-launcher/src/manifest.rs`
- Create: `crates/aizim-launcher/tests/manifest_contract.rs`
- Modify: `crates/aizim-launcher/src/lib.rs`
- Modify: `crates/aizim-launcher/src/main.rs`

**Interfaces:**

- Consumes: Node internal arguments from Task 3 and generated JSON manifests
  from Task 7.
- Produces:
  `LauncherArgs::parse`,
  `DistributionManifest`,
  `PlatformManifest`,
  `VerifiedDistribution`, and
  `LauncherError::exit_code()`.

- [ ] **Step 1: Write strict argument and manifest tests.**

The tests must prove:

- all three internal flags are required exactly once before `--`;
- every `OsString` after `--` is retained byte-for-byte;
- unknown flags and a missing separator are exit 78 configuration failures;
- unknown JSON fields are rejected;
- schema/version/target, Node platform/architecture, Rust target, and libc
  mismatches are exit 78;
- absolute artifact paths and `..` components are rejected;
- size/hash mismatch, symlink escape, and non-regular artifacts are exit 74;
- valid manifests return canonical wheel, requirements, uv, and Codex paths.
- safe version mismatch diagnostics contain the layer, expected/observed
  versions, and remediation without a path or nested source string.

Build fixture artifacts from the four ASCII bytes `test`, whose SHA-256 is
`9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08`,
and use these manifest shapes:

```json
{
  "schema_version": 1,
  "aizim_version": "0.1.0",
  "python_version": "3.14.6",
  "wheel": {
    "path": "vendor/aizim-0.1.0-py3-none-any.whl",
    "size": 4,
    "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
  },
  "runtime_requirements": {
    "path": "vendor/runtime-requirements.txt",
    "size": 4,
    "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
  },
  "codex_version": "0.145.0",
  "minimum_node_version": "22.22.2",
  "platform_schema_version": 1
}
```

```json
{
  "schema_version": 1,
  "aizim_version": "0.1.0",
  "package_name": "@aiz.im/aizim-darwin-arm64",
  "target": "darwin-arm64",
  "node_platform": "darwin",
  "node_arch": "arm64",
  "rust_target": "aarch64-apple-darwin",
  "libc": null,
  "launcher_version": "0.1.0",
  "uv_version": "0.11.31",
  "uv": {
    "path": "vendor/uv",
    "size": 4,
    "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
  },
  "distribution_schema_version": 1
}
```

- [ ] **Step 2: Run the Rust test and observe missing manifest APIs.**

Run:

```bash
cargo test --locked --test manifest_contract
```

Expected: FAIL because `aizim_launcher::manifest` and `args` do not exist.

- [ ] **Step 3: Define strict errors and argument parsing.**

`error.rs` defines:

```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    Unavailable,
    Internal,
    Integrity,
    Configuration,
}

impl ErrorKind {
    pub const fn exit_code(self) -> i32 {
        match self {
            Self::Unavailable => 69,
            Self::Internal => 70,
            Self::Integrity => 74,
            Self::Configuration => 78,
        }
    }
}

#[derive(Debug)]
pub struct LauncherError {
    pub kind: ErrorKind,
    pub code: &'static str,
    message: &'static str,
    remediation: &'static str,
    safe_context: Vec<(&'static str, String)>,
    pub source: Option<Box<dyn std::error::Error + Send + Sync>>,
}
```

Its constructors accept context keys only from `expected`, `observed`, and
`cache_key`; values must match ASCII `[A-Za-z0-9._-]`, must be at most 80
bytes, and are omitted otherwise. `Display` emits:

```text
CODE: <static message>; expected=<safe>; observed=<safe>; cache_key=<safe>; remediation=<static remediation>
```

Only present safe fields are rendered. It never includes paths, environment
values, URLs, or nested error text. Add constructors
`unavailable(code)`, `internal(code)`, `integrity(code)`, and
`configuration(code)`, each with a static layer-specific message and concrete
remediation. Version mismatch tests require expected and safely parsed
observed values; provisioning errors add the deterministic cache key.

`args.rs` defines:

```rust
pub struct LauncherArgs {
    pub distribution_manifest: PathBuf,
    pub platform_manifest: PathBuf,
    pub codex_executable: PathBuf,
    pub user_args: Vec<OsString>,
}
```

Parse the closed flag set without `clap`; reject duplicate, unknown, empty, or
relative manifest/Codex paths.

- [ ] **Step 4: Implement strict manifest types and artifact verification.**

Use `#[serde(deny_unknown_fields)]` on:

```rust
pub struct Artifact {
    pub path: PathBuf,
    pub size: u64,
    pub sha256: String,
}

pub struct DistributionManifest {
    pub schema_version: u32,
    pub aizim_version: String,
    pub python_version: String,
    pub wheel: Artifact,
    pub runtime_requirements: Artifact,
    pub codex_version: String,
    pub minimum_node_version: String,
    pub platform_schema_version: u32,
}

pub struct PlatformManifest {
    pub schema_version: u32,
    pub aizim_version: String,
    pub package_name: String,
    pub target: String,
    pub node_platform: String,
    pub node_arch: String,
    pub rust_target: String,
    pub libc: Option<String>,
    pub launcher_version: String,
    pub uv_version: String,
    pub uv: Artifact,
    pub distribution_schema_version: u32,
}
```

`integrity.rs` reads files in bounded chunks, computes SHA-256 with
`sha2::Sha256`, verifies lowercase 64-character digests, rejects path traversal
before canonicalization, and requires a regular file with the exact size.
Each manifest path must be exactly `<package-root>/manifest/<name>.json`;
artifact paths resolve relative to that `<package-root>` and must remain below
it after canonicalization. Distribution artifacts may not resolve below the
platform root, and platform artifacts may not resolve below the meta root.

`VerifiedDistribution::load(&LauncherArgs)` validates:

- all schema versions are 1;
- all Aizim/launcher versions equal `env!("CARGO_PKG_VERSION")`;
- Python is `3.14.6`, minimum Node is `22.22.2`, uv is `0.11.31`, and Codex is
  `0.145.0`;
- package name, target, Node platform/architecture, Rust triple, and libc are
  one of the four closed combinations;
- the supplied Codex executable is absolute, executable, and a regular file;
- wheel, requirements, and uv artifacts pass size/hash verification.

- [ ] **Step 5: Wire the binary to verify before reporting not-yet-provisioned.**

`main.rs` calls a `run()` function, prints only
`aizim-launcher: <safe diagnostic>` on failure, and exits through
`LauncherError::exit_code()`. At this task boundary, valid manifests may return
`RUNTIME_NOT_PROVISIONED` with exit 69; invalid artifacts must fail first with
their stable category.

- [ ] **Step 6: Run Rust checks.**

Run:

```bash
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --locked
git diff --check
```

Expected: all checks PASS.

- [ ] **Step 7: Commit the verified manifest boundary.**

```bash
git add crates/aizim-launcher
git diff --cached --check
git commit -m "Verify npm distribution manifests in Rust"
```

---

### Task 5: Add versioned cache locking and atomic recovery

**Acceptance criteria:** Specification sections 9.1 and 9.3.

**Files:**

- Create: `crates/aizim-launcher/src/cache.rs`
- Create: `crates/aizim-launcher/tests/cache_contract.rs`
- Modify: `crates/aizim-launcher/src/lib.rs`

**Interfaces:**

- Consumes: `VerifiedDistribution` from Task 4.
- Produces:
  `CacheEnvironment`,
  `CacheKey`,
  `CacheLayout`,
  `RuntimeLock`,
  `ReadyMarker`, and
  atomic staging/promotion operations used by Task 6.

- [ ] **Step 1: Write cache root and key tests.**

Cover these exact roots:

| Inputs | Expected root |
| --- | --- |
| Darwin, `HOME=/Users/tester` | `/Users/tester/Library/Caches/aizim` |
| Linux, `XDG_CACHE_HOME=/cache` | `/cache/aizim` |
| Linux, no XDG, `HOME=/home/tester` | `/home/tester/.cache/aizim` |
| `AIZIM_CACHE_DIR=/custom/cache` | `/custom/cache` |

Reject a relative override, a missing required home, a root that resolves
through a symlink, and a non-directory root. Assert that changing version,
target, wheel digest, Python version, or cache schema changes the cache key,
while identical inputs produce the same path.

- [ ] **Step 2: Write concurrency and interrupted-staging tests.**

Use `tempfile::TempDir`, two threads, and a barrier. Assert:

- only one thread holds an exclusive lock at a time;
- a persistent lock file without a held OS lock does not block;
- a valid ready environment is reused without mutation;
- a missing/invalid ready marker is not reused;
- promotion uses a same-parent rename;
- a complete final environment is never overwritten;
- cleanup removes only names matching the current key's
  `.staging-<pid>-<counter>` prefix;
- unrelated and older complete version directories remain untouched.

- [ ] **Step 3: Run the tests and observe the missing cache module.**

Run:

```bash
cargo test --locked --test cache_contract
```

Expected: FAIL because `aizim_launcher::cache` does not exist.

- [ ] **Step 4: Implement deterministic cache layout.**

`cache.rs` defines:

```rust
pub const CACHE_SCHEMA_VERSION: u32 = 1;

pub struct CacheEnvironment {
    pub platform: &'static str,
    pub home: Option<PathBuf>,
    pub xdg_cache_home: Option<PathBuf>,
    pub override_root: Option<PathBuf>,
}

pub struct CacheKey {
    pub aizim_version: String,
    pub target: String,
    pub wheel_sha256: String,
    pub python_version: String,
}

pub struct CacheLayout {
    pub root: PathBuf,
    pub managed_python_root: PathBuf,
    pub uv_cache_root: PathBuf,
    pub runtime_root: PathBuf,
    pub lock_path: PathBuf,
}
```

The runtime path is:

```text
<root>/runtime/v1/<aizim-version>/<target>/<wheel-sha256>/py3.14.6
```

The lock file is a sibling of the final `py3.14.6` directory. Normalize absolute
paths lexically, create missing roots with mode 0700, use `symlink_metadata`
before and after creation, and reject symlink components.

- [ ] **Step 5: Implement OS-backed locking and ready markers.**

`RuntimeLock::acquire` opens the lock file with create/read/write and calls
`fs2::FileExt::lock_exclusive`. Its `Drop` calls `unlock`; no PID text is used
to decide whether a lock is held.

Define a strict marker:

```rust
#[derive(serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReadyMarker {
    pub schema_version: u32,
    pub aizim_version: String,
    pub target: String,
    pub wheel_sha256: String,
    pub python_version: String,
    pub aizim_entrypoint: String,
    pub sidecar_entrypoint: String,
}
```

Entrypoint values are relative to the runtime root and must contain only normal
path components. Write the marker to `READY.json.tmp`, `sync_all`, rename to
`READY.json`, and sync the directory. A ready environment requires an exact
marker plus executable `aizim` and `aizim-gateway-sidecar` files.

- [ ] **Step 6: Implement scoped staging and promotion.**

Create staging directories under the runtime parent with:

```text
.<cache-leaf>.staging-<pid>-<monotonic-counter>
```

Use `create_dir`, not a predictable preexisting directory. Promotion fails if
the final path exists and is not already valid. Cleanup accepts both the
runtime parent and the exact prefix and refuses any path outside that parent.

- [ ] **Step 7: Run Rust quality checks.**

Run:

```bash
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --locked
git diff --check
```

Expected: all checks PASS, including the two-thread lock test.

- [ ] **Step 8: Commit cache lifecycle ownership.**

```bash
git add crates/aizim-launcher/src/cache.rs \
  crates/aizim-launcher/src/lib.rs \
  crates/aizim-launcher/tests/cache_contract.rs
git diff --cached --check
git commit -m "Add atomic Aizim runtime caching"
```

---

### Task 6: Provision Python with bundled uv and exec Aizim

**Acceptance criteria:** Specification sections 7.2, 9.2, 9.3, and 10.

**Files:**

- Create: `crates/aizim-launcher/src/process.rs`
- Create: `crates/aizim-launcher/src/provision.rs`
- Create: `crates/aizim-launcher/tests/provision_contract.rs`
- Modify: `crates/aizim-launcher/src/lib.rs`
- Modify: `crates/aizim-launcher/src/main.rs`

**Interfaces:**

- Consumes: `VerifiedDistribution`, `CacheLayout`, and `LauncherArgs`.
- Produces:
  `CommandSpec`,
  `CommandRunner`,
  `ProvisionRequest`,
  `ensure_runtime(...) -> ReadyRuntime`, and
  final Unix `exec`.

- [ ] **Step 1: Write command-sequence and environment tests.**

Use an injected fake `CommandRunner` and assert a cold bootstrap issues these
programs in order:

```text
<bundled-uv> --no-config python install --install-dir <python-root> 3.14.6
<bundled-uv> --no-config venv --managed-python --python 3.14.6 <staging>/venv
<bundled-uv> --no-config pip install --python <venv-python> \
  --require-hashes --no-deps --default-index https://pypi.org/simple \
  -r <runtime-requirements>
<bundled-uv> --no-config pip install --python <venv-python> \
  --no-deps <wheel>
<staging-aizim> --version
```

Assert that every uv command has:

```text
UV_CACHE_DIR=<cache>/uv
UV_PYTHON_INSTALL_DIR=<cache>/python
UV_NO_PROGRESS=1
```

and that it removes `UV_*`, `PIP_*`, `PYTHONPATH`, `PYTHONHOME`, and
`VIRTUAL_ENV` inherited values before setting the three exact uv variables.
Only `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, `SSL_CERT_FILE`, `SSL_CERT_DIR`,
`LANG`, `LC_ALL`, and the platform temporary-directory variable may pass
through from the bootstrap allowlist.

- [ ] **Step 2: Write success, reuse, and failure-recovery tests.**

Assert:

- successful commands create the ready marker last and promote atomically;
- a valid ready runtime runs no provisioning command;
- failure at each command leaves no final ready directory;
- interruption before promotion is recoverable;
- wrong `aizim --version` output is exit 69 and not promoted;
- user arguments including non-UTF-8 bytes are preserved for final exec;
- final exec environment contains the six exact npm distribution variables;
- final exec removes Python injection and bootstrap-only variables;
- a cold bootstrap prints only concise layer, Python-version, and cache-key
  status lines to stderr, while a warm launch prints no bootstrap line;
- no diagnostic includes proxy credentials or source error text.

- [ ] **Step 3: Run the tests and observe missing provisioning APIs.**

Run:

```bash
cargo test --locked --test provision_contract
```

Expected: FAIL because `aizim_launcher::provision` does not exist.

- [ ] **Step 4: Define process specifications and the real runner.**

`process.rs` defines:

```rust
pub struct CommandSpec {
    pub program: PathBuf,
    pub args: Vec<OsString>,
    pub env_clear: bool,
    pub env: Vec<(OsString, OsString)>,
    pub cwd: PathBuf,
}

pub trait CommandRunner {
    fn run(&mut self, spec: &CommandSpec) -> Result<ExitStatus, LauncherError>;
}

pub struct RealCommandRunner;
```

The real runner uses `std::process::Command`, inherited stdin/stdout/stderr,
`env_clear`, the explicit allowlist, and no shell. A nonzero uv or version
command maps to `RUNTIME_PROVISION_FAILED` exit 69 without embedding raw stderr
in the Rust error.

- [ ] **Step 5: Implement the cold bootstrap.**

`ProvisionRequest` contains verified paths, cache layout, target, manifest
digests, Codex executable, and expected version. `ensure_runtime`:

1. acquires the key lock;
2. returns immediately for an exact valid marker;
3. removes only current-key stale staging directories;
4. creates one private staging directory;
5. runs the five exact command groups from Step 1;
6. checks both console entry points are executable;
7. writes the exact ready marker;
8. promotes the staging directory;
9. reopens and revalidates the promoted marker;
10. returns canonical entrypoint paths.

On a cold path, write these stable status forms to inherited stderr:

```text
aizim: provisioning managed Python 3.14.6 runtime (<cache-key>)
aizim: runtime ready (<cache-key>)
```

Do not print the cache root, home, package-index URL, command environment, or
download credentials. A warm reuse emits neither line.

Use the platform venv paths:

```text
<runtime>/venv/bin/python
<runtime>/venv/bin/aizim
<runtime>/venv/bin/aizim-gateway-sidecar
```

Windows layout is not implemented.

- [ ] **Step 6: Set the closed npm distribution context and exec.**

Before exec, set only:

```text
AIZIM_DISTRIBUTION_MODE=npm
AIZIM_DISTRIBUTION_VERSION=0.1.0
AIZIM_DISTRIBUTION_TARGET=<target>
AIZIM_CODEX_EXECUTABLE=<canonical native Codex path>
AIZIM_DISTRIBUTION_MANIFEST_SHA256=<digest>
AIZIM_PLATFORM_MANIFEST_SHA256=<digest>
```

`main.rs` loads args/manifests, builds the cache environment from the process
environment, ensures the runtime, then uses
`std::os::unix::process::CommandExt::exec` on the `aizim` entrypoint with
unchanged `user_args`. If `exec` returns, map the I/O error to
`AIZIM_EXEC_FAILED` exit 74.

- [ ] **Step 7: Run all Rust tests and a synthetic launcher invocation.**

Run:

```bash
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --locked
cargo run --locked -p aizim-launcher -- --version
git diff --check
```

Expected: checks PASS and the last binary command prints
`aizim-launcher 0.1.0`.

- [ ] **Step 8: Commit the native provisioning path.**

```bash
git add crates/aizim-launcher
git diff --cached --check
git commit -m "Provision the npm Python runtime"
```

---

### Task 7: Build wheel, requirements, Rust, and uv from npm

**Acceptance criteria:** Specification sections 8.1, 8.2, 11.1, and 11.2.

**Files:**

- Create: `scripts/npm/uv-artifacts.json`
- Create: `scripts/npm/lib/command.mjs`
- Create: `scripts/npm/lib/hash.mjs`
- Create: `scripts/npm/lib/paths.mjs`
- Create: `scripts/npm/fetch-uv.mjs`
- Create: `scripts/npm/build.mjs`
- Create: `tests/npm/build-tools.test.mjs`

**Interfaces:**

- Consumes: package/Cargo workspace from Task 1, launcher from Task 6,
  `pyproject.toml`, and `uv.lock`.
- Produces:
  `build/npm/meta/`,
  `build/npm/<target>/`,
  `build/npm/build-summary.json`, and exact generated manifests.

- [ ] **Step 1: Record the official uv archive table.**

Create `scripts/npm/uv-artifacts.json` with:

```json
{
  "version": "0.11.31",
  "targets": {
    "darwin-arm64": {
      "archive": "uv-aarch64-apple-darwin.tar.gz",
      "sha256": "b2b93e82a6786f9c7cb89fd4ca0e859a147b292ae8f6f95784f9742f0efec39e",
      "directory": "uv-aarch64-apple-darwin"
    },
    "darwin-x64": {
      "archive": "uv-x86_64-apple-darwin.tar.gz",
      "sha256": "33ee6bd62b57fcd77a499deb54e4432dc1e1a2f3d34930ba987ad8b43f9c7bc7",
      "directory": "uv-x86_64-apple-darwin"
    },
    "linux-arm64": {
      "archive": "uv-aarch64-unknown-linux-gnu.tar.gz",
      "sha256": "d74f23949fd07be4970f293d06ca99d87cd2a78a341c3d7b7fc0df7bc2d8a145",
      "directory": "uv-aarch64-unknown-linux-gnu"
    },
    "linux-x64": {
      "archive": "uv-x86_64-unknown-linux-gnu.tar.gz",
      "sha256": "8cc1cd82d434ec565376f98bd938d4b715b5791a80ff2d3aa78821cf85091b4b",
      "directory": "uv-x86_64-unknown-linux-gnu"
    }
  }
}
```

- [ ] **Step 2: Write build-tool unit tests.**

Tests use temporary files and an injected `fetch` implementation to prove:

- SHA-256 comparison is lowercase and constant-time;
- a mismatched archive is deleted and never extracted;
- extraction uses `tar -xzf <archive> -C <private-directory>` without a shell;
- the extracted `uv` must be a regular executable;
- target detection agrees with Task 2;
- a version mismatch among Python, npm, Cargo, and platform manifests fails;
- generated manifest sizes and hashes match fixture artifacts;
- the source-input digest changes for every declared input and ignores only
  generated build output.

- [ ] **Step 3: Run the test and observe missing build helpers.**

Run:

```bash
node --test tests/npm/build-tools.test.mjs
```

Expected: FAIL with `ERR_MODULE_NOT_FOUND`.

- [ ] **Step 4: Implement safe subprocess and hashing helpers.**

`scripts/npm/lib/command.mjs` exposes:

```js
import { spawn } from "node:child_process";
import { basename } from "node:path";

export async function run(program, args, {
  cwd,
  env = process.env,
  stdio = "inherit",
} = {}) {
  return await new Promise((resolve, reject) => {
    const child = spawn(program, args, {
      cwd,
      env,
      stdio,
      shell: false,
    });
    child.once("error", () => {
      reject(new Error(`${basename(program)} failed to start`));
    });
    child.once("exit", (code, signal) => {
      if (code === 0) {
        resolve();
        return;
      }
      const outcome = code === null ? `signal ${signal}` : `status ${code}`;
      reject(new Error(`${basename(program)} exited with ${outcome}`));
    });
  });
}
```

The implementation uses `spawn(program, args, { shell: false, ... })`, reports
only the program basename and numeric status, and never serializes `env`.

`hash.mjs` streams SHA-256, returns lowercase hex, verifies expected digests
with `timingSafeEqual`, and hashes a sorted explicit input path list with path
names and file bytes.

`paths.mjs` exposes canonical repository, build, dist, meta, target, and tool
paths and rejects any generated path outside `build/npm` or `dist/npm`.

- [ ] **Step 5: Download and verify uv before extraction.**

`fetch-uv.mjs` builds only this URL:

```text
https://github.com/astral-sh/uv/releases/download/0.11.31/<archive>
```

It downloads to a temporary sibling, syncs and renames the completed archive,
verifies the committed SHA before `tar`, extracts into a new private
directory, copies only `<directory>/uv` to the target tool path, sets mode
0755, and records the binary SHA-256. Existing cached archives and binaries are
reverified on every build.

- [ ] **Step 6: Implement the exact build orchestration.**

`build.mjs` performs:

```text
verify Node 26.5.0 and npm 12.0.1
detect current target and require matching rustc host
verify all source versions are 0.1.0
fetch and verify uv 0.11.31
<uv> --no-config build --wheel --out-dir build/npm/wheel
<uv> --no-config export --locked --no-dev --no-emit-project \
  --format requirements.txt \
  --output-file build/npm/runtime-requirements.txt
cargo build --release --locked -p aizim-launcher
assemble build/npm/meta and build/npm/<target>
generate both strict manifests with measured sizes and hashes
write build-summary.json last
```

The summary contains schema 1, exact full Git SHA, dirty tracked-input boolean,
target, source-input digest, artifact paths, and artifact SHA-256 values. It
must not contain the environment, home path, credentials, or authenticated
URLs.

The meta staging tree contains the wheel and requirements. The platform tree
contains the launcher and extracted uv binary. Both manifests use the exact
schema from Task 4.

- [ ] **Step 7: Run the real source build surface.**

Run:

```bash
npm run build
test -x build/npm/darwin-arm64/bin/aizim-launcher
test -x build/npm/darwin-arm64/vendor/uv
build/npm/darwin-arm64/bin/aizim-launcher --version
node -e 'const x=require("./build/npm/build-summary.json"); if(x.target!=="darwin-arm64") process.exit(1)'
```

On the current Apple Silicon host, expected launcher output is
`aizim-launcher 0.1.0` and every command exits 0.

- [ ] **Step 8: Run aggregate build checks.**

Run:

```bash
npm run test:node
cargo test --workspace --locked
git diff --check
```

Expected: all tests PASS.

- [ ] **Step 9: Commit the npm build pipeline.**

```bash
git add scripts/npm tests/npm/build-tools.test.mjs
git diff --cached --check
git commit -m "Build Aizim native packages with npm"
```

---

### Task 8: Assemble licensed, allowlisted npm tarballs

**Acceptance criteria:** Specification sections 11.2, 11.4, 12.4, and 15
package-content requirements.

**Files:**

- Create: `npm/README.md`
- Create: `third_party/uv/LICENSE-MIT`
- Create: `third_party/uv/LICENSE-APACHE`
- Create: `third_party/uv/SHA256SUMS`
- Create: `scripts/npm/write-notices.mjs`
- Create: `scripts/npm/assemble.mjs`
- Create: `scripts/npm/pack.mjs`
- Create: `scripts/npm/check.mjs`
- Create: `tests/npm/package-contents.test.mjs`
- Modify: `scripts/npm/build.mjs`
- Create: generated-and-committed `THIRD_PARTY_NOTICES.md`

**Interfaces:**

- Consumes: staging artifacts and build summary from Task 7.
- Produces: two current-host tarballs under `dist/npm`, exact package
  allowlists, deterministic notices, and stale-build refusal.

- [ ] **Step 1: Add pinned upstream uv license texts.**

Run:

```bash
mkdir -p third_party/uv
curl -fsSL https://raw.githubusercontent.com/astral-sh/uv/0.11.31/LICENSE-MIT \
  -o third_party/uv/LICENSE-MIT
curl -fsSL https://raw.githubusercontent.com/astral-sh/uv/0.11.31/LICENSE-APACHE \
  -o third_party/uv/LICENSE-APACHE
grep -q "MIT License" third_party/uv/LICENSE-MIT
grep -q "Apache License" third_party/uv/LICENSE-APACHE
(
  cd third_party/uv
  shasum -a 256 LICENSE-APACHE LICENSE-MIT > SHA256SUMS
  shasum -a 256 -c SHA256SUMS
)
```

`SHA256SUMS` is committed beside the two files. `check.mjs` parses exactly two
lowercase SHA-256 records from it and rejects missing, additional, renamed, or
changed license content. The archive digest table in
`scripts/npm/uv-artifacts.json` remains limited to release binaries.

- [ ] **Step 2: Write package-content tests before assembly.**

`tests/npm/package-contents.test.mjs` invokes `npm pack --dry-run --json` on
fixture staging directories and compares exact sorted path sets.

The meta package set is:

```text
LICENSE
README.md
THIRD_PARTY_NOTICES.md
bin/aizim.js
lib/assets.mjs
lib/codex.mjs
lib/errors.mjs
lib/launch.mjs
lib/platform.mjs
manifest/distribution.json
package.json
vendor/aizim-0.1.0-py3-none-any.whl
vendor/runtime-requirements.txt
```

The platform package set is:

```text
LICENSE
README.md
THIRD_PARTY_NOTICES.md
bin/aizim-launcher
index.cjs
manifest/platform.json
package.json
vendor/licenses/uv/LICENSE-APACHE
vendor/licenses/uv/LICENSE-MIT
vendor/uv
```

Also assert executable modes for the JavaScript bin, Rust launcher, and uv;
package versions/names; public access; exact optional dependencies; no
lifecycle scripts; and absence of `.git`, `.aizim`, `.omc`, `.env`, tests,
source maps, Python caches, and credential-like filenames.

- [ ] **Step 3: Run the content test and observe missing assembly scripts.**

Run:

```bash
node --test tests/npm/package-contents.test.mjs
```

Expected: FAIL because the fixture assembly helper is unavailable.

- [ ] **Step 4: Generate complete deterministic third-party notices.**

`write-notices.mjs` runs:

```text
cargo metadata --format-version 1 --locked
```

It sorts every package included in the launcher dependency graph by
name/version, requires a nonempty SPDX license expression from the allowlist
`MIT`, `Apache-2.0`, `MIT OR Apache-2.0`, `Apache-2.0 OR MIT`, `ISC`, and
`Unicode-3.0`, then writes:

```text
Aizim Third-Party Notices
uv 0.11.31 — MIT OR Apache-2.0
<one exact name version license line per locked Rust package>
```

The generated root `THIRD_PARTY_NOTICES.md` is committed. `npm run check`
regenerates into memory and fails if committed content differs.

- [ ] **Step 5: Assemble staging trees from explicit copies.**

`assemble.mjs` deletes only the exact generated target staging directories,
recreates them mode 0700, and copies:

- root `package.json`, `bin/aizim.js`, the five runtime `lib/*.mjs` files,
  `LICENSE`, `npm/README.md`, notices, wheel, requirements, and generated
  distribution manifest into meta;
- current platform `package.publish.json` renamed to staged `package.json`,
  `index.cjs`, `LICENSE`, `npm/README.md`, notices, launcher, uv, uv licenses,
  and platform manifest into platform. The private source-workspace
  `package.json` is never copied into a publish staging tree.

It sets file modes explicitly and rejects symlinks, sockets, devices, FIFOs,
hard-linked duplicates, and any source or destination outside the declared
roots.

- [ ] **Step 6: Pack only fresh verified output.**

`pack.mjs` recomputes the source-input digest from Task 7, requires it to match
the build summary, re-verifies every manifest hash, runs:

```text
npm pack --json --pack-destination dist/npm build/npm/<target>
npm pack --json --pack-destination dist/npm build/npm/meta
```

and then opens both tarball listings through `npm pack --dry-run --json` or
`tar -tzf` to compare the exact allowlists. It writes no registry state.
After both tarballs pass, it writes `dist/npm/pack-summary.json` last with the
full Git SHA, target, and each package's exact name, version, filename, byte
size, SHA-256, and npm-reported integrity value.

`check.mjs` uses the current build's verified bundled uv to run
`ruff check .` and `ty check`, runs `cargo fmt --all --check`, and then runs
version, notice, manifest, file-mode, and allowlist checks without rebuilding.
It preserves the first nonzero status and does not invoke a package manager or
registry write.

- [ ] **Step 7: Build, check, and pack the current host.**

Run:

```bash
npm run build
npm run check
npm run pack
find dist/npm -maxdepth 1 -name '*.tgz' -print | sort
```

Expected: exactly:

```text
dist/npm/aiz.im-aizim-0.1.0.tgz
dist/npm/aiz.im-aizim-darwin-arm64-0.1.0.tgz
```

On another host, replace only the platform suffix.

- [ ] **Step 8: Run aggregate checks.**

Run:

```bash
npm run test:node
cargo test --workspace --locked
git diff --check
```

Expected: all tests PASS.

- [ ] **Step 9: Commit licensed package assembly.**

```bash
git add npm/README.md third_party/uv THIRD_PARTY_NOTICES.md \
  scripts/npm tests/npm/package-contents.test.mjs
git diff --cached --check
git commit -m "Assemble verified Aizim npm packages"
```

---

### Task 9: Inject the npm distribution context into Python

**Acceptance criteria:** Specification section 7.3 and the npm-local Codex
portion of acceptance criterion 8.

**Files:**

- Create: `src/aizim/runtime/distribution.py`
- Create: `tests/unit/test_distribution_context.py`
- Modify: `src/aizim/runtime/__init__.py`
- Modify: `src/aizim/cli/doctor_command.py:52-126`
- Modify: `src/aizim/orchestration/codex_worker.py:80-114,222-249`
- Modify: `tests/integration/test_cli_doctor.py`
- Modify: `tests/unit/test_codex_workspace_backend.py`

**Interfaces:**

- Consumes: the six environment values set by the Rust launcher in Task 6.
- Produces:
  `DistributionContext`,
  `load_distribution_context(environ)`,
  `resolve_codex_executable(environ)`, and
  `without_distribution_environment(environ)`.

- [ ] **Step 1: Write strict distribution-context tests.**

Create `tests/unit/test_distribution_context.py` covering:

```python
NPM_ENVIRONMENT = {
    "AIZIM_DISTRIBUTION_MODE": "npm",
    "AIZIM_DISTRIBUTION_VERSION": "0.1.0",
    "AIZIM_DISTRIBUTION_TARGET": "darwin-arm64",
    "AIZIM_CODEX_EXECUTABLE": "/absolute/fixture/codex",
    "AIZIM_DISTRIBUTION_MANIFEST_SHA256": "1" * 64,
    "AIZIM_PLATFORM_MANIFEST_SHA256": "2" * 64,
}
```

Tests must assert:

- no distribution keys means source mode;
- all six exact keys produce one immutable npm context;
- any partial set, unknown mode, wrong Aizim version, unsupported target,
  malformed digest, relative path, missing file, directory, symlink, or
  non-executable path raises `DistributionError` with a stable safe code;
- npm mode returns only the injected executable even when `PATH` contains a
  different executable;
- source mode resolves `codex` from the supplied `PATH`;
- `without_distribution_environment` removes all six keys and leaves ordinary
  non-secret variables unchanged;
- `repr(context)` contains no host path.

- [ ] **Step 2: Run the test and observe the missing module.**

Run:

```bash
uv run pytest tests/unit/test_distribution_context.py -q
```

Expected: FAIL with `ModuleNotFoundError:
No module named 'aizim.runtime.distribution'`.

- [ ] **Step 3: Implement the closed Python context.**

`src/aizim/runtime/distribution.py` defines:

```python
from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from aizim import __version__

DISTRIBUTION_ENVIRONMENT: Final = frozenset(
    {
        "AIZIM_DISTRIBUTION_MODE",
        "AIZIM_DISTRIBUTION_VERSION",
        "AIZIM_DISTRIBUTION_TARGET",
        "AIZIM_CODEX_EXECUTABLE",
        "AIZIM_DISTRIBUTION_MANIFEST_SHA256",
        "AIZIM_PLATFORM_MANIFEST_SHA256",
    }
)
_TARGETS: Final = frozenset(
    {"darwin-arm64", "darwin-x64", "linux-arm64", "linux-x64"}
)


class DistributionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DistributionContext:
    mode: Literal["source", "npm"]
    version: str
    target: str | None
    codex_executable: Path | None = field(default=None, repr=False)
    distribution_manifest_sha256: str | None = field(default=None, repr=False)
    platform_manifest_sha256: str | None = field(default=None, repr=False)
```

`load_distribution_context` treats no distribution keys as source mode and
returns `codex_executable=None` until source resolution is requested; it
requires all six keys for npm mode. `_executable` resolves strictly, rejects
symlinks with `lstat`, requires a regular executable, and never includes the
path in an error. `_digest` accepts only lowercase ASCII hexadecimal of length
64.

`resolve_codex_executable` calls `load_distribution_context`; source mode uses
`shutil.which("codex", path=environ.get("PATH"))`, while npm mode returns the
validated injected path without consulting `PATH`.

- [ ] **Step 4: Consume and scrub the exact executable in composition roots.**

Change `doctor_checks` to resolve Codex once and pass its absolute string to
`_command`; change `_command` so an absolute first argument bypasses
`shutil.which` but still uses `HostCommandSpec`.

Change `create_codex_backend` to call
`resolve_codex_executable(environment)` instead of `_executable("codex")`.
Remove the private `_executable` helper after its tests move to the shared
runtime resolver.

Update `scrubbed_command_environment` so it removes both secret-marker names
and every name in `DISTRIBUTION_ENVIRONMENT`. Assert that the model-facing
Codex parent environment and shell environment contain none of the six keys.

- [ ] **Step 5: Add integration regressions for global drift.**

In `tests/integration/test_cli_doctor.py`, place a wrong `codex` first in
`PATH`, inject an exact executable through `NPM_ENVIRONMENT`, and assert the
doctor checks the injected `0.145.0` binary. Add the inverse source-mode test
showing the supplied `PATH` still works.

In `tests/unit/test_codex_workspace_backend.py`, assert backend identity is
calculated from the injected executable and that changing the global `PATH`
does not change it.

- [ ] **Step 6: Run focused and aggregate Python checks.**

Run:

```bash
uv run pytest tests/unit/test_distribution_context.py \
  tests/integration/test_cli_doctor.py \
  tests/unit/test_codex_workspace_backend.py -q
uv run ruff check src/aizim/runtime src/aizim/cli/doctor_command.py \
  src/aizim/orchestration/codex_worker.py tests/unit/test_distribution_context.py
uv run ty check
git diff --check
```

Expected: all commands PASS.

- [ ] **Step 7: Commit npm-local executable injection.**

```bash
git add src/aizim/runtime src/aizim/cli/doctor_command.py \
  src/aizim/orchestration/codex_worker.py \
  tests/unit/test_distribution_context.py \
  tests/integration/test_cli_doctor.py \
  tests/unit/test_codex_workspace_backend.py
git diff --cached --check
git commit -m "Inject the npm Codex executable"
```

---

### Task 10: Move sandbox validation behind a platform-neutral boundary

**Acceptance criteria:** Specification section 7.4 while preserving all
existing macOS behavior.

**Files:**

- Create: `src/aizim/agents/permission_profile.py`
- Create: `src/aizim/agents/probe_execution.py`
- Create: `src/aizim/agents/platform_sandbox.py`
- Create: `tests/unit/test_platform_sandbox.py`
- Modify: `src/aizim/agents/sandbox.py:41-105`
- Modify: `src/aizim/agents/macos_profile.py`
- Modify: `src/aizim/agents/macos_sandbox.py`
- Modify: `src/aizim/agents/codex_backend.py:17-114`
- Modify: `src/aizim/orchestration/codex_worker.py:80-114`
- Modify: `src/aizim/security_gate.py:13-17,115-145`
- Modify: `src/aizim/cli/security_probe_command.py`
- Modify: `tests/unit/test_codex_launch_spec.py`
- Modify: `tests/unit/test_macos_profile_contract.py`
- Modify: `tests/unit/test_sandbox_profile.py`
- Modify: `tests/security/test_macos_sandbox.py`
- Modify: `tests/security/test_authority_gate.py`

**Interfaces:**

- Consumes: exact Codex executable from Task 9.
- Produces:
  adapter-owned validation,
  `sandbox_adapter(codex_executable, platform)`,
  shared bounded probe execution, and platform-tagged launch/probe records.

- [ ] **Step 1: Write generic-contract and macOS-regression tests.**

Tests must prove:

- `SandboxRequest.runtime_read_roots` defaults to an empty tuple;
- `SandboxLaunchSpec.platform_id` and `ProbeReport.platform_id` are closed to
  `darwin` or `linux`;
- a launch spec must match request view, scratch, cwd, absolute command,
  profile ID, and 64-character policy hash;
- `build_codex_launch_spec` rejects those generic mismatches but contains no
  macOS profile parser;
- `MacOSSandboxAdapter.compile` still calls exact macOS validation before
  returning;
- `sandbox_adapter(..., "darwin")` returns a macOS adapter using the supplied
  executable;
- unsupported hosts fail closed;
- runtime read roots are exact read-only permission entries and cannot be the
  canonical project, view, scratch, or a relative/symlink path;
- every previous macOS unit, security, and Gate B output assertion remains
  true, including `seatbelt_profile=pass`.

- [ ] **Step 2: Run the focused tests and observe missing platform fields.**

Run:

```bash
uv run pytest tests/unit/test_platform_sandbox.py \
  tests/unit/test_codex_launch_spec.py \
  tests/unit/test_macos_profile_contract.py \
  tests/unit/test_sandbox_profile.py -q
```

Expected: FAIL because `platform_id`, `runtime_read_roots`, and the selector do
not exist.

- [ ] **Step 3: Extend the shared sandbox types without weakening them.**

Add:

```python
type SandboxPlatform = Literal["darwin", "linux"]


@dataclass(frozen=True, slots=True)
class SandboxRequest:
    project_root: Path
    view_root: Path
    scratch_root: Path
    command: tuple[str, ...]
    parent_env: Mapping[str, str] = field(repr=False)
    runtime_read_roots: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class SandboxLaunchSpec:
    platform_id: SandboxPlatform
    argv: tuple[str, ...]
    cwd: Path
    parent_env: Mapping[str, str] = field(repr=False)
    shell_env: Mapping[str, str]
    view_root: Path
    scratch_root: Path
    profile_id: str
    policy_hash: str


@dataclass(frozen=True, slots=True)
class ProbeReport:
    platform_id: SandboxPlatform
    passed: bool
    codex_version: str
    sandbox_executable: str
    policy_hash: str
    attempts: tuple[ProbeAttempt, ...]
    unexpected_allows: tuple[ProbeOperation, ...]
    logical_digest_before: str
    logical_digest_after: str
```

Require every adapter to expose:

```python
class SandboxAdapter(Protocol):
    @property
    def platform_id(self) -> SandboxPlatform: ...

    def compile(self, request: SandboxRequest) -> SandboxLaunchSpec: ...

    async def launch_probe(self, request: ProbeRequest) -> ProbeReport: ...
```

Define `validate_launch_spec(request, spec)` for only platform-neutral
invariants. Adapter-specific compilers perform stronger validation before
returning.

- [ ] **Step 4: Extract deterministic permission and probe helpers.**

`permission_profile.py` owns:

- exact TOML string quoting;
- closed shell environment generation;
- canonical path and overlap checks;
- read-only runtime root entries;
- canonical-project deny entries;
- deterministic policy contract hashing from temporary fixture roots.

`probe_execution.py` owns the existing bounded async subprocess code from
`macos_sandbox.py`: read attack-probe source before spawn, start a new session,
bound stdout/stderr, kill/reap the process group on every failure, reject
secret output, parse all eleven operations, append events, compare protected
digests, and construct `ProbeReport`.

The extraction must be behavior-preserving; keep the existing output limits
and reason codes exact.

- [ ] **Step 5: Make macOS validation adapter-owned.**

`MacOSSandboxAdapter.compile` calls the shared path checks, compiles the macOS
permission profile, calls `validate_macos_profile`, then returns.
`build_codex_launch_spec` calls only `validate_launch_spec`.

Remove `developer_root` from `CodexBackendDependencies` and from
`build_codex_launch_spec`; it remains an internal macOS adapter dependency.
Update every construction and test accordingly.

Build runtime read roots from:

```python
(
    Path(sys.prefix).resolve(strict=True),
    codex_executable.parents[2].resolve(strict=True),
)
```

after validating that neither overlaps the canonical Lean project. These roots
are read-only inside the model sandbox and are removed from model-visible
environment values.

- [ ] **Step 6: Select the adapter in both real entry paths.**

`platform_sandbox.py` exposes:

```python
def sandbox_adapter(
    codex_executable: Path,
    platform: str = sys.platform,
) -> SandboxAdapter:
    if platform == "darwin":
        return MacOSSandboxAdapter.for_executable(codex_executable)
    raise SandboxHostError("unsupported sandbox platform")
```

Task 11 adds Linux to the same function.

Use this selector in `create_codex_backend` and `_run_live_gate`; never
instantiate `MacOSSandboxAdapter` directly at those composition roots.
`run_security_probe` chooses the output profile line from
`report.platform_id`, retaining the exact macOS line.

- [ ] **Step 7: Run the full macOS regression surface.**

Run:

```bash
uv run pytest tests/unit/test_platform_sandbox.py \
  tests/unit/test_codex_launch_spec.py \
  tests/unit/test_macos_profile_contract.py \
  tests/unit/test_sandbox_profile.py \
  tests/security/test_macos_sandbox.py \
  tests/security/test_authority_gate.py -q
uv run ruff check src/aizim/agents src/aizim/security_gate.py \
  src/aizim/orchestration/codex_worker.py
uv run ty check
git diff --check
```

Expected: all tests and checks PASS on macOS.

- [ ] **Step 8: Commit the platform-neutral sandbox seam.**

```bash
git add src/aizim/agents src/aizim/orchestration/codex_worker.py \
  src/aizim/security_gate.py src/aizim/cli/security_probe_command.py \
  tests/unit/test_platform_sandbox.py tests/unit/test_codex_launch_spec.py \
  tests/unit/test_macos_profile_contract.py tests/unit/test_sandbox_profile.py \
  tests/security/test_macos_sandbox.py tests/security/test_authority_gate.py
git diff --cached --check
git commit -m "Make sandbox validation platform-neutral"
```

---

### Task 11: Implement and qualify the Linux sandbox adapter

**Acceptance criteria:** Specification section 7.4 and acceptance criteria
10–11.

**Files:**

- Create: `src/aizim/agents/linux_profile.py`
- Create: `src/aizim/agents/linux_sandbox.py`
- Create: `tests/unit/test_linux_profile_contract.py`
- Create: `tests/unit/test_linux_sandbox_profile.py`
- Create: `tests/security/test_linux_sandbox.py`
- Modify: `src/aizim/agents/platform_sandbox.py`
- Modify: `src/aizim/agents/__init__.py`
- Modify: `src/aizim/cli/doctor_command.py:24-126`
- Modify: `src/aizim/cli/security_probe_command.py`
- Modify: `pyproject.toml`
- Modify: `tests/integration/test_cli_doctor.py`
- Modify: `tests/security/test_authority_gate.py`
- Modify: `docs/security/authority-boundary.md`

**Interfaces:**

- Consumes: shared sandbox/probe helpers from Task 10 and the local native Codex
  executable from Task 9.
- Produces:
  `LinuxSandboxAdapter`,
  `LinuxSandboxDependencies`,
  `compile_linux_profile`,
  `validate_linux_profile`, and real Linux Gate B evidence.

- [ ] **Step 1: Write Linux host and profile unit tests.**

Use injected dependencies to cover:

- only `sys.platform == "linux"` is accepted;
- Codex output must equal `codex-cli 0.145.0`;
- native Codex must be absolute, regular, and executable;
- bundled bwrap must be exactly
  `<codex-triple-root>/codex-resources/bwrap`, regular, and executable;
- the permission profile has `approval_policy="never"`, network disabled,
  read-only view/runtime roots, writable scratch only, and canonical project
  denies;
- shell `PATH` is `/usr/local/bin:/usr/bin:/bin`;
- `TMPDIR` is the private scratch root;
- policy hashes are root-independent but detect concrete-root tampering;
- profile argv contains no bypass, approval, network allow, broad writable
  root, raw environment, or user configuration route;
- invalid user namespaces/bwrap execution fails closed before a worker launch.

- [ ] **Step 2: Write the real Linux attack-probe test.**

`tests/security/test_linux_sandbox.py` mirrors the macOS test's eleven
operation assertions and is marked `linux_sandbox`. It must assert:

```python
assert report.platform_id == "linux"
assert report.codex_version == "codex-cli 0.145.0"
assert report.sandbox_executable.endswith("/codex-resources/bwrap")
assert all(attempt.verdict == "denied" for attempt in report.attempts[:9])
assert all(attempt.verdict == "allowed" for attempt in report.attempts[9:])
assert report.unexpected_allows == ()
```

It also checks zero TCP/Unix-socket connections, unchanged assets/state,
successful replay, no secret/path serialization, and exact 11 probe events.

- [ ] **Step 3: Run unit tests on macOS and observe missing Linux modules.**

Run:

```bash
uv run pytest tests/unit/test_linux_profile_contract.py \
  tests/unit/test_linux_sandbox_profile.py -q
```

Expected: FAIL with missing `aizim.agents.linux_profile`.

- [ ] **Step 4: Implement the Linux permission profile.**

`compile_linux_profile` uses the shared permission builder and this fixed
Codex prefix:

```python
_PROFILE_ID = "aizim-worker"
_FIXED_LAUNCH_POLICY = (
    "sandbox",
    "--permission-profile",
    _PROFILE_ID,
    "--sandbox-state-disable-network",
    "--log-denials",
    "-C",
)
```

The resulting argv is:

```text
<native-codex>
-c default_permissions="aizim-worker"
-c approval_policy="never"
-c permissions.aizim-worker={...network={enabled=false}}
-c shell_environment_policy={inherit="none",...}
sandbox --permission-profile aizim-worker
--sandbox-state-disable-network --log-denials
-C <view> <absolute-command> <arguments...>
```

`validate_linux_profile` reconstructs and compares the full expected contract,
including exact runtime roots, rather than checking for selected substrings.

- [ ] **Step 5: Implement the adapter and real probe.**

`LinuxSandboxDependencies` contains only safe injectable callables:

```python
@dataclass(frozen=True, slots=True)
class LinuxSandboxDependencies:
    platform: str
    codex_executable: Path
    codex_version: Callable[[], str]
    bundled_bwrap: Callable[[], Path]
    bwrap_usable: Callable[[Path], bool]
```

`LinuxSandboxAdapter._validate_host` checks all five conditions and rejects any
exception as `SandboxHostError`. `compile` performs the same canonical,
ephemeral, ownership, mode, overlap, command, and runtime-root checks as
macOS. `launch_probe` calls the shared bounded probe executor and reports the
verified bwrap path as the mechanism.

Do not add a Landlock or unsandboxed fallback.

- [ ] **Step 6: Wire selection, doctor, output, and markers.**

Add Linux selection to `sandbox_adapter`. Add this marker:

```toml
"linux_sandbox: requires Linux and packaged Codex CLI 0.145.0"
```

Doctor keeps the stable check ID `sandbox_exec` but reports:

- `sandbox-exec available` on macOS;
- `Codex Linux sandbox available` only after Linux host validation;
- `sandbox mechanism is unavailable` on failure.

Security-probe output is identical across platforms except:

```text
seatbelt_profile=pass
```

on macOS becomes:

```text
linux_profile=pass
```

on Linux. Update exact-output tests for both.

- [ ] **Step 7: Run all unit and platform-independent tests.**

Run on macOS:

```bash
uv run pytest tests/unit/test_linux_profile_contract.py \
  tests/unit/test_linux_sandbox_profile.py \
  tests/unit/test_platform_sandbox.py \
  tests/integration/test_cli_doctor.py \
  tests/security/test_authority_gate.py -q
uv run ruff check src/aizim/agents tests/unit/test_linux_profile_contract.py \
  tests/unit/test_linux_sandbox_profile.py
uv run ty check
git diff --check
```

Expected: unit and simulated-host tests PASS; the real Linux marker remains
unselected on macOS.

- [ ] **Step 8: Run real Linux Gate B on each Linux architecture.**

On `ubuntu-24.04` and `ubuntu-24.04-arm`, run:

```bash
npm install --ignore-scripts
npm run build
npm test
build/npm/linux-$(node -p 'process.arch')/vendor/uv run \
  pytest tests/security/test_linux_sandbox.py -m linux_sandbox -q
build/npm/linux-$(node -p 'process.arch')/vendor/uv run \
  aizim security-probe \
  --project tests/fixtures/attack_probe_project \
  --backend codex --no-model
```

Expected: the pytest passes and the command begins
`SECURITY GATE PASS`, includes `linux_profile=pass`, and exits 0. This step is
executed and evidenced by Task 13 CI before Linux support is claimed.

- [ ] **Step 9: Commit the Linux adapter.**

```bash
git add src/aizim/agents src/aizim/cli/doctor_command.py \
  src/aizim/cli/security_probe_command.py pyproject.toml \
  tests/unit/test_linux_profile_contract.py \
  tests/unit/test_linux_sandbox_profile.py \
  tests/security/test_linux_sandbox.py \
  tests/integration/test_cli_doctor.py \
  tests/security/test_authority_gate.py \
  docs/security/authority-boundary.md
git diff --cached --check
git commit -m "Add the fail-closed Linux sandbox"
```

---

### Task 12: Exercise real local and global npm installations

**Acceptance criteria:** Specification sections 4, 12.4, 12.5, and acceptance
criteria 1–8 and 11–12 on the current host.

**Files:**

- Create: `scripts/npm/test-python.mjs`
- Create: `scripts/npm/install-smoke.mjs`
- Create: `scripts/npm/test.mjs`
- Create: `tests/npm/install-smoke.test.mjs`
- Modify: `scripts/npm/check.mjs`
- Modify: `package.json`

**Interfaces:**

- Consumes: verified current-host tarballs, bundled uv, local Codex native
  package, and existing smoke Lean fixture.
- Produces: stable `npm test`, fresh local/global install evidence, first-run
  provisioning evidence, cache-reuse evidence, and fail-closed negative
  evidence.

- [ ] **Step 1: Write install-driver unit tests.**

With injected command and temporary-directory factories, assert the driver:

- refuses to use non-private or broad temporary roots;
- installs only the two expected tarballs with `--ignore-scripts`;
- creates distinct local, global, project, home, cache, and npm-prefix roots;
- uses the package-local `.bin/aizim`, never global `aizim`;
- runs `npx --no-install aizim` so npm cannot fetch an undeclared package;
- sets `HOME`, `AIZIM_CACHE_DIR`, npm prefix, and a minimal Node-containing
  `PATH`;
- runs `--version` twice and checks the same ready marker;
- uninstalls the local packages and preserves the completed Aizim cache plus
  an unrelated cache sentinel;
- tests meta-only `--omit=optional` installation and expects exit 78;
- tests a modified manifest copy and expects exit 74;
- removes only its generated private root in `finally`.

- [ ] **Step 2: Run the test and observe the missing driver.**

Run:

```bash
node --test tests/npm/install-smoke.test.mjs
```

Expected: FAIL with missing `scripts/npm/install-smoke.mjs`.

- [ ] **Step 3: Implement Python checks using the bundled uv.**

`test-python.mjs` resolves the current platform's bundled uv and runs:

```text
<uv> --no-config sync --frozen
<uv> --no-config run ruff check .
<uv> --no-config run ty check
<uv> --no-config run pytest \
  -m "not manual_real_codex" -q
```

It passes no `UV_*`, `PIP_*`, or Python injection environment from the host
except its explicit repository-local uv cache. It preserves pytest's numeric
exit status and prints no environment.

- [ ] **Step 4: Implement the fresh-consumer installation.**

`install-smoke.mjs`:

1. validates the two exact tarball names from the build summary;
2. creates one mode-0700 root with `mkdtemp`;
3. installs platform and meta tarballs locally with
   `npm install --ignore-scripts --no-audit --no-fund`;
4. runs `<consumer>/node_modules/.bin/aizim --version`;
5. runs `npx --no-install aizim --version` from the consumer and requires the
   same output;
6. records the single exact `READY.json` and its SHA-256;
7. runs `--version` again and requires the same SHA and mtime;
8. copies `tests/fixtures/attack_probe_project` without generated state;
9. runs `aizim init`, `aizim doctor`, no-model `security-probe`, and the
   deterministic fake `aizim run`;
10. requires `READY`, `SECURITY GATE PASS`, and `AIZIM RUN PASS`;
11. writes an unrelated cache sentinel, uninstalls both local packages with
    lifecycle scripts disabled, and requires both the ready runtime and
    sentinel to remain byte-identical;
12. installs both tarballs into a temporary global prefix and repeats
    `aizim --version`;
13. installs meta with `--omit=optional` into a separate consumer and requires
    `PLATFORM_PACKAGE_MISSING` with exit 78;
14. cleans only the validated generated root.

The test does not run the credentialed real-Codex path.
Before cleanup it writes `build/npm/install-smoke-evidence.json` last with the
full Git SHA, target, package hashes, first-run ready-marker hash and mtime,
second-run matching hash and mtime, local/global version results, Gate B
result, deterministic-run result, `npx --no-install` result, uninstall cache
preservation, missing-package exit 78, and corrupt-manifest exit 74. The file
contains no home, cache, project, or prefix paths.

- [ ] **Step 5: Compose the root `npm test` contract.**

`test.mjs` requires a fresh matching build summary, then runs in order:

```text
node --test tests/npm/*.test.mjs
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --locked
node scripts/npm/test-python.mjs
node scripts/npm/install-smoke.mjs
node scripts/npm/check.mjs
```

Keep `package.json` root scripts mapped to this file and the narrower commands
from Task 1. No script publishes or modifies tracked source.

- [ ] **Step 6: Run the real current-host user journey.**

Run:

```bash
npm install --ignore-scripts
npm run build
npm test
npm run pack
```

Expected: every command exits 0; the install smoke observes:

```text
aizim 0.1.0
READY
SECURITY GATE PASS
AIZIM RUN PASS
```

and produces exactly two tarballs.

- [ ] **Step 7: Re-run the existing uv-native acceptance surface.**

Use a fresh private smoke-project copy and run:

```bash
: "${TMPDIR:?TMPDIR must be the private per-user temporary directory}"
case "${TMPDIR%/}" in
  /tmp|/tmp/*|/private/tmp|/private/tmp/*|/var/tmp|/var/tmp/*|/private/var/tmp|/private/var/tmp/*)
    echo "unsafe TMPDIR" >&2
    exit 1
    ;;
esac
AIZIM_UV_SMOKE_BASE="$(mktemp -d "${TMPDIR%/}/aizim-uv-smoke.XXXXXX")"
chmod 0700 "$AIZIM_UV_SMOKE_BASE"
AIZIM_FAKE_ROOT="$AIZIM_UV_SMOKE_BASE/project"
mkdir -m 0700 "$AIZIM_FAKE_ROOT"
rsync -a --exclude '.aizim' --exclude '.lake' \
  examples/smoke_lean/ "$AIZIM_FAKE_ROOT/"
uv sync --frozen
uv run pytest -m "not manual_real_codex" -q
uv run ruff check .
uv run ty check
uv run aizim init "$AIZIM_FAKE_ROOT"
uv run aizim security-probe \
  --project "$AIZIM_FAKE_ROOT" --backend codex --no-model
uv run aizim run \
  --project "$AIZIM_FAKE_ROOT" \
  --profile autonomous-shared --backend fake
uv run python scripts/check_foundation.py \
  --project "$AIZIM_FAKE_ROOT" --run-id latest-fake
git diff --check
```

Expected: tests/lint/type checks PASS, Gate B prints
`SECURITY GATE PASS`, run prints `AIZIM RUN PASS`, and the checker prints
`FOUNDATION ACCEPTANCE PASS 16/16`.

- [ ] **Step 8: Commit the end-to-end npm test surface.**

```bash
git add package.json package-lock.json scripts/npm \
  tests/npm/install-smoke.test.mjs
git diff --cached --check
git commit -m "Test fresh npm Aizim installations"
```

---

### Task 13: Qualify all four native targets in CI

**Acceptance criteria:** Specification section 13 and acceptance criteria
1–12 on macOS arm64, macOS x64, Linux arm64, and Linux x64.

**Files:**

- Create: `scripts/npm/write-ci-evidence.mjs`
- Create: `scripts/npm/verify-ci-evidence.mjs`
- Create: `tests/npm/ci-evidence.test.mjs`
- Modify: `scripts/npm/install-smoke.mjs`
- Modify: `scripts/npm/test.mjs`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**

- Consumes: build, pack, and install-smoke summaries from Tasks 7, 8, and 12.
- Produces: one strict native evidence document per target and one aggregate
  verification that is bound to an exact full Git SHA.

- [ ] **Step 1: Write strict evidence-contract tests.**

`tests/npm/ci-evidence.test.mjs` builds four temporary artifact directories.
For the schema fixture, each tarball file contains the single ASCII byte `x`,
matching the concrete digests below. Each valid native document has exactly
this shape:

```json
{
  "schema_version": 1,
  "commit_sha": "1111111111111111111111111111111111111111",
  "version": "0.1.0",
  "target": "darwin-arm64",
  "runner": {
    "os": "darwin",
    "arch": "arm64",
    "libc": null
  },
  "packages": [
    {
      "name": "@aiz.im/aizim-darwin-arm64",
      "filename": "aiz.im-aizim-darwin-arm64-0.1.0.tgz",
      "size": 1,
      "sha256": "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881",
      "integrity": "sha512-pKvURIxJVi2CgRXROh/M6pJ/UrTVRZKX+LQ+QtqJI4vBNibkPcs43bCCSIkn7JBPtCBXRDmD6IWFF51QVRr+Yg=="
    },
    {
      "name": "@aiz.im/aizim",
      "filename": "aiz.im-aizim-0.1.0.tgz",
      "size": 1,
      "sha256": "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881",
      "integrity": "sha512-pKvURIxJVi2CgRXROh/M6pJ/UrTVRZKX+LQ+QtqJI4vBNibkPcs43bCCSIkn7JBPtCBXRDmD6IWFF51QVRr+Yg=="
    }
  ],
  "checks": {
    "source_build": true,
    "npm_test": true,
    "local_install": true,
    "global_install": true,
    "npx_no_install": true,
    "python_314_bootstrap": true,
    "cache_reused": true,
    "uninstall_preserved_cache": true,
    "local_codex_01450": true,
    "global_codex_01450": true,
    "ready": true,
    "security_gate": true,
    "aizim_run": true,
    "missing_platform_exit_78": true,
    "integrity_failure_exit_74": true
  }
}
```

The other target documents change only target-specific package and runner
fields. A separate minimum-runtime document records Node `v22.22.2`, the
Linux x64 target, exact package hashes, local/global/`npx --no-install`
version success, Python 3.14.6 bootstrap, cache reuse, Gate B, and deterministic
run success. Tests require the exact target set plus that document and reject:

- an unknown or duplicate target;
- an unknown JSON field;
- a non-full or different commit SHA;
- a different version;
- a false or missing check;
- a path outside the downloaded artifact root;
- a missing or modified tarball;
- a mismatched SHA-256, size, or npm integrity;
- different meta-package bytes between targets;
- Linux evidence without `glibc`;
- macOS evidence with a libc value.

- [ ] **Step 2: Run the evidence test and observe missing modules.**

Run:

```bash
node --test tests/npm/ci-evidence.test.mjs
```

Expected: FAIL with `ERR_MODULE_NOT_FOUND`.

- [ ] **Step 3: Implement evidence writing and aggregation.**

`write-ci-evidence.mjs` accepts only:

```text
--target <target>
--minimum-node-version <node-version>  # optional, minimum-runtime mode only
--output <absolute-private-path>
```

It detects the live host through Task 2's resolver, requires the requested
target to match, reads the three generated summaries, recomputes both tarball
hashes and sizes, requires every install-smoke result, and writes the evidence
document atomically with mode 0600. It derives `commit_sha` from the summaries
and `git rev-parse HEAD`; it never accepts a caller-supplied success flag. If
the minimum-version option is present, it additionally requires
`process.version` and the install-smoke evidence to equal that exact value and
writes the separate minimum-runtime schema.

`verify-ci-evidence.mjs` accepts:

```text
--directory <absolute-artifact-root>
--commit <full-git-sha>
--version <semver>
--minimum-node-version <node-version>
--output <absolute-private-path>
```

It validates the four documents and their tarballs, then writes one aggregate
document containing schema 1, the exact commit, version, sorted target list,
the common meta digest and integrity, and the four platform digests and
integrities. It separately verifies the minimum-runtime document against
`v22.22.2`. It writes no home paths, runner temporary paths, environments,
credentials, or URLs.

- [ ] **Step 4: Make the install smoke evidence authoritative.**

Update `install-smoke.mjs` so its evidence is written only after every
positive and negative case has been observed. Update `test.mjs` to require
that file, parse it strictly, and reject a stale Git SHA or target. Unit tests
must prove that a failed Gate B, changed ready marker, global Codex resolution,
or missing negative test prevents the evidence file from being written.

- [ ] **Step 5: Add the four native CI jobs and aggregate gate.**

Extend `.github/workflows/ci.yml` with this exact matrix:

| target | runner | os | arch | libc |
| --- | --- | --- | --- | --- |
| `darwin-arm64` | `macos-15` | `darwin` | `arm64` | null |
| `darwin-x64` | `macos-15-intel` | `darwin` | `x64` | null |
| `linux-arm64` | `ubuntu-24.04-arm` | `linux` | `arm64` | `glibc` |
| `linux-x64` | `ubuntu-24.04` | `linux` | `x64` | `glibc` |

Set `fail-fast: false` and a 45-minute timeout. Every native job:

1. checks out with
   `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1`;
2. installs Node from `.node-version` with
   `actions/setup-node@820762786026740c76f36085b0efc47a31fe5020`;
3. installs the pinned Lean toolchain with
   `leanprover/lean-action@38fbc41a8c28c4cbaec22d7f7de508ec2e7c0dd9`,
   both GitHub and Mathlib caches disabled;
4. runs the documented source command `npm install`, then requires
   `package-lock.json` to remain unchanged and reruns the no-lifecycle package
   contract;
5. runs `npm run build`, `npm test`, and `npm run pack`;
6. writes `dist/npm/native-evidence-<target>.json`;
7. uploads the two tarballs, manifests, summaries, and native evidence with
   `actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a`.

The artifact name is
`aizim-native-<target>-${{ github.sha }}`. Use
`if-no-files-found: error` and 14-day retention. Do not restore npm, Cargo,
uv, Python, or build caches in these jobs.

The aggregate job runs on `ubuntu-24.04`, depends on all native jobs,
downloads artifacts with
`actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c`,
and runs:

```bash
node scripts/npm/verify-ci-evidence.mjs \
  --directory "$RUNNER_TEMP/aizim-native-evidence" \
  --commit "$GITHUB_SHA" \
  --version 0.1.0 \
  --minimum-node-version v22.22.2 \
  --output "$RUNNER_TEMP/aizim-native-evidence/aggregate.json"
```

It uploads `aggregate.json` with the pinned upload action. No native or
aggregate job receives a model or npm credential.

Add a `minimum-node` job on `ubuntu-24.04`. It first installs Node `26.5.0`,
Lean, dependencies, and builds/packs the Linux x64 tarballs without caches.
It then invokes the pinned setup-node action again with `node-version:
"22.22.2"` and runs:

```bash
node --test tests/npm/package-contract.test.mjs \
  tests/npm/platform-resolution.test.mjs \
  tests/npm/codex-resolution.test.mjs \
  tests/npm/assets.test.mjs \
  tests/npm/launch.test.mjs
node scripts/npm/install-smoke.mjs
node -e 'const x=require("./build/npm/install-smoke-evidence.json"); if(x.node_version!=="v22.22.2") process.exit(1)'
node scripts/npm/write-ci-evidence.mjs \
  --target linux-x64 \
  --minimum-node-version v22.22.2 \
  --output dist/npm/minimum-node-evidence.json
```

It uploads the strict minimum-runtime document and exact tarballs as
`aizim-minimum-node-${{ github.sha }}`. The aggregate job depends on this job
as well as the native matrix and rejects missing or non-`v22.22.2` evidence.

- [ ] **Step 6: Run local evidence and workflow checks.**

Run:

```bash
node --test tests/npm/ci-evidence.test.mjs
npm run test:node
ruby -e 'require "yaml"; YAML.load_file(".github/workflows/ci.yml", aliases: true)'
rg -n "uses:" .github/workflows/ci.yml
git diff --check
```

Expected: tests PASS, YAML parses, every `uses:` value is a full immutable
SHA, and the diff check exits 0.

- [ ] **Step 7: Commit four-platform CI.**

```bash
git add .github/workflows/ci.yml scripts/npm \
  tests/npm/ci-evidence.test.mjs
git diff --cached --check
git commit -m "Qualify npm packages on four native targets"
```

- [ ] **Step 8: Obtain explicit push authorization and observe CI.**

This is a GitHub external-write gate. Do not push until the user explicitly
authorizes the exact commit range. After authorization:

```bash
AIZIM_CI_SHA="$(git rev-parse HEAD)"
git push origin main
for AIZIM_CI_POLL in {1..12}; do
  AIZIM_CI_RUN_ID="$(gh run list \
    --workflow CI --branch main --commit "$AIZIM_CI_SHA" \
    --limit 1 --json databaseId,headSha \
    --jq 'map(select(.headSha == "'"$AIZIM_CI_SHA"'"))[0].databaseId')"
  test -n "$AIZIM_CI_RUN_ID" && break
  sleep 5
done
test -n "$AIZIM_CI_RUN_ID"
gh run watch --exit-status "$AIZIM_CI_RUN_ID"
```

Download the successful run's aggregate artifact into a new mode-0700
temporary directory and rerun `verify-ci-evidence.mjs` against the pushed
full SHA. Task 13 is complete only after all four native jobs, the
minimum-Node job, and the aggregate job are green at that SHA.

---

### Task 14: Document npm use and prepare non-publishing release workflows

**Acceptance criteria:** Specification sections 4, 14, and 16, with no npm
registry mutation.

**Files:**

- Create: `docs/operations/npm-distribution.md`
- Create: `.github/workflows/npm-release.yml`
- Create: `.github/workflows/npm-registry-smoke.yml`
- Create: `scripts/npm/verify-release-bundle.mjs`
- Create: `scripts/npm/registry-verify.mjs`
- Create: `scripts/npm/registry-smoke.mjs`
- Create: `scripts/npm/write-release-record.mjs`
- Create: `tests/npm/release-tools.test.mjs`
- Create: `tests/npm/release-workflow.test.mjs`
- Modify: `README.md`
- Modify: `docs/operations/foundation-runbook.md`
- Modify: `package.json`

**Interfaces:**

- Consumes: the four-target evidence contract from Task 13.
- Produces: separate source/npm user instructions, a fresh-build release
  bundle verifier, a read-only registry smoke workflow, and a release workflow
  that cannot publish in this task.

- [ ] **Step 1: Write release-tool tests.**

`tests/npm/release-tools.test.mjs` covers:

- a release bundle must contain exactly four platform tarballs, one
  byte-identical meta tarball, four native evidence documents, and one
  aggregate document;
- version, full Git SHA, target, digest, size, and integrity must agree;
- a dirty tracked-input flag is rejected;
- a release version must equal Python, npm, Cargo, and all platform versions;
- registry verification requires the exact version and expected
  `dist.integrity`;
- registry 404 is distinguishable from a mismatched existing version;
- registry polling stops after 12 attempts with five-second intervals;
- registry smoke evidence accepts only all-four-target `READY`,
  `SECURITY GATE PASS`, and `AIZIM RUN PASS` results;
- release-record output is deterministic and contains no filesystem paths,
  authenticated URLs, npm identity token, environment, or credentials.

All subprocess and delay functions are injected in tests. No test reads or
writes the live npm registry.

- [ ] **Step 2: Write disabled-release workflow tests.**

`tests/npm/release-workflow.test.mjs` reads both workflow files as UTF-8 and
asserts exact job, permission, trigger, runner, action-SHA, and command
fragments; the separate Ruby command in Step 8 performs general YAML parsing.
The Node test requires:

- `.github/workflows/npm-release.yml` has only `workflow_dispatch`;
- release inputs are exact `version` and `commit_sha` strings;
- the same four native runners from Task 13 are used;
- builds restore no cache and upload exact-SHA evidence;
- the aggregate verifier is mandatory;
- no step contains `npm publish`;
- workflow permissions are only `contents: read`;
- neither `id-token: write`, `NODE_AUTH_TOKEN`, nor an npm secret occurs;
- `.github/workflows/npm-registry-smoke.yml` is manual-only, accepts one exact
  version, uses all four runners, and contains no registry write command.

- [ ] **Step 3: Run the tests and observe missing release tools.**

Run:

```bash
node --test tests/npm/release-tools.test.mjs \
  tests/npm/release-workflow.test.mjs
```

Expected: FAIL with missing modules or workflow files.

- [ ] **Step 4: Implement fresh release-bundle verification.**

`verify-release-bundle.mjs` wraps the Task 13 aggregate verifier and adds:

```text
--directory <absolute-artifact-root>
--commit <full-git-sha>
--version <semver>
--output <absolute-private-path>
```

It requires release evidence to report no restored build cache and no dirty
tracked input. It copies no artifact and mutates no registry state.

`registry-verify.mjs` invokes:

```text
npm view <exact-name>@<exact-version> version dist.integrity --json
```

without a shell. In wait mode it retries a registry 404 for at most 12
attempts separated by five seconds. It succeeds only when both version and
integrity equal the release bundle and can atomically write a redacted JSON
receipt.

`registry-smoke.mjs` creates a mode-0700 consumer, home, npm prefix, cache, and
project root below the runner's private temporary directory. It installs only
`@aiz.im/aizim@<exact-version>` with lifecycle scripts disabled, then observes
local and global version output, first-run Python 3.14.6 bootstrap, cache reuse,
doctor, Gate B, deterministic fake run, and foundation acceptance. It writes
one path-free evidence document and removes only its validated generated
root.

`write-release-record.mjs` consumes the verified bundle, five registry
receipts, and four registry-smoke evidence documents and writes a deterministic
Markdown record with exact version, Git SHA, workflow run IDs, package
integrities, target results, the private-repository provenance limitation, and
the statement that no credentialed model run was performed.

- [ ] **Step 5: Create the non-publishing release-candidate workflow.**

`.github/workflows/npm-release.yml` is manual-only in this task. It requires
the operator to supply `version` and a full `commit_sha`, checks out that exact
commit, and runs the Task 13 native build/test/pack/evidence path on all four
runners with no restored caches. Its aggregate job calls
`verify-release-bundle.mjs`. Every action uses the immutable SHAs from Task
13. It uploads the release bundle for 14 days and has no publish job,
environment, OIDC permission, npm credential, tag trigger, or registry write.

- [ ] **Step 6: Create the read-only public-registry smoke workflow.**

`.github/workflows/npm-registry-smoke.yml` is manual-only and accepts an exact
version string. On each of the four native runners it:

1. checks out the exact repository ref used to dispatch the workflow;
2. installs Node and Lean with the pinned actions;
3. runs `registry-smoke.mjs --version <version> --output <private-path>`;
4. uploads the path-free evidence document;
5. aggregates the exact four target documents and rejects any false or
   missing result.

It installs only public packages, has `contents: read`, and receives no npm or
model credential.

- [ ] **Step 7: Split source and npm documentation.**

Update `README.md` with:

- global `npm install --global @aiz.im/aizim` and project-local
  `npm install --save-dev @aiz.im/aizim`;
- first use may download managed CPython 3.14.6;
- Node `>=22.22.2`, external Lean/elan, and glibc-only Linux requirements;
- npm mode uses package-local Codex 0.145.0 and bundled uv, never global
  Python, uv, or Codex;
- source mode remains `uv sync --frozen` for Python development or
  `npm install --ignore-scripts && npm run build` for the complete source
  distribution build;
- uninstalling npm packages does not remove projects or Aizim runtime caches.

Update `foundation-runbook.md` so its existing global Codex prerequisite is
clearly limited to the uv-native path. Add
`docs/operations/npm-distribution.md` with exact current-host build, pack,
local/global tarball install, cache, failure-code, four-platform evidence,
first-publication, partial-publication recovery, and Trusted Publishing
procedures. The runbook states that build/test/pack and both workflows in this
task never publish.

- [ ] **Step 8: Run the release-readiness audit.**

Run:

```bash
node --test tests/npm/release-tools.test.mjs \
  tests/npm/release-workflow.test.mjs
npm run test:node
ruby -e 'require "yaml"; %w[.github/workflows/npm-release.yml .github/workflows/npm-registry-smoke.yml].each { |p| YAML.load_file(p, aliases: true) }'
if rg -n "npm publish|id-token: write|NODE_AUTH_TOKEN|NPM_TOKEN" \
  .github/workflows/npm-release.yml \
  .github/workflows/npm-registry-smoke.yml; then
  exit 1
fi
npm install --ignore-scripts
npm run build
npm test
npm run pack
uv run pytest -m "not manual_real_codex" -q
uv run ruff check .
uv run ty check
git diff --check
```

Expected: every check exits 0, two current-host tarballs are produced, and
the secret/publish scan finds no match.

- [ ] **Step 9: Commit release readiness.**

```bash
git add README.md docs/operations/foundation-runbook.md \
  docs/operations/npm-distribution.md \
  .github/workflows/npm-release.yml \
  .github/workflows/npm-registry-smoke.yml \
  scripts/npm tests/npm package.json package-lock.json
git diff --cached --check
git commit -m "Prepare Aizim npm release verification"
```

Do not dispatch a workflow, create a tag, push, create an npm package, or
publish from this task.

---

### Task 15: Perform the separately authorized first npm publication

**Acceptance criteria:** Specification sections 14.1–14.3 and acceptance
criterion 13.

**Files:**

- Create after successful verification: `docs/releases/npm-0.1.0.md`

**External writes:** annotated Git tag and push, GitHub workflow dispatch,
five first-time public npm publications, registry-smoke dispatch, and the
release-record commit/push.

- [ ] **Step 1: Obtain publication-specific authorization.**

Stop before every action below until the user approves this exact scope:

```text
version: 0.1.0
repository: AIZ-IM/Aizim
tag: v0.1.0
packages:
  @aiz.im/aizim-darwin-arm64
  @aiz.im/aizim-darwin-x64
  @aiz.im/aizim-linux-arm64
  @aiz.im/aizim-linux-x64
  @aiz.im/aizim
access: public
workflow: .github/workflows/npm-release.yml
```

The approval must also cover pushing the exact local release commit range to
`origin/main`, pushing the release tag, dispatching GitHub workflows, and
pushing the generated release record. It does not authorize Trusted Publisher
configuration, which remains Task 16.

- [ ] **Step 2: Freeze and verify the release commit.**

After authorization, require:

```bash
git diff --quiet
git diff --cached --quiet
test "$(git branch --show-current)" = main
git fetch origin main
git merge-base --is-ancestor origin/main HEAD
npm whoami
npm org ls aiz.im --json
```

Expected npm identity is `frankieew`, and the organization response must show
owner access. Run the full Task 14 release-readiness audit again, then push
only the reviewed fast-forward commit range and verify the remote:

```bash
AIZIM_RELEASE_VERSION=0.1.0
AIZIM_RELEASE_SHA="$(git rev-parse HEAD)"
test "${#AIZIM_RELEASE_SHA}" -eq 40
git push origin main
git fetch origin main
test "$AIZIM_RELEASE_SHA" = "$(git rev-parse origin/main)"
git tag -a "v${AIZIM_RELEASE_VERSION}" "$AIZIM_RELEASE_SHA" \
  -m "Aizim npm ${AIZIM_RELEASE_VERSION}"
git push origin "v${AIZIM_RELEASE_VERSION}"
```

If the tag already exists, require it to resolve to the same full SHA; never
move or replace a published release tag.

- [ ] **Step 3: Build the uncached four-platform release bundle.**

Dispatch the manual workflow at the tag:

```bash
gh workflow run npm-release.yml \
  --repo AIZ-IM/Aizim \
  --ref "v${AIZIM_RELEASE_VERSION}" \
  -f "version=${AIZIM_RELEASE_VERSION}" \
  -f "commit_sha=${AIZIM_RELEASE_SHA}"
```

Resolve the new run ID by workflow, tag, and creation time, then use
`gh run watch --exit-status`. Download all artifacts into a new mode-0700
temporary root and run `verify-release-bundle.mjs` with the exact version and
SHA. Do not publish if any target, Gate B result, deterministic run, digest,
integrity, version, or commit differs.

- [ ] **Step 4: Perform an idempotent registry preflight.**

For each package in platform-first order, query the exact version:

```text
npm view <name>@0.1.0 version dist.integrity --json
```

Classify each result:

- registry 404: package/version is absent and eligible for first publication;
- exact existing version and matching release-bundle integrity: already
  complete, so do not republish;
- existing version with a different or missing integrity: stop immediately.

Save redacted receipts below the private release root. Do not change access,
owners, or two-factor settings during the preflight.

- [ ] **Step 5: Publish and verify all platform packages first.**

Publish only missing platform versions, in this exact order:

```text
@aiz.im/aizim-darwin-arm64
@aiz.im/aizim-darwin-x64
@aiz.im/aizim-linux-arm64
@aiz.im/aizim-linux-x64
```

For each one:

```bash
npm publish "<verified-platform-tarball>" --access public
node scripts/npm/registry-verify.mjs \
  --package "<exact-platform-package>" \
  --version "$AIZIM_RELEASE_VERSION" \
  --integrity "<bundle-integrity>" \
  --wait \
  --output "<private-receipt-path>"
```

Use the interactive npm authentication flow; never place a token or one-time
password in a command, file, log, or workflow. Because the source repository
is private, do not request or claim npm provenance.

If any platform publication fails, do not publish the meta package. Preserve
the verified tarballs and receipts, report the exact completed package set,
and resume later with the same bytes. Never unpublish, overwrite, or reuse
version `0.1.0` with different bytes.

- [ ] **Step 6: Publish the meta package last.**

Only after all four exact platform versions and integrities are visible:

```bash
npm publish "<verified-meta-tarball>" --access public
node scripts/npm/registry-verify.mjs \
  --package "@aiz.im/aizim" \
  --version "$AIZIM_RELEASE_VERSION" \
  --integrity "<bundle-integrity>" \
  --wait \
  --output "<private-receipt-path>"
```

Then verify that the registry meta manifest contains the four exact
`optionalDependencies`, exact `@openai/codex` version `0.145.0`, public
access, and no lifecycle scripts.

- [ ] **Step 7: Observe public installation on all four targets.**

Dispatch `.github/workflows/npm-registry-smoke.yml` at the release tag with
version `0.1.0`. Watch the run to completion, download the four evidence
documents, and require on every target:

```text
aizim 0.1.0
READY
SECURITY GATE PASS
AIZIM RUN PASS
```

Also require first-run Python 3.14.6 provisioning, second-run cache reuse,
package-local Codex 0.145.0, and both local and temporary-global invocation.
No workflow receives a model or npm credential.

- [ ] **Step 8: Generate and commit the immutable release record.**

Run `write-release-record.mjs` with the verified bundle summary, five registry
receipts, four registry-smoke documents, release workflow run ID, and smoke
workflow run ID. Write
`docs/releases/npm-0.1.0.md`, inspect it for credentials and local paths, then:

```bash
git add docs/releases/npm-0.1.0.md
git diff --cached --check
git commit -m "Record Aizim npm 0.1.0 release"
git push origin main
```

Task 15 is complete only when a fresh public registry installation succeeds
on all four targets. Until then, describe the state as partial publication or
release-ready, never publicly available.

---

### Task 16: Configure npm Trusted Publishing and enable tag releases

**Acceptance criteria:** Specification sections 14.2–14.3 after the five
packages exist.

**Files:**

- Modify: `.github/workflows/npm-release.yml`
- Modify: `tests/npm/release-workflow.test.mjs`
- Modify: `docs/operations/npm-distribution.md`
- Modify: `docs/releases/npm-0.1.0.md`

**External writes:** GitHub Environment settings, workflow commit/push, and
five npm Trusted Publisher settings.

- [ ] **Step 1: Obtain Trusted-Publishing-specific authorization.**

Stop until the user approves these exact external changes:

```text
GitHub repository: AIZ-IM/Aizim
GitHub Environment: npm-release
workflow file: npm-release.yml
npm packages: the five public @aiz.im packages from Task 15
publisher repository owner: AIZ-IM
publisher repository name: Aizim
publisher environment: npm-release
```

This approval does not authorize publishing another version or creating
another tag.

- [ ] **Step 2: Change the workflow-contract test first.**

Update `tests/npm/release-workflow.test.mjs` to require:

- tag trigger `v<semver>` plus manual `verify-only` dispatch;
- native builds and aggregate verification remain mandatory and uncached;
- the publish job runs only for a tag;
- the publish job alone has `contents: read` and `id-token: write`;
- the publish job uses protected environment `npm-release`;
- four platform tarballs are published and registry-verified before the meta
  tarball;
- the exact release bundle is reused without rebuilding in the publish job;
- no `NODE_AUTH_TOKEN`, `NPM_TOKEN`, long-lived secret, `--provenance`, or
  credentialed model call appears;
- manual dispatch never reaches a publish command.

Run:

```bash
node --test tests/npm/release-workflow.test.mjs
```

Expected: FAIL because the Task 14 workflow is verification-only.

- [ ] **Step 3: Enable OIDC publication for future tags.**

Modify `.github/workflows/npm-release.yml` so:

1. `workflow_dispatch` supports only `mode=verify-only`, exact version, and
   exact commit SHA;
2. a tag matching the committed semver package version runs the four uncached
   native jobs and aggregate verifier;
3. one `publish` job on `ubuntu-24.04` downloads the verified bundle;
4. that job has `environment: npm-release`, `contents: read`, and
   `id-token: write`;
5. it installs Node from `.node-version` with the pinned setup action;
6. it publishes or verifies the four platform packages in the fixed Task 15
   order, waits for all four exact integrities, then publishes or verifies the
   meta package;
7. it uses npm 12's Trusted Publishing identity automatically and supplies no
   registry token;
8. it omits provenance because npm does not generate it for this private
   source repository.

The existing `v0.1.0` tag is not rerun. The first OIDC publication occurs only
for a separately authorized future version tag.

- [ ] **Step 4: Verify and commit the enabled workflow.**

Run:

```bash
node --test tests/npm/release-workflow.test.mjs \
  tests/npm/release-tools.test.mjs
ruby -e 'require "yaml"; YAML.load_file(".github/workflows/npm-release.yml", aliases: true)'
if rg -n "NODE_AUTH_TOKEN|NPM_TOKEN|--provenance|manual_real_codex" \
  .github/workflows/npm-release.yml; then
  exit 1
fi
git diff --check
git add .github/workflows/npm-release.yml \
  tests/npm/release-workflow.test.mjs \
  docs/operations/npm-distribution.md
git diff --cached --check
git commit -m "Enable npm Trusted Publishing"
git push origin main
```

- [ ] **Step 5: Protect the GitHub release environment.**

In `AIZ-IM/Aizim`, create or update the `npm-release` Environment. Require the
repository owner as reviewer and prohibit self-bypassing where the GitHub plan
supports it. Do not add environment secrets. Verify the workflow is the only
documented publication path.

- [ ] **Step 6: Configure all five npm Trusted Publishers.**

For each public package, open its npm Trusted Publisher settings and enter:

```text
provider: GitHub Actions
organization or user: AIZ-IM
repository: Aizim
workflow filename: npm-release.yml
environment: npm-release
```

Save and reread each package setting before continuing. Do not change package
owners, public access, two-factor policy, or existing versions.

- [ ] **Step 7: Run a non-publishing workflow verification.**

Dispatch the workflow on `main` with `mode=verify-only`, version `0.1.0`, and
the exact current full SHA. Observe all four native jobs and aggregate
verification. Assert from the run graph and logs that the publish job was
skipped and no registry version changed.

Update `docs/releases/npm-0.1.0.md` with the Trusted Publisher configuration
date, workflow commit SHA, and verify-only run ID. Commit and push that
evidence:

```bash
git add docs/releases/npm-0.1.0.md
git diff --cached --check
git commit -m "Record npm Trusted Publisher configuration"
git push origin main
```

Task 16 is complete when all five settings are visible, the protected
environment is active, verify-only CI is green, and no new npm version was
created.

---

## Requirement Coverage

### Specification sections

| Specification requirement | Implemented and observed in |
| --- | --- |
| 1–3 purpose, decisions, goals, non-goals | Global Constraints; Tasks 1–16 |
| 4 user experience | Tasks 3, 6, 12, 14, 15 |
| 5 package topology | Tasks 1–3, 8 |
| 6 repository layout | Tasks 1, 7, 8 |
| 7.1 JavaScript entry point | Tasks 2–3 |
| 7.2 Rust launcher | Tasks 4–6 |
| 7.3 Python runtime | Task 9 |
| 7.4 sandbox adapters | Tasks 10–11 |
| 8 distribution manifests and integrity | Tasks 4, 7–8 |
| 9 runtime provisioning and cache | Tasks 5–6, 12–13 |
| 10 process, signal, and exit behavior | Tasks 3–6, 12 |
| 11 build system | Tasks 1, 7–8, 12–14 |
| 12 testing and qualification | Tasks 1–13 |
| 13 CI design | Task 13 |
| 14 npm release design | Tasks 14–16 |
| 15 security properties | Tasks 1–13, 16 |
| 16 compatibility and migration | Tasks 9, 12, 14 |
| 17 acceptance criteria | Tasks 12–16 and table below |

### Acceptance criteria

| Criterion | Evidence task |
| --- | --- |
| 1. Four-target clean `npm install && npm run build` | Tasks 7, 13 |
| 2. Four-target `npm test` | Tasks 12–13 |
| 3. Exactly two correct tarballs per target | Tasks 8, 12–14 |
| 4. Fresh local tarball install | Tasks 12–13 |
| 5. Fresh temporary-global tarball install | Tasks 12–13 |
| 6. First-run isolated Python 3.14.6 provisioning | Tasks 6, 12–13 |
| 7. Completed-cache reuse | Tasks 5, 12–13 |
| 8. Exact local Codex 0.145.0 | Tasks 2, 9, 12–13 |
| 9. macOS arm64/x64 READY, Gate B, and run | Tasks 10, 12–13 |
| 10. Linux arm64/x64 READY, Gate B, and run | Tasks 11–13 |
| 11. Closed failure behavior | Tasks 2–6, 10–13 |
| 12. Existing uv-native workflows green | Tasks 9–13 |
| 13. Public registry install on four targets | Task 15 |

The Danus worker reference affects lifecycle structure only: Global Constraint
21 and Tasks 5, 9, and 12 preserve centralized path ownership, call-time
environment resolution, explicit evidence, and artifact-safe recovery without
duplicating Aizim's authoritative state.
