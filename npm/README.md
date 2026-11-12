# Aizim for npm

Aizim combines an independent Python research supervisor, a native Rust bootstrap launcher,
omp RPC, the magpie local gateway, and Lean 4 verification.

## Install

Node.js 22.22.2 or newer, `rg`, a Lean toolchain managed by `elan`, omp 18.18.2, and
magpie 0.1.3338 are required. The package has no installation lifecycle script; the verified
Python runtime is provisioned on first use. Linux requires `bwrap` and unprivileged user
namespaces; macOS requires `/usr/bin/sandbox-exec`.

```sh
npm install --global @aiz.im/aizim
aizim --version
```

The first command invocation downloads the locked CPython 3.15.2 runtime and
hash-verified Python dependencies through the bundled uv 0.13.7 binary.
Subsequent invocations reuse the versioned local runtime cache.

Install omp and magpie independently and configure magpie with a responding local gateway and an
available model. Magpie owns model selection and provider credentials. Aizim does not install
these services or change their configuration. Run `aizim doctor --project PROJECT` to check
readiness before starting research.

## Run research

```sh
aizim init /absolute/lean/project
aizim doctor --project /absolute/lean/project
aizim research task add --project /absolute/lean/project \
  --task-id smoke-true --title 'Prove True' --statement True \
  --max-rounds 2 --max-failures 1 --timeout-seconds 120
aizim research run --project /absolute/lean/project --max-seconds 300
aizim research task list --project /absolute/lean/project --json
```

The supervisor runs its security gate before scheduling. Research can make model requests
through magpie; only trusted Lean publication can verify a target. Upgrades preserve project
state, credentials, and runtime caches, and historical execution records keep their identities.

## Build from source

Source builds use the exact toolchain versions recorded in the repository:
Node.js 26.13.0, npm 12.2.2, Rust 1.99.1, and uv 0.13.7.

```sh
npm install
npm run build
npm run check
npm run pack
```

`npm run build` creates the wheel, locked requirements, Rust launcher, bundled
uv, and strict manifests. `npm run pack` writes verified current-platform
tarballs below `dist/npm/` without publishing them.

Installation smoke checks resolve npm and npx from the build environment's `PATH`
and run them with the current Node.js executable. Node.js and npm may be installed
in separate directories. Consumer processes retain their private home, cache, and
restricted `PATH`.
