# Aizim foundation runbook

This runbook operates the macOS Slices 1–2 engineering-smoke path. It does not establish an
open-problem result, novelty, or general autonomous proving capability.

## Safety boundary

- Treat model output, worker processes, worker views, and model-requested tools as untrusted.
- Never open or edit `.aizim/state.sqlite3` manually during a live run. Use `aizim status`, the
  StateService RPC, and registered artifacts.
- A failed or skipped security probe prohibits autonomous execution. Do not continue to a fake or
  real autonomous run until Gate B passes on the same host and project state.
- Keep `AIZIM_MODEL` in the invoking shell only. Do not put it in `.aizim/config.toml`, an `.env`
  file, a workflow, a report, or an artifact.

## Fixed foundation pins

| Component | Required value |
|---|---|
| Platform | macOS with `/usr/bin/sandbox-exec` |
| Python | 3.12–3.14 |
| uv | 0.11.31 |
| Codex CLI | 0.145.0 |
| Lean | 4.32.1 |
| Lean toolchain | `leanprover/lean4:v4.32.1` |
| lean-lsp-mcp | 0.28.1 |
| leanclient | 0.12.1 |
| MCP | 1.28.1 |
| Free disk floor | 2,147,483,648 bytes (2 GiB) |
| Lake imports | `Std` only; no Mathlib, Batteries, REPL, or Loogle index |

Install `uv` 0.11.31 with the organization's managed Python tooling, install `elan` and Node.js,
then install the two executable pins:

```sh
elan toolchain install leanprover/lean4:v4.32.1
npm install --global @openai/codex@0.145.0
uv sync --frozen
```

Verify rather than assuming the active tools:

```sh
uv --version
codex --version
(cd examples/smoke_lean && lake env lean --version)
(cd examples/smoke_lean && lake --version)
uv run python -c 'import importlib.metadata as m; print(m.version("lean-lsp-mcp"), m.version("leanclient"), m.version("mcp"))'
```

Expected pins are `uv 0.11.31`, `codex-cli 0.145.0`, Lean `4.32.1`, and Python package versions
`0.28.1 0.12.1 1.28.1`.

## Initialize a Lean project

The project must already contain regular `lakefile.toml` and `lean-toolchain` files. Initialization
is idempotent and creates only private state below `.aizim/`:

```sh
uv run aizim init /absolute/path/to/lean-project
```

Do not hand-edit the generated database or runtime lock. A conflicting `.aizim/config.toml` is a
hard initialization failure.

## Prepare separate acceptance copies

Fake and real acceptance runs must not share generated state. Copy only the checked-in smoke
project, excluding `.aizim/` and `.lake/`, into two fresh directories below the private macOS
`${TMPDIR}`. Do not put a canonical project below `/tmp` or `/private/tmp`; the sandbox rejects
their shared parent permissions.

```sh
: "${TMPDIR:?TMPDIR must be the private macOS per-user temporary directory}"
case "${TMPDIR%/}" in
  /tmp|/tmp/*|/private/tmp|/private/tmp/*|/var/tmp|/var/tmp/*|/private/var/tmp|/private/var/tmp/*)
    echo "unsafe TMPDIR" >&2; exit 1;;
esac
AIZIM_FAKE_ROOT="$(mktemp -d "${TMPDIR%/}/aizim-fake.XXXXXX")"
AIZIM_REAL_ROOT="$(mktemp -d "${TMPDIR%/}/aizim-real.XXXXXX")"
rsync -a --exclude '.aizim' --exclude '.lake' examples/smoke_lean/ "$AIZIM_FAKE_ROOT/"
rsync -a --exclude '.aizim' --exclude '.lake' examples/smoke_lean/ "$AIZIM_REAL_ROOT/"
uv run aizim init "$AIZIM_FAKE_ROOT"
uv run aizim init "$AIZIM_REAL_ROOT"
```

## Readiness and Gate B

Run readiness and Gate B on each copy before its autonomous run:

```sh
for AIZIM_PROJECT_ROOT in "$AIZIM_FAKE_ROOT" "$AIZIM_REAL_ROOT"; do
  uv run aizim doctor --project "$AIZIM_PROJECT_ROOT"
  uv run aizim security-probe \
    --project "$AIZIM_PROJECT_ROOT" --backend codex --no-model
done
```

Every doctor must end `READY`. The fixed Gate B success heading is `SECURITY GATE PASS`. Gate B
must deny all nine protected operations, allow the two intended view/scratch operations, preserve
protected hashes and the logical digest, and replay after restart. Any nonzero exit or
`SECURITY GATE FAIL` blocks autonomous execution on that copy.

## Deterministic fake run

The fake backend exercises shared orchestration without a model call:

```sh
uv run aizim run \
  --project "$AIZIM_FAKE_ROOT" \
  --profile autonomous-shared \
  --backend fake
uv run aizim status --project "$AIZIM_FAKE_ROOT" --json
```

Success includes `AIZIM RUN PASS`, knowledge epoch 2, two verified declarations, no live leases,
and a stopped shared Lean runtime. A fake pass is engineering validation, not real-Codex evidence.

## Credentialed real run

Run this gate locally and manually. CI intentionally has no model credential and never invokes a
model API. Gate B, the direct real run, and the checker must target the same fresh real copy, in
that order. Its starting knowledge epoch must be 0.

```sh
test -n "${AIZIM_MODEL:-}"
uv run aizim run \
  --project "$AIZIM_REAL_ROOT" \
  --profile autonomous-shared \
  --backend codex
uv run python scripts/check_foundation.py \
  --project "$AIZIM_REAL_ROOT" \
  --run-id latest-real
```

The last command must print exactly `FOUNDATION ACCEPTANCE PASS 16/16`. Missing, skipped,
inconclusive, mixed-policy, or post-publication evidence fails closed.

That runtime `16/16` covers replayable real-run predicates only and does not independently prove
CI-only or test-only rows of the overall handoff matrix. The full matrix remains binding and is
evidenced separately by the clean automated suite, pinned CI workflows, credentialed manual gate,
and final handoff report. The runtime artifacts never fabricate receipts for external tests.

Run the credentialed pytest E2E separately:

```sh
uv run pytest tests/e2e/test_real_codex_smoke.py \
  -m "manual_real_codex and lean_integration" -q -s
```

That test creates and deletes its own fresh temp copy. Its artifacts are intentionally not the
checker target.

## Status and registered artifacts

Use the CLI for live state:

```sh
uv run aizim status --project "$AIZIM_REAL_ROOT"
uv run aizim status --project "$AIZIM_REAL_ROOT" --json
```

Each completed evaluated run registers exactly these files under
`.aizim/artifacts/RUN_ID/`:

- `run-manifest.json`
- `formal-trace.jsonl`
- `formal-trace.sha256`
- `alignment-review.json`
- `acceptance-report.json`

The manifest records exact tool/runtime policy, the trace is chained to source events, and the
acceptance report carries the engineering-smoke limitation. Treat all five as one evidence set.

## Clean up stopped ephemeral views

Normal completion removes private `aizim-view-*` and `aizim-scratch-*` directories automatically.
After a hard process kill, first confirm `aizim status` reports stopped workers and no live leases,
and confirm no Aizim, Codex, or Lean process owns the candidate directory. List candidates without
deleting them:

```sh
AIZIM_TEMP_ROOT="$(uv run python -c 'import tempfile; print(tempfile.gettempdir())')"
find "$AIZIM_TEMP_ROOT" -maxdepth 1 -type d \
  \( -name 'aizim-view-*' -o -name 'aizim-scratch-*' \) -print
```

Move only an exact, inspected orphan path to Trash. Never use a wildcard deletion and never remove
`.aizim/state.sqlite3`, `.aizim/artifacts/`, or a view still owned by a process.

For crash, lease, runtime, publication, replay, and quarantine handling, continue with the
[event recovery runbook](event-recovery.md).
