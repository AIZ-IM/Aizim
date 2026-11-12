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

Consumers need Node.js 22.22.2 or newer and `rg`; research additionally requires an external Lean
4.35.0 toolchain managed by `elan`, omp 18.18.2, and magpie 0.1.3338. The npm package supplies uv
0.13.7 and a managed CPython 3.15.2 runtime; it does not install or configure omp, magpie, or Lean.
The external Lean host is currently research-only: it is not bundled and does not participate in
npm build, pack, installation, or release qualification. musl Linux and Windows are unsupported.
Intel macOS is unsupported; macOS packages target Apple silicon only. Linux requires `bwrap` and
unprivileged user namespaces; macOS requires `/usr/bin/sandbox-exec`.

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

The first invocation may download and assemble CPython 3.15.2 below the Aizim runtime cache. A
successful second invocation reuses the same verified `READY.json` record. Uninstalling the npm
package never removes a Lean project or the runtime cache.

## Configure external research services

Install omp 18.18.2 and magpie 0.1.3338 independently, and configure magpie with a responding
local gateway and an available model. Model selection, provider accounts, and credentials belong
to magpie. Aizim does not install these tools, modify their configuration, or copy credentials.
Run `aizim doctor --project PROJECT` to check readiness, including omp RPC, magpie, the selected
model, the restricted host-tool transport, and Lean prerequisites. Fix reported failures before
starting research.

Upgrades preserve existing project state, credentials, and runtime caches. Historical events and
artifacts retain the execution identities recorded at the time; they are not evidence of a new
omp/magpie run.

## Build the current host from source

Use Node.js 26.13.0 with npm 12.2.2, Rust 1.99.1, Python 3.15.2, and uv 0.13.7, as pinned by
`.node-version`, `package.json`, `rust-toolchain.toml`, `.python-version`, and `pyproject.toml`.
The npm source build does not require a Lean host.

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
uv run pytest -m "not lean_integration and not linux_sandbox and not macos_sandbox" -q
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

Use `node scripts/npm/install-smoke.mjs` for the automated local/global install, version/help,
expected missing-dependency doctor failure, research task add/list roundtrip, cache reuse,
bundled-agent upgrade, missing-platform, and corrupt-integrity scenarios. The upgrade path checks
that project-state and credential-sentinel digests remain byte-identical. These checks do not run
a model or prove a Lean theorem.

## Run research

Initialize a Lean project, check readiness, add a target, and run the bounded supervisor:

```sh
aizim init /absolute/lean/project
aizim doctor --project /absolute/lean/project
aizim research task add --project /absolute/lean/project \
  --task-id smoke-true --title 'Prove True' --statement True \
  --max-rounds 2 --max-failures 1 --timeout-seconds 120
aizim research run --project /absolute/lean/project --max-seconds 300
aizim research task list --project /absolute/lean/project --json
```

Research execution runs the existing security gate before scheduling and can make model requests
through magpie. Only trusted Lean publication can verify a target; a completed model response is
not proof.

## Stable failure codes

| Exit code | Meaning |
| --- | --- |
| 3 | Readiness failed; inspect `doctor --json` |
| 64 | CLI usage error |
| 69 | Required child service unavailable |
| 70 | Internal software failure |
| 74 | Distribution or cached artifact integrity failure |
| 78 | Unsupported host, missing platform package, or invalid configuration |

An Aizim artifact integrity or platform failure is fail-closed. Do not bypass it with a different
Python or uv. External service readiness failures also block research until the reported
prerequisites are repaired.

## Three-platform evidence

`.github/workflows/ci.yml` builds the exact three targets without restored npm, Cargo, uv, Python,
or build caches. Each target runs source build, tests, pack, local/global tarball installation,
managed Python preparation, cache reuse, a missing-dependency doctor failure, a research task
roundtrip, bundled-agent upgrade preservation, and negative integrity/platform checks. A separate
Node 22.22.2 job verifies the minimum runtime. Distribution smoke does not run a model or Lean.
Lean integration and real research evidence are separate from this release bundle.
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
authorized publication, it installs one exact public version on all three targets and checks
version/help, the expected doctor failure, cache reuse, and a research task roundtrip. It never
writes to the registry.

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
