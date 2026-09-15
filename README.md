# Aizim

Aizim is a formal-native autonomous mathematical research system built around Lean 4.
Slices 1–2 establish an executable engineering-smoke path for its authority foundation
and shared formal loop.

The trusted boundary contains the supervisor-owned state, capability gateway, document
broker, shared Lean bridge, verification and promotion services, and attested provider CLI
transports. Model outputs and model-controlled tool calls are untrusted and receive only explicitly
authorized capabilities and materialized worker views.

Slices 1–2 support macOS first. Linux is a future interface adapter and is not an
acceptance platform for these slices.

## Install from npm

The public npm distribution supports macOS arm64 and glibc-based Linux on arm64 and x64. It
requires Node.js 22.22.2 or newer, `rg`, and an external Lean 4 installation managed by `elan`.

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

The first command may download and prepare a managed CPython 3.14.6 runtime in the Aizim cache
through bundled uv 0.11.31. Aizim does not install Codex or Claude. Install Codex CLI 0.154.0
independently for every operational configuration; install Claude Code 2.1.218 only when selecting
a Claude Controller:

```sh
npm install --global @openai/codex@0.154.0
npm install --global --allow-scripts=@anthropic-ai/claude-code \
  @anthropic-ai/claude-code@2.1.218
```

Codex remains the fixed Worker and sandbox runtime. Controller selection is explicit and may use
Codex or Claude. Provider executables are user-owned: Aizim resolves an explicit override before
`PATH`, records the canonical path, exact version, and SHA-256 as trust-on-first-use evidence, and
revalidates that image before each launch. Removing or upgrading the Aizim package does not remove
Lean projects, credentials, or Aizim runtime caches.

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

This source build requires the exact toolchain versions recorded in `.node-version`,
`rust-toolchain.toml`, `package.json`, `pyproject.toml`, and `lean-toolchain`. See the
[npm distribution runbook](docs/operations/npm-distribution.md) for the full three-platform
qualification and release-readiness procedure.

## Persistent controller and workers

For ordinary Lean projects, dependency scheduling, bounded repair rounds, offline lemma search,
research memory, operator guidance, human contribution records, and the local dashboard, use the
[research workflow](docs/operations/research-workflow.md). The
[human-role design](docs/designs/2026-09-15-human-role-in-lean-native-research.md) explains how
formal validity, mathematical meaning, research value, attribution, and exposition are recorded.

Initialize a Lean project, select one primary controller provider, then register workers and
assign versioned tasks:

```sh
aizim init /absolute/path/to/lean-project
aizim controller configure \
  --project /absolute/path/to/lean-project \
  --provider codex \
  --model gpt-6-astra
aizim worker register \
  --project /absolute/path/to/lean-project \
  --worker-id counterexample-a \
  --role counterexample_agent
aizim worker assign \
  --project /absolute/path/to/lean-project \
  --worker-id counterexample-a \
  --task "prove (n : Nat) : n + 0 = n"
AIZIM_MODEL=gpt-6-astra aizim controller start \
  --project /absolute/path/to/lean-project \
  --foreground
aizim controller show --project /absolute/path/to/lean-project --json
aizim worker list --project /absolute/path/to/lean-project --json
```

For a Claude-controlled planning loop, configure the same project with:

```sh
aizim controller configure \
  --project /absolute/path/to/lean-project \
  --provider claude \
  --model claude-opus-4-6
AIZIM_MODEL=gpt-6-astra aizim controller start \
  --project /absolute/path/to/lean-project \
  --foreground
```

The provider selects only the controller. Worker execution remains Codex-backed and reads its
model from `AIZIM_MODEL` or the project configuration. The foreground controller is the sole live
state writer, executes each durable assignment at most once, and records runtime and execution
status for `controller show` and `worker list`. Configuration, registration, assignments, and
terminal execution status survive restarts; terminal assignments are not dispatched again.
Codex CLI 0.154.0 is required for either Controller choice; Claude Code 2.1.218 is additionally
required only for the Claude Controller.

The Codex examples select [GPT-6 Astra](https://learn.chatgpt.com/docs/models) using
`gpt-6-astra`. Set the Controller's `--model` and the Worker's `AIZIM_MODEL` separately;
changing one does not change the other. Other explicitly configured models remain supported.

Slices 1–2 are engineering smoke tests only. They make no open-problem, novelty, or
general proof-capability claim.

## Foundation handoff

The supported acceptance platform is macOS. Prepare fake and real acceptance runs in separate
fresh copies under the private macOS `${TMPDIR}`; never reuse generated `.aizim/` or `.lake/`:

```sh
: "${TMPDIR:?TMPDIR must be the private macOS per-user temporary directory}"
case "${TMPDIR%/}" in
  /tmp|/tmp/*|/private/tmp|/private/tmp/*|/var/tmp|/var/tmp/*|/private/var/tmp|/private/var/tmp/*)
    echo "unsafe TMPDIR" >&2; exit 1;;
esac
AIZIM_REAL_ROOT="$(mktemp -d "${TMPDIR%/}/aizim-real.XXXXXX")"
rsync -a --exclude '.aizim' --exclude '.lake' examples/smoke_lean/ "$AIZIM_REAL_ROOT/"
uv run aizim init "$AIZIM_REAL_ROOT"
uv run aizim controller configure --project "$AIZIM_REAL_ROOT" --provider codex
uv run aizim doctor --project "$AIZIM_REAL_ROOT"
uv run aizim security-probe --project "$AIZIM_REAL_ROOT" --backend codex --no-model
```

The doctor must end `READY` and Gate B must print `SECURITY GATE PASS`. A failed or skipped
security probe prohibits autonomous execution. Never open `.aizim/state.sqlite3` manually during
a live run.

Use the [foundation runbook](docs/operations/foundation-runbook.md) for exact pins, initialization,
fake and real runs, registered artifacts, and cleanup. Use the
[event recovery runbook](docs/operations/event-recovery.md) for crashes, expired leases, stale
contributions, interrupted publication, replay, and quarantine inspection.

## Shared smoke runs

The deterministic path exercises the same shared-runtime orchestration without a model. Use a
different fresh copy from the real gate:

```sh
AIZIM_FAKE_ROOT="$(mktemp -d "${TMPDIR%/}/aizim-fake.XXXXXX")"
rsync -a --exclude '.aizim' --exclude '.lake' examples/smoke_lean/ "$AIZIM_FAKE_ROOT/"
uv run aizim init "$AIZIM_FAKE_ROOT"
uv run aizim controller configure --project "$AIZIM_FAKE_ROOT" --provider codex
uv run aizim security-probe --project "$AIZIM_FAKE_ROOT" --backend codex --no-model
uv run aizim run --project "$AIZIM_FAKE_ROOT" --profile autonomous-shared --backend fake
```

The hardened real-Codex path is a manual, credential-gated gate. Supply a model through
`AIZIM_MODEL` in your own shell. Gate B, the direct run, and the checker must target the same fresh
real copy, in that order:

```sh
test -n "${AIZIM_MODEL:-}"
uv run aizim run --project "$AIZIM_REAL_ROOT" --profile autonomous-shared --backend codex
uv run python scripts/check_foundation.py \
  --project "$AIZIM_REAL_ROOT" --run-id latest-real
```

The runtime `16/16` covers replayable real-run predicates only and does not independently prove
CI-only or test-only rows of the overall handoff matrix. The full matrix remains binding and is
closed separately by the pinned workflows, automated suite, credentialed manual gate, and final
handoff report.

The credentialed pytest E2E is a separate manual gate. It creates and deletes its own temp copy, so
its artifacts are intentionally not the checker target:

```sh
uv run pytest tests/e2e/test_real_codex_smoke.py \
  -m "manual_real_codex and lean_integration" -q -s
```

It uses isolated read-only worker views, a capability-gated gateway, a shared Lean runtime,
and a separate machine alignment review. It records a manifest, replayable formal trace, and
acceptance report below `.aizim/artifacts/RUN_ID/`. This result is an engineering smoke test
and is not evidence of open-problem, novelty, or general autonomous proving capability.
