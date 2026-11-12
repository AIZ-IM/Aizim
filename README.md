# Aizim

Aizim is a local formal mathematical research supervisor built around Lean 4, omp, and magpie.
It schedules bounded attempts, keeps research records, and uses the trusted Lean publication
path to verify targets. Model output and agent tool calls are untrusted; the supervisor mediates
access through its capability gateway, document leases, and promotion services.

## Install from npm

The public npm distribution supports macOS arm64 and glibc-based Linux on arm64 and x64. It
requires Node.js 22.22.2 or newer and `rg`. Actual research also requires an external Lean 4.35.0
installation managed by `elan`.

Install the command globally:

```sh
npm install --global @aiz.im/aizim
aizim --version
```

Or install it in one project:

```sh
npm install --save-dev @aiz.im/aizim
npx --no-install aizim --version
```

The first command may download and prepare a managed CPython 3.15.2 runtime in the Aizim cache
through bundled uv 0.13.7. Install omp 18.18.2 and magpie 0.1.3338 separately, configure the
magpie local gateway, and select its model in the omp configuration. Aizim does not store provider
credentials. Removing or upgrading the Aizim package does not remove Lean projects or runtime caches.
The external Lean host is currently research-only and does not participate in npm release
qualification.

## Build from source

For Python development:

```sh
uv sync --frozen
uv run aizim --version
uv run python -m aizim --version
uv run pytest tests/unit/test_package.py -q
```

For the complete current-host npm distribution, including the Rust launcher, Python wheel, and
bundled uv:

```sh
npm install --ignore-scripts
npm run build
npm test
npm run pack
```

Source builds use Node.js 26.13.0, npm 12.2.2, Rust 1.99.1, Python 3.15.2, and uv 0.13.7, as
recorded in `.node-version`, `package.json`, `rust-toolchain.toml`, `.python-version`, and
`pyproject.toml`. The npm build itself does not require Lean. See the
[npm distribution runbook](docs/operations/npm-distribution.md) for the full three-platform
qualification and release-readiness procedure.

## Research workflow

Initialize a Lean project after installing Lean 4.35.0, omp, and magpie. Select a model through
your magpie-backed omp configuration, then check readiness and define a target:

```sh
aizim init /absolute/path/to/lean-project
aizim doctor --project /absolute/path/to/lean-project
aizim research task add --project /absolute/path/to/lean-project \
  --task-id addition --title 'Addition identity' \
  --statement '(n : Nat) : n + 0 = n' --import Std
aizim research run --project /absolute/path/to/lean-project --max-seconds 1800
aizim research task list --project /absolute/path/to/lean-project --json
aizim research dashboard --project /absolute/path/to/lean-project
```

Each attempt starts a restricted omp RPC process. magpie owns model selection and credentials;
the research supervisor retains scheduling, recovery, leases, and the Lean publication authority.
Only a published declaration bound to the target statement and its candidate can verify a target.
Human reviews and agent summaries remain separate from formal evidence.

The [research workflow](docs/operations/research-workflow.md) covers dependencies, bounded repair,
search, guidance, reviews, and export. The [architecture contract](docs/designs/2026-11-08-omp-magpie-research-architecture.md)
describes the execution and trust boundaries. The [event recovery runbook](docs/operations/event-recovery.md)
covers interrupted publication and replay.

The [benchmark runbook](docs/operations/research-benchmark.md) describes the versioned Lean
ladder, nightly deterministic runs, on-demand model runs, and comparable per-configuration metrics.
See the [capability roadmap](docs/plans/2026-11-09-research-capability-roadmap.md) for implemented
execution reliability and planned research capabilities.
