# Aizim foundation runbook

The autonomous workflow is `aizim research …`, using one restricted omp RPC process per attempt
and the local magpie gateway. This runbook distinguishes current operational checks from legacy
Codex foundation evidence. Neither an engineering smoke test nor a model response establishes an
open-problem result, novelty, or general autonomous proving capability.

## Safety boundary and prerequisites

Treat model output, RPC events, views, and tool arguments as untrusted. Never edit
`.aizim/state.sqlite3` manually during a live run; use the CLI, StateService RPC, and registered
artifacts. A failed or skipped required security control blocks autonomous execution.

Install omp 18.18.2 and magpie 0.1.3338, and configure the local magpie service with an available
model. The omp version pin has one source: `aizim.omp.runtime.OMP_VERSION` (currently `18.18.2`).
Observed harness identity is recorded as `omp/18.18.2`, with the canonical executable hash; the
model backend is `magpie`. Model selection and provider credentials belong to magpie. Do not set
`AIZIM_MODEL` or copy provider credentials into Aizim config, prompts, artifacts, or reports.

Linux requires a working system `bwrap` and user namespaces. macOS requires Apple's
`/usr/bin/sandbox-exec`. Both use an explicit read-only materialized view and writable scratch;
canonical project/state and gateway Unix socket access are restricted. An unavailable Linux user
namespace fails closed. A filtered minimal environment excludes provider credentials, and isolated
per-attempt configuration under scratch rejects ambient plugins, extensions, skills, rules, and
MCP. The host network is shared for local magpie access, so the omp process can also access other
local listeners and external networks available to the host. There is no OS TCP destination deny or
isolation guarantee. The trusted pinned omp executable, disabled model shell/file tools and ambient
integrations, and trusted host gateway enforce the remaining authority. See the
[authority boundary](../security/authority-boundary.md).

For the uv-native source workflow, install uv 0.13.7 and Python 3.15.2.
The Lean project must already contain regular `lakefile.toml` and `lean-toolchain` files. For the
foundation engineering evidence contract, retain these formal-runtime pins:

| Component | Required value |
|---|---|
| Lean | 4.35.0 |
| Lean toolchain | `leanprover/lean4:v4.35.0` |
| lean-lsp-mcp | 0.33.0 |
| leanclient | 0.13.2 |
| MCP | `mcp[cli]==2.2.0` (exact) |
| Free disk floor | 2,147,483,648 bytes (2 GiB) |
| Foundation imports | `Std` only; no Mathlib, Batteries, REPL, or Loogle index |

MCP 2.2.0 is an exact upstream compatibility constraint for lean-lsp-mcp 0.33.0; do not treat it
as a minimum or substitute another MCP version. omp and magpie do not provision the pinned Python
environment or the external Lean toolchain. The Lean host is research-only and is not part of npm
release qualification. An unavailable Lean toolchain must be reported as unavailable formal
verification.

## Initialize and check readiness

Use a private project directory and initialize it once:

```sh
uv sync --frozen
uv run aizim init /absolute/path/to/lean-project
uv run aizim doctor --project /absolute/path/to/lean-project
uv run aizim security-probe --project /absolute/path/to/lean-project
```

Initialization creates private state below `.aizim/`; do not hand-edit its database or runtime
lock. A conflicting `.aizim/config.toml` is a hard initialization failure. For isolated acceptance
copies, copy checked-in project source into separate private directories without `.aizim/` or
`.lake/`. Never mix generated evidence from different project copies.

Doctor checks supported omp/magpie readiness, the selected model, RPC startup and host-tool
transport, and Lean prerequisites. It does not inspect provider credentials or check Codex/Claude.
A doctor failure blocks scheduling until the reported prerequisite is repaired.

The no-model security gate must print `SECURITY GATE PASS`. It executes restricted real omp RPC
`get_state` and tool-inventory inspection, without a prompt or model call, alongside gateway
authority checks and filesystem/environment probes. Eight operations must be denied: six
filesystem attacks, gateway Unix-socket access, and secret-environment access. Three controls must
be allowed: reading the view, writing scratch, and connecting to a host TCP listener. Protected
hashes/logical state must remain unchanged and replay must match after restart. The report must
state the shared-network limitation; the allowed TCP control does not demonstrate destination
confinement.

The new terminal receipt is `omp_security_gate_complete`, bound to the attack probe policy hash.
A legacy Codex `gate_b_complete` receipt cannot substitute for this gate. The restricted RPC
attestation has its own paths and identity; do not require its policy hash to equal the attack
probe hash. Any failed, missing, skipped, or inconclusive required evidence blocks the run.

## Run bounded research

Create an immutable target and start the supervisor:

```sh
uv run aizim research task add --project PROJECT \
  --task-id smoke-true --title 'Prove True' --statement True \
  --max-rounds 2 --max-failures 1 --timeout-seconds 120
uv run aizim research run --project PROJECT --max-seconds 300
uv run aizim status --project PROJECT --json
uv run aizim research task list --project PROJECT --json
```

`research run` performs the security gate before scheduling. This command can make model requests
through magpie; the security gate itself cannot. Each attempt receives fresh run/worker/lease
identities, private session storage, and only the authorized stdio host tools. Terminal work is
not dispatched again after recovery. Pause or resume a target through `research task pause` or
`research task resume` with its `--task-id`. Resume accepts `--max-rounds` and `--max-failures`;
a blocked target resumes only after its limits allow another round. Three consecutive
infrastructure failures, such as a model outage, pause a target without using its failure budget.

A verified target requires trusted Lean publication bound to the immutable target statement,
candidate, and attempt. Inspect the attempt's publication evidence; a completed omp prompt,
assistant summary, magpie usage record, or accepted human review is insufficient. Human reviews
of meaning, value, exposition, and release remain separate from formal verification.

## Foundation artifact acceptance and legacy evidence

The retained foundation checker evaluates a specific engineering evidence package, not every
research run. Its five registered artifacts are `run-manifest.json`, `formal-trace.jsonl`,
`formal-trace.sha256`, `alignment-review.json`, and `acceptance-report.json`, under
`.aizim/artifacts/RUN_ID/`. It requires the package's complete event/epoch, verification, worker,
alignment, shutdown, and policy bindings. A research run without this package is not a foundation
acceptance run; do not manufacture artifacts or a passing event to fill that gap.

For a current omp/magpie foundation package, run:

```sh
uv run python scripts/check_foundation.py --project PROJECT --run-id RUN_ID
# Select the newest omp/magpie foundation candidate:
uv run python scripts/check_foundation.py --project PROJECT --run-id latest-real
```

Success prints `FOUNDATION ACCEPTANCE PASS 16/16`. Selection requires `agent_harness_name=omp`
and `model_backend=magpie`; the final manifest must retain the starting identity, match
`omp/OMP_VERSION`, and identify the `aizim-worker` isolation profile. The checker requires a
preceding complete omp security receipt and consistent policy hashes. It does not fall back from
a later failed or incomplete security gate to an older successful one.

Runtime `16/16` covers replayable real-run predicates only and does not independently prove
CI-only or test-only rows of the overall handoff matrix. Real RPC smoke, deterministic protocol
and failure tests, formal/recovery regressions, and browser checks supply separate evidence.
Formal end-to-end acceptance additionally requires the external pinned Lean toolchain.

Historical Codex foundation events, exports, and fixtures remain legacy records with their
original identities and hashes. They are replayable and interpretable, but neither their old
Codex pin nor a historical Gate B pass qualifies new omp/magpie acceptance. Fake-child/synthetic
fixtures test checker behavior; they are not evidence that a live model or theorem run occurred.
The former `run --profile autonomous-shared`, Controller configuration, Worker registration, and
Codex-specific security-probe options are removed, with no execution aliases.

## Status, cleanup, and recovery

Use `aizim status --project PROJECT --json`, research task listings, and the read-only research
console/export to inspect current evidence. Keep formal verification and semantic review separate.
Never treat a stale omp session file as research authority.

Normal cleanup releases capabilities/leases and removes private views, scratch, and session
storage. After a hard kill, first stop the supervisor and confirm no Aizim, omp, or Lean process
owns a candidate directory and that no live lease remains. Remove only exact inspected orphan
paths. Never delete the state database, registered artifacts, or directories still owned by a
process. Continue with the [event recovery runbook](event-recovery.md) for interruption, replay,
publication, and quarantine handling.
