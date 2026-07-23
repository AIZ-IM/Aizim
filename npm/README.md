# Aizim for npm

Aizim combines a Python research control plane, a native Rust bootstrap
launcher, Lean 4 verification, and a package-local Codex executable.

## Install

Node.js 22.14.0 or newer is required. The package has no installation
lifecycle script; the verified Python runtime is provisioned on first use.

```sh
npm install --global @aiz.im/aizim
aizim --version
```

The first command invocation downloads the locked CPython runtime and
hash-verified Python dependencies through the bundled uv 0.11.31 binary.
Subsequent invocations reuse the versioned local runtime cache.

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
