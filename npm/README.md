# Aizim for npm

Aizim combines a Python research control plane, a native Rust bootstrap
launcher, Lean 4 verification, and package-local Codex and Claude executables.

## Install

Node.js 22.22.2 or newer is required. The package has no installation
lifecycle script; the verified Python runtime is provisioned on first use.

```sh
npm install --global @aiz.im/aizim
aizim --version
```

The first command invocation downloads the locked CPython 3.14.6 runtime and
hash-verified Python dependencies through the bundled uv 0.11.31 binary.
Subsequent invocations reuse the versioned local runtime cache.

## Run the foreground controller

```sh
aizim init /absolute/lean/project
aizim controller configure \
  --project /absolute/lean/project \
  --provider codex \
  --model gpt-5.6-sol
aizim worker register \
  --project /absolute/lean/project \
  --worker-id proof-a \
  --role proof_explorer
aizim worker assign \
  --project /absolute/lean/project \
  --worker-id proof-a \
  --task "prove the current Lean target"
AIZIM_MODEL=gpt-5.6-sol aizim controller start \
  --project /absolute/lean/project \
  --foreground
```

Select Claude for controller planning with `--provider claude --model
claude-opus-4-6`. The worker remains Codex-backed and uses `AIZIM_MODEL`; the
controller provider does not change worker authority or tooling.

## Build from source

Source builds use the exact toolchain versions recorded in the repository:
Node.js 26.5.0, npm 12.0.1, Rust 1.97.1, and uv 0.11.31.

```sh
npm install
npm run build
npm run check
npm run pack
```

`npm run build` creates the wheel, locked requirements, Rust launcher, bundled
uv, and strict manifests. `npm run pack` writes verified current-platform
tarballs below `dist/npm/` without publishing them.
