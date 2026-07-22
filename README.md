# Aizim

Aizim is a formal-native autonomous mathematical research system built around Lean 4.
Slices 1–2 establish an executable engineering-smoke path for its authority foundation
and shared formal loop.

The trusted boundary contains the supervisor-owned state, capability gateway, document
broker, shared Lean bridge, and verification and promotion services. Agent processes,
model outputs, and model-controlled tool calls are untrusted and receive only explicitly
authorized capabilities and materialized worker views.

Slices 1–2 support macOS first. Linux is a future interface adapter and is not an
acceptance platform for these slices.

## Quick start

```sh
uv sync --frozen
uv run aizim --version
uv run python -m aizim --version
uv run pytest tests/unit/test_package.py -q
```

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
