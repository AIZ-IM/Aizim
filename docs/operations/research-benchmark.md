# Research benchmark ladder

The packaged `research-ladder-v1` fixture runs seven immutable targets under Lean 4.35.0:
right and left addition identities, associativity, reuse of a published identity, a project
increment definition, a project composition lemma using the published increment lemma, and a
project theorem combining the published composition and identity lemmas. The fixture includes
its own small Lake project and scripted proof bodies. It has no Mathlib or external Lake
package dependency. Changes to the targets, project sources, dependencies, or scripted proofs
require a new ladder version; the complete fixture is also content-hashed.

Each invocation creates a private `benchmark-*` child under the output directory, initializes
its `lean-project`, and writes `metrics.json` beside it. Existing output files, prior runs, and
user Lean projects are preserved. There is deliberately no `--project` argument. Keep the run
directory if you need the SQLite event history, submissions, published sources, or a normal
research export for investigation. CI uploads only the metrics JSON.

## Deterministic execution with actual Lean

Prerequisites are the repository's pinned Python dependencies, `uv` 0.13.7, and an installed
`elan` with Lean `leanprover/lean4:v4.35.0`. The toolchain's `lake` and `lean` must be reachable
through `PATH`. No omp, magpie service, provider account, or model secret is needed.

From the repository root:

```sh
uv sync --frozen
elan toolchain install leanprover/lean4:v4.35.0
uv run python -m aizim.research.benchmark \
  --backend scripted \
  --output-dir /tmp/aizim-benchmarks
```

The command prints the exact metrics path and verified-target count. Each target goes through
`WorkerExecutionHost`, `LeanExecutor`, and the ordinary worker capability gateway. The scripted
`AgentBackend` calls `lean.goal`, `lean.multi_attempt`, `document.apply`,
`contribution.submit`, and `knowledge.read`. A dependent proof substitutes the previously
published declaration's actual name and imports its actual module supplied by `LeanExecutor`.
The promotion service checks Lean and binds the publication to the target and worker. The
benchmark does not manufacture publication events or use an executor returning assumed success.
Metrics independently use the same trusted target/publication binding to count verified targets.

Successful default scripted execution verifies all seven targets in seven charged rounds and
35 charged gateway calls. The scripts make no model requests; their model token usage and
provider cost remain unknown, because there is no measured provider usage to report.

Exit status is `0` when every target verifies, `1` when the ladder finishes with unverified
targets, and `2` for invalid options, infrastructure failure, or the overall time limit. Runtime
startup failures still produce metrics with a stable failure code or exception class, without raw
exception text. Invalid arguments are rejected before creating a project. Filesystem failure
preventing creation of the output directory or metrics file cannot produce an artifact.

## On-demand real omp execution

Install omp 18.18.2 and magpie 0.1.3338 separately, select a responding model through the
existing omp/magpie configuration, and start the local magpie service. Linux also needs a
working system `bwrap` with user namespaces; macOS uses the system sandbox. Follow the setup
in [research-workflow.md](research-workflow.md). Before starting any model worker, the benchmark
runs the same security gate required by `research run`; failure records `SECURITY_GATE_FAILED`
and prevents execution. It then preflights the supported omp RPC/model configuration and uses
the ordinary restricted omp backend and host tools. Scripted execution skips the model security
gate and has no omp or magpie dependency.

```sh
uv run python -m aizim.research.benchmark \
  --backend omp \
  --output-dir /tmp/aizim-real-benchmarks \
  --workers 1 \
  --max-rounds 2 \
  --timeout-seconds 300 \
  --call-budget 400 \
  --max-seconds 1800
```

Model selection and credentials stay in omp/magpie. The real backend receives the immutable
statement and verified dependency facts, but receives no scripted proof. Its prose or successful
RPC completion cannot verify a target. Actual provider tokens are recorded when omp supplies
them; missing usage remains unknown. Add `--prices /absolute/path/prices.json` to estimate
cost using the same price format as `research export --prices`. For example, an operator price
table has this shape (replace the illustrative model and rates with the intended configuration):

```json
{
  "magpie/example": {
    "input_per_million": "2.00",
    "cached_input_per_million": "1.00",
    "output_per_million": "4.00"
  }
}
```

## Comparing metrics

Every metrics JSON includes the metrics schema version, ladder version/hash, configuration/hash,
run ID, generated timestamp, exit reason, wall seconds, per-target records, and aggregate values.
The configuration identifies the backend, resolved model, backend version/executable hash when
available, worker count, round/call/time limits, pinned Lean/MCP versions, Aizim version,
implementation hash, and supplied prices. The implementation hash covers relative paths and
contents of all non-hidden files in the installed `aizim` package, including Python modules,
Lean sources, SQL, prompts, and other bundled resources. Bytecode and `__pycache__` are excluded.
Runtime edits, additions, removals, and renames therefore change the fingerprint, including
changes outside the research package; moving an unchanged installation does not.
The configuration hash excludes run IDs, absolute paths, timestamps, and measured results.
Compare runs with the same ladder hash and examine configuration changes; remote magpie routing
and provider behavior can still change behind a model alias. The hash identifies the recorded
local configuration, not every upstream provider setting.

- `verify_rate` divides trusted verified targets by all seven ladder targets, including targets
  blocked on dependencies or not started after an outage.
- `charged_rounds_per_verified_target` divides all charged rounds, including unsuccessful proof
  work, by the number of verified targets. Infrastructure and interrupted attempts do not become
  charged rounds. The monotonic attempt sequence remains separate.
- `charged_calls_per_verified_target` divides measured gateway budget charges across all attempts
  by verified targets. This includes calls made during infrastructure attempts. Retryable rate
  denials and already-exhausted budget denials do not consume the gateway budget. `gateway_requests`
  also counts those requests. Lost replies produce `unknown_call_outcomes`, a known subtotal,
  and a null total charged-call count instead of an invented value.
- `infrastructure_failure_rate` divides infrastructure-failed attempts by finished attempts.
  Startup/cleanup failures are also exposed through `run_infrastructure_failure` and the run reason;
  an outage before any attempt has a null attempt-level failure rate.
- Worker and recorded controller token counts are grouped by model. `known_tokens` and
  `known_cost_usd` are measured subtotals. Missing stages make `total_tokens` and the total
  `estimated_cost_usd` unknown. Missing prices also keep the total cost unknown. Explicitly measured
  zero tokens can cost zero; missing measurements cannot.
- Ratios with zero verified targets or zero finished attempts are JSON `null`. `wall_seconds`
  measures initialization, prerequisite checks, Lean startup, execution, publication settlement,
  host cleanup, and state shutdown. It is a measurement, not a deterministic value.

## Nightly and dispatched CI

[research-benchmark.yml](../../.github/workflows/research-benchmark.yml) runs at 03:17 UTC daily
on `macos-15`. It installs the pinned Lean toolchain using the same pinned action as the existing
Lean integration workflow, installs locked Python dependencies, executes the actual scripted
ladder, and uploads metrics for 30 days even when the benchmark exits unsuccessfully.
Nightly execution has no model credentials or secret references.

The regular Lean integration workflow also runs the benchmark evidence regression on pushes
and pull requests, checking publications, dependency reuse, and recorded metrics.

Dispatch the same deterministic job manually:

```sh
gh workflow run research-benchmark.yml
```

To also execute a real model:

```sh
gh workflow run research-benchmark.yml -F real_model=true
```

The optional job requires a self-hosted runner labelled `aizim-research`, with supported Linux
or macOS, the supported omp/magpie versions, a responding selected model, and the platform
sandbox prerequisites already configured for the runner account. It installs Lean and locked
Python dependencies itself. Model setup belongs to that runner, and no provider secret is
copied into the workflow. Without that runner, dispatching `real_model=true` waits for a runner;
ordinary nightly and deterministic dispatch execution use GitHub-hosted macOS and are unaffected.

## Focused checks

The integrating maintainer can run these after all related execution changes have landed:

```sh
uv run pytest tests/unit/test_research_benchmark.py -q
uv run pytest tests/integration/test_research_benchmark_lean.py -m lean_integration -q
uv run ruff check src/aizim/research/benchmark*.py \
  tests/unit/test_research_benchmark.py tests/integration/test_research_benchmark_lean.py
```

The Lean integration test requires the external pinned toolchain. It checks actual promotion
publications, immutable statements, dependency metadata, event replay, round/call metrics, and
unknown provider cost; the unit tests cover aggregation, unknowns, denied-call charging,
isolation, dependency/target boundaries, credential-free scripted startup failure, real-backend
security rejection, and the first-publication project-epoch invariant.
