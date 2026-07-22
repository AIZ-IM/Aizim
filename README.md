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

## Shared smoke runs

The deterministic path exercises the same shared-runtime orchestration without a model:

```sh
uv run aizim run --project examples/smoke_lean --profile autonomous-shared --backend fake
```

The hardened real-Codex path is a manual, credential-gated gate. Supply a model through
`AIZIM_MODEL` in your own shell, then run:

```sh
test -n "${AIZIM_MODEL:-}"
uv run pytest tests/e2e/test_real_codex_smoke.py -m "manual_real_codex and lean_integration" -q -s
```

It uses isolated read-only worker views, a capability-gated gateway, a shared Lean runtime,
and a separate machine alignment review. It records a manifest, replayable formal trace, and
acceptance report below `.aizim/artifacts/RUN_ID/`. This result is an engineering smoke test
and is not evidence of open-problem, novelty, or general autonomous proving capability.
