# Aizim npm distribution runbook

This runbook covers building, testing, packaging, and qualifying the Aizim npm distribution. None
of the commands or workflows in this document publishes to npm. Public registry mutation remains
a separate, explicitly authorized operation.

## Supported consumers

The distribution supports:

| npm target | Host |
| --- | --- |
| `darwin-arm64` | macOS arm64 |
| `linux-arm64` | Linux arm64 with glibc |
| `linux-x64` | Linux x64 with glibc |

Consumers need Node.js 22.22.2 or newer, `rg`, and an external Lean toolchain managed by `elan`.
The npm package supplies uv 0.11.31 and a managed CPython 3.14.6 runtime.
It does not install Codex or Claude.
Codex CLI 0.154.0 is always required for Worker and sandbox readiness. Claude Code 2.1.218 is
required only when `claude` is the selected Controller. musl Linux and Windows are unsupported.
Intel macOS is unsupported; macOS packages target Apple silicon only.
Linux hosts must permit unprivileged user namespaces for the supported external Codex
bubblewrap sandbox; on Ubuntu 24.04, grant that permission with an AppArmor profile for the
installed executable.

## Install as a consumer

Global installation:

```sh
npm install --global @aiz.im/aizim
aizim --version
```

Project-local installation:

```sh
npm install --save-dev @aiz.im/aizim
npx --no-install aizim --version
```

The first invocation may download and assemble CPython 3.14.6 below the Aizim runtime cache. A
successful second invocation reuses the same verified `READY.json` record. Uninstalling the npm
package never removes a Lean project or the runtime cache.

## Install and resolve external agents

Install the exact supported Codex independently. Install Claude only for a Claude Controller:

```sh
npm install --global @openai/codex@0.154.0
npm install --global --allow-scripts=@anthropic-ai/claude-code \
  @anthropic-ai/claude-code@2.1.218
```

Resolution is independent by role and has no autodetection of the Controller provider:

- Codex resolves `AIZIM_CODEX_EXECUTABLE` before `codex` on `PATH`.
- Claude resolves `AIZIM_CLAUDE_EXECUTABLE` before `claude` on `PATH`.
- An empty override falls through to `PATH`; a non-empty override must be an absolute executable
  path and never falls back after an error.

The external executable is outside Aizim's distribution-signing boundary. At each Controller
start, Aizim establishes trust on first use by recording the canonical path, exact version, and
SHA-256. It revalidates all three before every provider launch. This detects replacement during
the operation, but it does not attest user-owned provider provenance. Users and their package
manager remain responsible for that provenance.

Version compatibility is exact: `codex --version` must print `codex-cli 0.154.0`, and
`claude --version` must print `2.1.218 (Claude Code)`. A rejection reports
`observed=...; supported=...` and names the matching override. Install a supported side-by-side
CLI and set that override to its absolute path instead of downgrading an unrelated default CLI.

When upgrading a project initialized with Codex CLI 0.145.0, stop its Controller before editing
the `[foundation]` section of `.aizim/config.toml` to set `codex_cli_version = "0.154.0"`.
Then select `gpt-6-astra` through `controller configure --provider codex --model gpt-6-astra`
and `AIZIM_MODEL=gpt-6-astra` for Workers. Run `doctor` and the no-model `security-probe` again
before autonomous execution with the upgraded CLI. Keep existing state and historical run
artifacts intact; they record the version used for those runs.

Upgrading from a release that bundled agent packages preserves project state and credentials.
The new package removes its old dependency edges and does not use the old nested agent packages.
Before starting a Controller after upgrade, expose a supported external Codex and, for a Claude
Controller, a supported external Claude. Aizim does not delete `~/.codex/`, `~/.claude/`, project
state, or runtime caches.

## Build the current host from source

Use the exact stable versions pinned by `.node-version`, `package.json`, `rust-toolchain.toml`,
`pyproject.toml`, and `lean-toolchain`.

```sh
npm install --ignore-scripts
npm run build
npm test
npm run pack
```

`npm run build` produces one host platform package and one platform-neutral meta package.
`npm test` runs Node, Rust, Python, package, and real tarball installation gates. `npm run pack`
writes both tarballs and `dist/npm/pack-summary.json`. No lifecycle hook performs a build or
downloads a runtime during `npm install`.

To inspect a source checkout without building:

```sh
npm run test:node
```

Python-only development remains:

```sh
uv sync --frozen
uv run pytest -m "not manual_real_codex and not manual_real_controller" -q
```

## Install freshly packed tarballs

After `npm run pack`, select the current-host tarball and the meta tarball from
`dist/npm/pack-summary.json`. Install both into a private temporary consumer:

```sh
AIZIM_NPM_CONSUMER="$(mktemp -d "${TMPDIR%/}/aizim-consumer.XXXXXX")"
chmod 700 "$AIZIM_NPM_CONSUMER"
cd "$AIZIM_NPM_CONSUMER"
npm init --yes
npm install --ignore-scripts \
  /absolute/path/to/dist/npm/aiz.im-aizim-<target>-<version>.tgz \
  /absolute/path/to/dist/npm/aiz.im-aizim-<version>.tgz
npx --no-install aizim --version
```

For a global-prefix smoke test without changing the user npm prefix:

```sh
AIZIM_NPM_PREFIX="$(mktemp -d "${TMPDIR%/}/aizim-prefix.XXXXXX")"
chmod 700 "$AIZIM_NPM_PREFIX"
npm install --global --ignore-scripts --prefix "$AIZIM_NPM_PREFIX" \
  /absolute/path/to/dist/npm/aiz.im-aizim-<target>-<version>.tgz \
  /absolute/path/to/dist/npm/aiz.im-aizim-<version>.tgz
"$AIZIM_NPM_PREFIX/bin/aizim" --version
```

Use `node scripts/npm/install-smoke.mjs` for the automated provider-free local/global install,
version/help, expected role-aware doctor failure, cache reuse, deterministic run, deterministic
controller-loop, bundled-agent upgrade, missing-platform, and corrupt-integrity scenarios. The
upgrade path proves project-state and credential-sentinel digests stay byte-identical. The
controller smoke runs `scripts/qa/controller_smoke.py` with the provisioned runtime's own
`venv/bin/python`; imports therefore come from the wheel installed in that runtime, not repository
uv, a repository virtual environment, or global Python.

## Operate the foreground controller

The npm and source commands have the same public control surface:

```sh
aizim init /absolute/lean/project
aizim controller configure \
  --project /absolute/lean/project \
  --provider codex \
  --model gpt-6-astra
aizim worker register \
  --project /absolute/lean/project \
  --worker-id proof-a \
  --role proof_explorer
aizim worker assign \
  --project /absolute/lean/project \
  --worker-id proof-a \
  --task "prove the current Lean target"
AIZIM_MODEL=gpt-6-astra aizim controller start \
  --project /absolute/lean/project \
  --foreground
```

For Claude planning, repeat configuration with `--provider claude --model
claude-opus-4-6`, then start with the same `AIZIM_MODEL` worker setting. The provider changes only
the controller backend; workers remain Codex-backed. `controller show` exposes the validated
runtime state, and `worker list` exposes the current assignment's validated execution state.
Codex is always required for Worker and sandbox readiness. Claude is required only when `claude`
is the selected Controller.

## Stable failure codes

| Exit code | Meaning |
| --- | --- |
| 3 | Role readiness failed; inspect `doctor --json` |
| 64 | CLI usage error |
| 69 | Required child service unavailable |
| 70 | Internal software failure |
| 74 | Distribution or cached artifact integrity failure |
| 78 | Unsupported host, missing platform package, or invalid configuration |

An Aizim artifact integrity or platform failure is fail-closed. Do not bypass it with a different
Python or uv. Provider compatibility failures are also fail-closed; use a supported side-by-side
CLI through the explicit override rather than changing Aizim-owned manifests.

## Three-platform evidence

`.github/workflows/ci.yml` builds the exact three targets without restored npm, Cargo, uv, Python,
or build caches. Every target runs source build, full tests, pack, local/global tarball
installation with no provider on `PATH`, managed Python preparation, cache reuse, expected
role-aware doctor failure, deterministic fake execution, deterministic controller restart
behavior, bundled-agent upgrade preservation, and negative integrity/platform checks. A separate
Node 22.22.2 job verifies the minimum runtime.

Each target emits a path-free native evidence document bound to the full Git SHA, package version,
runner ABI, tarball size, SHA-256, npm integrity, clean tracked inputs, and no restored build
cache. The aggregate job rejects a missing or duplicate target, mismatched meta package, stale
commit, modified tarball, or false check.

## Release candidate workflow

`.github/workflows/npm-release.yml` is manual-only. Supply an exact semantic version and full
40-character Git SHA. It checks out that commit on all three runners, performs a fresh
build/test/pack/install qualification, merges exactly three platform tarballs and one common meta
tarball, and runs `verify-release-bundle.mjs`.

The workflow has only `contents: read`, uses immutable action SHAs, restores no build caches,
receives no npm or model credential, and contains no publish job. Its bundle is retained for 14
days.

`.github/workflows/npm-registry-smoke.yml` is also manual-only and read-only. After a separately
authorized publication, it installs one exact public version on all three targets without agent
packages and requires version/help success, the expected doctor failure, cache reuse, a Lean
build, and `AIZIM RUN PASS`. It never writes to the registry.

## First-publication procedure

First publication is a separate external-write operation. Before requesting authorization:

1. Require green CI at the exact release SHA.
2. Run the manual release-candidate workflow for the same SHA and version.
3. Download and independently verify its aggregate and all four tarballs.
4. Confirm the npm scope owner and package names:
   `@aiz.im/aizim`, `@aiz.im/aizim-darwin-arm64`,
   `@aiz.im/aizim-linux-arm64`, and `@aiz.im/aizim-linux-x64`.
5. Obtain explicit authorization naming the version, SHA, tag, four packages, and public access.

Publish platform packages before the meta package. After each write, query the exact version and
`dist.integrity` with `registry-verify.mjs`. Publish the meta package only after all three platform
receipts match the verified bundle. Then run the registry-smoke workflow.

## Partial-publication recovery

npm versions are immutable. Never overwrite, unpublish, or reuse a version during recovery.

- If no package exists, fix the release process and restart only after new authorization.
- If some platform packages exist and their integrities match the verified bundle, publish only
  the missing platform packages under the same authorization.
- If any existing integrity differs, stop. Do not publish the meta package. Prepare a new patch
  version, rebuild all targets, and obtain new authorization.
- If all platform packages match but the meta package is absent, publish only the verified meta
  package after confirming the three receipts again.
- If the meta package exists but a platform package is missing or mismatched, treat that version
  as failed, document it, and prepare a new patch version.

`registry-verify.mjs --wait` retries only a registry 404, at most 12 attempts with five seconds
between attempts. A version or integrity mismatch never retries.

## Trusted Publishing

Configure npm Trusted Publishing only after the GitHub repository, npm organization, workflow
filename, and release environment are final. Bind the npm packages to the exact
`AIZ-IM/Aizim` workflow identity, grant `id-token: write` only to the future dedicated publish
job, and keep build/test jobs at `contents: read`.

Trusted Publishing configuration and a future credential-free `npm publish --provenance` job are
outside this runbook's implemented workflows. Adding either is a separate security review and
requires explicit publication authorization. Never add `NODE_AUTH_TOKEN`, `NPM_TOKEN`, a
long-lived npm token, or a model credential to build, test, release-candidate, or registry-smoke
jobs.
