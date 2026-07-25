# Aizim for npm

Aizim combines a Python research control plane, a native Rust bootstrap
launcher, and Lean 4 verification. It does not install Codex or Claude.

## Install

Node.js 22.22.2 or newer, `rg`, and a Lean toolchain managed by `elan` are
required. The package has no installation lifecycle script; the verified
Python runtime is provisioned on first use.

```sh
npm install --global @aiz.im/aizim
aizim --version
```

The first command invocation downloads the locked CPython 3.14.6 runtime and
hash-verified Python dependencies through the bundled uv 0.11.31 binary.
Subsequent invocations reuse the versioned local runtime cache.

Install the user-owned agent CLIs independently. Codex CLI 0.145.0 is always
required for Worker and sandbox readiness. Claude Code 2.1.218 is required
only when `claude` is the selected Controller.

```sh
npm install --global @openai/codex@0.145.0
npm install --global --allow-scripts=@anthropic-ai/claude-code \
  @anthropic-ai/claude-code@2.1.218
```

`AIZIM_CODEX_EXECUTABLE` takes precedence over `codex` on `PATH`;
`AIZIM_CLAUDE_EXECUTABLE` independently takes precedence over `claude` on
`PATH`. Aizim records each canonical path, exact version, and SHA-256 as
trust-on-first-use evidence for user-owned provider provenance, then
revalidates it before each launch. A version error reports `observed=...` and
`supported=...`; install a supported side-by-side CLI and point the matching
override at its absolute path.

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

Upgrading from an older bundled-agent package preserves project state and
credentials, but the new package does not use the old nested agent packages.
Expose a supported external Codex, and Claude when selected, before starting
the Controller.

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
