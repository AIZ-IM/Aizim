# Research capability roadmap

Status: Phase 1 implemented on 2026-11-11; Phases 2–5 remain proposed. The baseline below
describes the 2026-11-09 behavior that Phase 1 extends.

## Scope

Grow the native research workflow from a bounded proof loop into a research system that plans,
explores, reuses partial progress, and explains its results, while omp stays the customizable
agent runtime. Each phase below must keep the trust-core invariants. The direction follows
[Aizim on omp and magpie](../designs/2026-11-08-omp-magpie-research-architecture.md).

## Baseline

The 2026-11-09 defect fixes establish the execution semantics that later phases build on:

- an authorized tool that raises returns a stable reason code, is audited as `GatewayCallFailed`,
  and leaves the worker's channel usable; an accepted submission outranks earlier tool errors;
- a worker that abstains is distinct from a backend failure;
- each attempt has an enforced gateway call budget of 400 calls, stated in its instructions;
- a failed round keeps the agent's closing report, usage, stop reason, and gateway outcome counts;
- a submission that publishes after its round closed still verifies the target, also after a
  restart, and a later research run takes over a promotion an earlier one abandoned; a later round
  never downgrades a verified target;
- outages spare the failure budget and pause a target after three in a row; resume can raise the
  round and failure limits;
- `lean_multi_attempt` results keep the goals each snippet leaves open.

The domain model already declares research roles such as `formal_planner`, `lemma_inventor`,
`literature_scout`, and `counterexample_agent`, and the gateway advertises tools per role. The
research engine schedules only `proof_explorer` today.

## Trust-core invariants

Every phase keeps these properties. A feature that needs to weaken one requires a security
review and a revised boundary document before implementation.

- Only a trusted `DeclarationPublished` event bound to the immutable target, candidate, and
  attempt marks a target verified.
- `StateService` remains the only writer. Model processes never receive the service session.
- Every new model-facing operation is a gateway tool: role-advertised, minted per capability,
  budgeted, audited, and returning fixed public codes without exception text.
- Model-generated memory, plans, reviews, and reports are labelled as model output. They never
  satisfy formal verification or a human review.
- Workers have no network egress except through trusted host services.
- Targets stay immutable. A different question is a new target.

## Phase 1: execution reliability and measurement

Implemented behavior:

- **Benchmark ladder and metrics.** A versioned seven-target ladder covers identities,
  project definitions, and reuse of published dependencies. Both scripted and real omp backends
  use the trusted Lean publication path. Each fresh private run stores configuration and ladder
  hashes, verify rate, charged rounds/calls per verified target, measured token usage and optional
  price estimates, infrastructure failures, and wall time in `metrics.json`. Missing usage and
  prices remain unknown. A scheduled GitHub workflow runs the scripted ladder nightly; real
  models run on demand. See the [benchmark runbook](../operations/research-benchmark.md).
- **Run-level infrastructure circuit breaker.** Three consecutive infrastructure failures
  across completed attempts stop new scheduling, including under `--watch`, and finish the run
  with `infrastructure_unavailable`. Active attempts retain their existing bounds. Ready targets
  without a recent outage precede retries of affected targets.
- **Retryable rate limiting.** `REQUEST_RATE_EXCEEDED` remains a `CapabilityDenied` audit event
  but returns reserved `GATEWAY_RATE_LIMITED` with a bounded retry hint. The worker bridge waits
  and retries with fresh authorization within its attempt deadline. Throttles spend no call
  budget and do not appear as refusals.
- **Configurable per-target call budgets.** `call_budget` defaults to 400, ranges from 1 to
  10000, and stays outside the target hash. Each attempt snapshots it and mints it into the
  capability; the gateway enforces it across tokens for the same worker. Resume may raise it
  for later attempts. Existing capability databases migrate additively; old events remain readable.
- **Infrastructure rounds outside `max_rounds`.** Attempt `round` remains a monotonic sequence,
  tracked separately as task `attempt_sequence`. Task `rounds` counts charged attempts; outages
  and interruptions are refunded from durable attempt records, including across restarts.
- **Longer default rounds.** Research targets default to 900 seconds. Round publication grace
  uses the target timeout, capped at 900 seconds; an idle run uses the largest target timeout
  with the same cap. Late-publication reconciliation remains authoritative.
  Worker authority starts after resource reservation and shares a bounded setup/execution
  deadline, with per-directive leases covering both 900- and 14400-second attempts.
- **Promotion takeover everywhere.** Every consumer drains queued work at startup and polls
  every 15 seconds for queued submissions and claims without a heartbeat for one minute.
  `continue_on_failure` controls failure handling independently of recovery.

Acceptance evidence: the actual Lean scripted ladder verifies all seven targets and saves its
metrics; regression coverage exercises a three-target outage stopping once, retry after rate
limiting with fresh authorization, round refunds across repeated recovery crashes and late
verification, minted budgets, and promotion takeover.
The ladder also exposed and fixed an empty publication manifest newline that changed the base
epoch after its first publication, preventing subsequent targets from running.

## Phase 2: worker capability

Phase 2 depends on the Phase 1 budgets and metrics, so each gain is measurable.

- **Partial progress and helper lemmas.** Let a worker submit a helper lemma as its own
  contribution with its own candidate name. The existing promotion path publishes it, and later
  attempts can import it. Record the remaining goals of an unfinished proof in the round report.
  Proof text containing `sorry` is never published; it may be kept only as advisory memory.
- **Agent search tool.** Expose the offline declaration search and scoped memory search as
  read-only gateway tools with result caps. Security review: confirm results reveal no paths or
  content outside the materialized view and installed packages.
- **Memory-write tool.** Let a worker record dead ends and conjectures as worker-origin,
  unverified memory with size and rate caps. Security review: memory feeds later prompts, so
  treat it as persistent prompt-injection surface. Label it as untrusted when it is supplied,
  and let the operator redact entries.
- **Parallel attempts per target.** Allow several concurrent attempts on one target with diverse
  strategies or models. The first verified attempt settles the target; the others settle at their
  round boundary. Recovery must settle every running attempt of a target, not only the latest.

Acceptance: on the ladder, helper-lemma reuse and parallel attempts raise the verify rate or
lower calls per verified target; the security reviews are recorded before the tools are
advertised; a parallel run that verifies once never records a downgrade.

## Phase 3: research planning

Phase 3 depends on helper lemmas and the search tool.

- **Role scheduling.** Let the engine schedule declared roles other than `proof_explorer`, each
  with its own advertised tools and call budget. The items below use it.
- **Planner and auto-decomposition.** The `formal_planner` role proposes sub-targets as new
  targets with dependencies. Lean must elaborate each proposed statement before it is scheduled.
  Proposals need operator approval unless an explicit autonomy policy allows them, and a planner
  cannot edit existing targets.
- **Reviewer pre-audit.** A read-only reviewer role checks a statement before work begins for
  vacuous hypotheses, trivial conclusions, and mismatch with the informal claim. Its findings are
  model-generated review records; they never replace human semantic review.
- **Literature search.** A host-side search for the `literature_scout` role over an allowlist of
  sources, such as arXiv, zbMATH, and Mathlib documentation. Results are untrusted citations stored
  as source references. Network egress stays in the trusted host.
- **Compute sandbox.** A sandboxed computation tool for experiments such as counterexample search
  and numeric checks, with no network and with CPU, memory, and time limits. It reuses the
  platform sandbox adapters. Outputs are advisory memory, never evidence.

Acceptance: a decomposed ladder target verifies through planner-proposed sub-targets; every
proposed statement is elaborated before its first round; the sandbox passes the no-model
security gate with its own denial probes.

## Phase 4: omp role customization

Phase 4 can proceed in parallel with Phase 3, after the Phase 1 metrics exist.

- **Role definitions.** Per-role system prompt templates, models through magpie routing aliases,
  thinking level, and call budgets. Each attempt records the hash of the role definition it ran.
- **Sessions.** Optionally continue an omp session across rounds of one target. Session storage
  stays private to the target, and its lifetime is recorded.
- **Skills, extensions, and hooks.** Attempts currently run with `--no-extensions --no-skills`.
  Allow only reviewed, pinned, and hashed skills or extensions that run under the same sandbox and
  add no execution path outside the gateway.
- **Human chat.** A lower-latency operator channel to a target. Messages remain inbox entries
  consumed at round boundaries, and chat never grants authority.

Acceptance: two role definitions run the same ladder with recorded hashes and comparable
metrics; an unreviewed extension is rejected before launch; chat messages appear in the consuming
attempt's record.

## Phase 5: reports and exposition

Phase 5 depends on the records the earlier phases produce.

- **Research reports.** Per target, combine the statement, the published Lean source, the
  dependency graph, an attempt summary, human contributions, and reviews. Model-written
  exposition is labelled and kept separate from human exposition records. The existing private
  export path carries the report; nothing is published externally.

Acceptance: an exported report reproduces from the event record alone, and every model-written
passage carries its label.

## Dependencies

| Item | Depends on |
|---|---|
| Benchmark ladder, circuit breaker, retryable rate limit, longer rounds | none |
| Promotion takeover everywhere | none |
| Per-target call budgets | capability minting |
| Infrastructure rounds outside `max_rounds` | recovery attempt ordering |
| Helper lemmas, search tool, memory-write tool | Phase 1 metrics; security review for tools |
| Parallel attempts | per-target budgets; recovery of every running attempt |
| Role scheduling | per-target budgets |
| Planner | role scheduling; helper lemmas; search tool |
| Reviewer pre-audit | role scheduling |
| Literature search | role scheduling; a trusted host network service |
| Compute sandbox | role scheduling; platform sandbox adapters; no-model security gate |
| Role definitions, sessions, skills | Phase 1 metrics |
| Human chat | operator inbox |
| Research reports | records from earlier phases |
