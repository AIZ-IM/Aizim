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
