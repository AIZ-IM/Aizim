# Aizim: Formal-Native Autonomous Mathematical Research

**Status:** Revised draft for user review

**Date:** 2026-07-20

**Scope:** System architecture and the first executable vertical slice

## 1. Purpose

Aizim is an agentic mathematics research system in which formal language is the
native research medium, not merely the final transcription target. Its proving
agents inspect Lean goals and contexts, propose Lean terms, tactics,
declarations, constructions, and counterexamples, and learn from Lean's
diagnostics. Natural language remains available for research strategy,
literature work, human intervention, and explanation.

Aizim combines the strongest ideas of two existing systems without merely
joining them:

- From Archon: Lean project awareness, parallel proof work, review artifacts,
  and final project-level verification.
- From Danus: persistent research workers, divergent exploration, shared
  memory, fact dependencies, role separation, and human strategic guidance.
- From lean-lsp-mcp: structured goal inspection, scratch-document tactic
  screening, diagnostics, premise search, and soundness checks.

The defining change is the correctness boundary: an LLM never decides that a
formal fact is true. Only the Lean kernel can promote a declaration into formal
truth.

## 2. Confirmed product decisions

The following decisions are fixed for this design:

1. Lean 4 is the first and only formal backend in v1. Internal interfaces may
   later admit other proof assistants, but no prover-neutral formal IR is built
   now.
2. The Python orchestration project is uv-native: one `pyproject.toml`, one
   `uv.lock`, uv-managed environments, and uv-run entry points.
3. Aizim uses a hybrid formal action space:
   - proof-state actions for local reasoning;
   - new definitions, lemmas, examples, and counterexamples for research-level
     development.
4. Observable proof research is formal. Natural language is permitted in the
   strategy and collaboration layer.
5. Research remains divergent. Conflicting conjectures, abandoned routes, and
   alternative proof branches remain available until final assembly.
6. The initial product has three human-participation modes: `autonomous`,
   `collaborative`, and `learning`.
7. Aizim supports shared and isolated Lean runtimes. `shared` is the default,
   including for autonomous evaluation on this machine.
8. Question Lab searches for genuine mathematical gaps using literature, not
   merely missing mathlib formalizations.
9. The core is domain-general. The first domain pack targets algebraic number
   theory and quadratic number fields.
10. The first executable demonstration uses a smoke theorem only. Choosing a
    serious benchmark or open question is deliberately deferred until the
    foundation works and is not an acceptance dependency for the foundation.

## 3. Goals and non-goals

### 3.1 Goals

- Let multiple autonomous agents conduct long-running, divergent mathematical
  research while maintaining explicit provenance.
- Make Lean goals, contexts, declarations, and diagnostics first-class research
  state.
- Share new verified prerequisites quickly without forcing all workers onto one
  route.
- Prevent workers from silently overwriting one another.
- Separate literature evidence, candidate mathematics, and kernel-verified
  formal truth.
- Record failed attempts as reusable evidence rather than discarding them.
- Measure autonomous system capability without hidden human intervention.
- Reuse the same formal trace for collaboration and learning.

### 3.2 Foundation non-goals

- Solving or selecting a serious open problem.
- Claiming mathematical novelty from absence in search results.
- Supporting Coq, Isabelle, or a custom logic.
- Building a browser UI, paper-writing system, or publication pipeline.
- Automatically reorganizing an arbitrary external Lean repository.
- Running untrusted third-party Lean code without sandboxing.
- Maximizing worker count on the current machine.

## 4. Architecture

```text
Human natural-language guidance
             |
             v
      Research Conductor
       /              \
      v                v
Question Lab       Proving Lab
agents             agents
       \              /
        v            v
   Role-gated Capability Gateway (all agent I/O)
       /                    \
Evidence Proposal API    DocumentBroker
       |                    |
Literature adapters      Lean Bridge Supervisor
       |                 /                    \
       |        SharedLeanRuntime      IsolatedLeanRuntime
       |                 |                    |
       |              Lean LSP          worker Lean LSP
       |                  \                    /
       |                   Lean kernel
       \                      /
        v                    v
     Trusted State and Promotion Services
        |                    |
 EvidenceGraph      VerifiedFormalGraph
        \                    /
         \--- AlignmentEdge /
                  |
         KnowledgeDelta stream
                  |
         Final Assembly / Format
```

### 4.1 Research Conductor

The conductor maintains the research forest, budgets, worker identities,
runtime assignments, and mode policy. It may start, pause, resume, or redirect
workers, but it cannot write verified facts. It limits resources without forcing
mathematical convergence.

### 4.2 Question Lab

Question Lab accepts a human research direction and coordinates literature
scouts, boundary mappers, conjecture generators, counterexample agents,
novelty auditors, and formalizers. Its output is an evidence dossier and one or
more precisely stated candidate problems. It is an improved Danus-style
research subsystem, not a theorem-name search box.

### 4.3 Proving Lab

Proving Lab coordinates formal planners, proof explorers, lemma inventors, and
counterexample agents. Agents operate through the Lean Bridge, not by declaring
their own success. They may keep multiple incompatible branches alive.

### 4.4 Lean Bridge Supervisor

The supervisor owns Lean process lifecycle. Aizim exposes a composite
`LeanRuntime` interface; this interface is intentionally larger than the public
tool surface of lean-lsp-mcp:

- inspect a goal or term goal;
- retrieve structured local context;
- screen several tactics or terms without modifying source;
- apply an accepted action to a worker-owned document;
- create and check a candidate declaration;
- inspect diagnostics and dependencies;
- verify axioms and project build status;
- snapshot, reopen, or discard a branch.

The foundation composes, rather than forks, upstream
`lean-lsp-mcp==0.28.1` at commit
`15766fef24246f2159f55a5f6897126a26916ae8`. Aizim directly pins both
`lean-lsp-mcp==0.28.1` and `leanclient==0.12.1` in `pyproject.toml`; `uv.lock`
then fixes the exact artifacts. The adapter supplies goal and context inspection,
scratch-document multi-attempt screening, diagnostics, search, build, and axiom
queries. It does **not** edit source, own worker namespaces, or implement branch
transactions.

Aizim's `DocumentBroker` supplies the remaining operations: unique worker
document namespaces, compare-and-swap edits, candidate declaration creation,
source snapshots, reopen, discard, and promotion staging. The broker records
each accepted action and result as canonical Aizim state; raw MCP transcripts
are diagnostic artifacts only.

The optional `leanprover-community/repl` accelerator is a separate dependency,
not part of the authority boundary. It is disabled in slices 1 and 2; the LSP
path must satisfy the smoke run. If enabled later, its git revision is pinned to
the matching `lean-toolchain` release and recorded in the run manifest. The
Archon-bundled `0.25.1.post1` fork is an implementation reference, especially
for rate-limit adjustments, but is not a hidden foundation dependency. Aizim
will fork upstream only if a documented, tested upstream gap cannot be handled
by composition.

### 4.5 Capability Gateway and DocumentBroker

Agent processes are outside the trusted state boundary. Every agent call goes
through a role-gated gateway whose capability token is minted by the trusted
supervisor and binds `role`, `run_id`, `worker_id`, allowed operations, and any
`lease_id`. The token is held by a gateway sidecar outside the model-visible
environment; its claims are not supplied or editable by the model. Unknown or
missing roles fail closed to an empty mutation set.

The gateway exposes only role-appropriate tools. A proof worker can read the
approved project view, inspect Lean state, try actions in its private virtual
documents, propose an accepted edit, submit a contribution, and read published
knowledge. It cannot directly write a source file, invoke a public build, open
the SQLite database, promote a declaration, or use another worker's lease. The
conductor can schedule and route work but has no contribution-promotion tool.
The formal verification and promotion services are trusted deterministic
services, not LLM roles.

Backend isolation reinforces tool gating. The trusted launcher keeps model-
transport credentials out of model-controlled command environments. Those
commands receive a filtered, read-only project view that excludes `.aizim/`, a
private ephemeral scratch directory, no database file descriptor or writable
shared path, and a scrubbed environment. They have no arbitrary outbound
network: only launcher-owned model transport and the gateway sidecar are
allowlisted, while literature, Lean, and state access are brokered. A backend
that cannot establish and pass this isolation profile is refused for autonomous
execution. A launch with approvals or sandboxing bypassed is not a valid Aizim
backend.

### 4.6 Platform sandbox adapters

Slices 1 and 2 support macOS first. `MacOSSandboxAdapter` pins Codex CLI
`0.144.6` and uses its native permission-profile compiler, which enforces
filesystem and network policy with Seatbelt through `/usr/bin/sandbox-exec`.
Before launch, `WorkspaceViewBuilder` materializes a symlink-free worker view
containing only approved source; `.aizim/`, `.git/`, unleased documents, and
shared artifacts are absent. `CodexBackend` runs with the Aizim-owned profile,
read-only command execution, no approvals or sandbox bypass, an ephemeral
session, ignored user config/rules, and a minimal shell environment. Model-
controlled commands have no direct network; model transport remains launcher-
owned, and all mutations go through role-gated tools. The same compiled policy
drives deterministic filesystem, environment, and network probes, so startup
fails before a worker runs if enforcement is weaker than the declared profile.

The portable `SandboxAdapter` contract is shared, but Linux is not a slices-1/2
acceptance platform. Its planned adapter uses the pinned Codex CLI's default
bubblewrap filesystem/user/PID namespace isolation plus its seccomp network
filter; the legacy Landlock fallback is rejected unless separately qualified by
the same probes. Unsupported platforms fail closed rather than running without
an adapter.

## 5. Authority and persistent state

### 5.1 EvidenceGraph

EvidenceGraph stores literature claims, source metadata, quotations within
copyright limits, known boundaries, conjectures, counterexamples, negative
searches, and failed research routes. It is evidence, not formal truth.

Open-problem status is explicit:

- `open_cited`: a reliable source explicitly identifies the problem as open;
- `known`: a proof, theorem, or counterexample is located;
- `unresolved_search`: this search did not settle the status;
- `novel_candidate`: Aizim proposed the conjecture and novelty is not verified;
- `refuted`: a valid counterexample or contradiction is available.

Failure to find a result never upgrades a claim to `open_cited`.

### 5.2 VerifiedFormalGraph

Only Lean-accepted declarations enter VerifiedFormalGraph. Each node stores:

- fully qualified declaration name and type;
- source artifact and proof trace hashes;
- predecessor declaration IDs;
- imports, Lean toolchain, and `environment_fingerprint`;
- axioms used and verification results;
- originating worker, run, and contribution;
- links to supporting literature evidence.

### 5.3 AlignmentEdge

An AlignmentEdge records why a Lean statement expresses an informal research
claim. It stores symbol mappings, assumptions, quantifier scope, conventions,
and review status. Kernel acceptance proves the Lean statement, not the quality
of this semantic alignment; alignment therefore remains independently
auditable.

### 5.4 Storage

Runtime coordination uses a project-local `.aizim/state.sqlite3` database in WAL
mode for transactions and concurrent readers. Large artifacts live under
`.aizim/artifacts/<run_id>/` and are referenced by content hash. Stable reports
and promoted Lean artifacts can be exported from the database. Only the trusted
`StateService` process holds a database connection and resolves these paths;
the state tree is absent from the materialized agent view and the platform
sandbox denies access even if a model guesses its host path. All state mutations
arrive as authenticated gateway requests and are validated again inside the
service.

The database contains append-only events plus materialized views. Important
objects are `Run`, `Worker`, `ResearchDirective`, `FileLease`, `EvidenceNode`,
`CandidateClaim`, `AlignmentEdge`, `FormalAction`, `Contribution`,
`VerifiedDeclaration`, `KnowledgeDelta`, and `Intervention`.

Every event has an immutable envelope containing `event_id`, `schema_version`,
`event_type`, `occurred_at`, `actor`, `run_id`, `causation_id`, and a typed
payload. The foundation emits `schema_version = 1`. Schema changes add
deterministic upcasters and rebuildable projections; migrations never rewrite
historical payloads in place. A release must replay every event version it may
encounter before it can be declared compatible with an existing state store.

## 6. Agent model

Agent identities and Local Memory persist across rounds, as in Danus. One model
session does not have to persist forever: a new execution round can resume from
the worker's typed state and event history.

| Agent role | Responsibility | Exposed gateway capabilities |
|---|---|---|
| Research Conductor | resources, routing, worker lifecycle | query state; propose scheduling actions |
| Literature Scout | primary-source and citation search | brokered search; submit evidence proposals |
| Boundary Mapper | reconstruct known theorem boundaries | query evidence; submit evidence proposals |
| Conjecture Generator | propose precise mathematical candidates | submit candidate proposals |
| Novelty Auditor | seek equivalent results and counterexamples | brokered search; submit audit proposals |
| Formalizer | create Lean statements and alignment records | private Lean trials; submit candidate and alignment proposals |
| Formal Planner | create a forest of typed subgoals | query formal graph; submit task-edge proposals |
| Proof Explorer | inspect and modify its formal branch | leased Lean trials; propose document actions and contributions |
| Lemma Inventor | propose auxiliary formal declarations | leased Lean trials; submit contributions |
| Counterexample Agent | test and formalize counterexamples | submit evidence proposals and contributions |
| Learning Agent | explain and replay verified traces | read verified traces; submit educational annotations |

These are capability families, not permission to write storage. Agents submit
proposals; evidence rules, lease checks, Lean, the `StateService`, and the
promotion service perform durable transitions. The role-to-tool table is one
auditable, fail-closed source of truth used both when advertising tools and when
authorizing each call, following Danus's construction-level separation rather
than prompt-only instructions.

## 7. Divergence, files, and runtime isolation

### 7.1 Shared runtime, the default

`SharedLeanRuntime` uses one Lean project root and one `lake serve` process.
Workers share immutable prerequisites, caches, verified knowledge, and search
services, but never receive a writable project tree or share a mutable proof
document. Worker document state is owned by the `DocumentBroker`.

Each proof worker receives a `FileLease` containing:

- `worker_id` and `run_id`;
- broker-owned `document_id` and physical file, if materialized;
- unique virtual-document namespace;
- `base_epoch`, `knowledge_epoch`, file version, and content hash;
- expiration and recovery metadata.

Only a request authenticated as the lease owner may ask the broker to apply an
edit, and the broker checks lease ownership, expected version, content hash, and
epoch in one transaction before an atomic file replacement. The worker process
itself cannot write the physical file. Other agents may inspect published state
or request private virtual forks. `lean_multi_attempt`-style scratch slots let
workers screen alternatives without editing the original document. Public
builds and common imports are available only to the promotion service.

### 7.2 Epoch model

The two epochs identify different aspects of the same verified formal snapshot:

- `environment_fingerprint` is a content hash of `lean-toolchain`, the Lake
  manifest and dependency revisions, the allowed-import manifest, and
  elaboration-affecting Aizim configuration.
- `base_epoch` is a content-addressed hash of the canonical promoted Lean source
  tree, the promoted-module manifest, and `environment_fingerprint`.
- `knowledge_epoch` is a monotonically increasing sequence number for published
  verified-formal snapshots. The initial snapshot is epoch `0`; every successful
  declaration promotion or approved environment/import transition publishes one
  new `base_epoch` and increments `knowledge_epoch` exactly once.
- `evidence_revision` is a separate sequence for EvidenceGraph and alignment
  metadata. Evidence-only changes do not advance either formal epoch.

The single-writer promotion service publishes the `base_epoch` to
`knowledge_epoch` mapping. Thus the hash answers *which exact formal base?* and
the sequence answers *in what publication order?* A `KnowledgeDelta` names both
its previous and new pair. Run manifests and leases pin the pair, so two runs can
select an identical starting state rather than merely a similarly named one.

Every scored evaluation freezes the environment and allowed-import manifest;
any transition request is rejected and the run cannot continue under a changed
environment. Outside scored evaluation, the conductor may only propose a
transition. A trusted `EnvironmentPolicy` approves it: a predeclared policy may
approve unscored autonomous research, while collaborative runs require a human
approval event. Any human approval changes the participation label. The
promotion service executes an approved transition but cannot approve one, and
the approver, reason, old fingerprint, and new fingerprint are durable events.

A contribution is stale if either pinned formal epoch differs from the current
published pair. A patch-form contribution is also stale if its expected leased
file version no longer matches; an immutable snapshot-form contribution is
self-contained and is not made stale merely because the worker later edits its
leased document. An environment change necessarily changes `base_epoch`; there
is no independent, weaker environment-staleness rule. Stale work remains
immutable evidence but cannot be promoted directly. The promotion queue may
replay it in a staging document on the current pair; success creates a new
rebased contribution while preserving the original and its provenance.

### 7.3 Isolated runtime, available on demand

`IsolatedLeanRuntime` creates a worker-specific source snapshot, Lean process,
optional REPL, and build directory. It is selected for same-file competition,
cross-file refactoring, incompatible imports or toolchains, unstable heavy
experiments, or explicit process-isolated evaluation.

The interface and contribution format are identical in both runtime modes.
Switching isolation must not alter mathematical authority.

### 7.4 Escalation policy

Shared mode never silently becomes isolated during a benchmark. Outside a
benchmark, the conductor may propose escalation, but the resource policy must
approve it and the event log records it.

## 8. Knowledge promotion and final formatting

Research-time promotion and final presentation are separate.

### 8.1 Contribution

A worker submits a `Contribution` with:

```text
worker_id, run_id, lease_id
base_epoch, knowledge_epoch, file_version
payload_kind = patch | snapshot
candidate declaration and proof
formal dependencies
imports and environment_fingerprint
source patch with expected file_version, or immutable snapshot with payload_hash
assumptions and axioms
formal action trace
research notes and evidence links
```

For `patch`, `file_version` is an application precondition. For `snapshot`, it
records provenance only; integrity and replay use `payload_hash`.

### 8.2 Knowledge Promotion Layer

Shared mode has one durable, single-writer promotion queue. Its order is the
database enqueue sequence with `contribution_id` as a deterministic tie-breaker;
two racing contributions are never allowed to publish concurrently. For each
entry, promotion validates the capability and lease snapshot, stages or rebases
the contribution on the current formal epoch pair, checks diagnostics, build,
and axioms, resolves names and duplicates, and places the accepted result in an
immutable research knowledge module.

Publication is a crash-recoverable state machine (`staged`, `verified`,
`materialized`, `published`). Materialized modules are content-addressed and do
not enter the worker-visible import manifest until the final `StateService`
transaction. That transaction updates the manifest and formal graph projection,
advances the epoch pair, and emits a typed `KnowledgeDelta`. A declaration delta
contains the theorem name, complete type, module, dependencies, assumptions,
and evidence links; an environment delta contains the changed dependency/import
fingerprints. Both contain the old and new epoch pair. On restart, the service
completes or quarantines an unfinished publication before accepting the next
queue entry.

Workers may consume a delta or continue from their previous epoch. Existing
branches are not force-updated, preserving divergence; a later contribution
from such a branch follows the explicit stale replay/rebase path in section 7.2.

Unverified conclusions may be broadcast as tagged research leads but cannot be
used as verified predecessors.

### 8.3 Final Assembly / Format Layer

Only final assembly forces convergence. It chooses an accepted route, removes
redundant artifacts, places declarations into coherent human-facing modules,
normalizes names and imports, produces explanations and learning traces, and
re-runs whole-project verification. It does not erase discarded research
branches from the event history.

## 9. Human-participation modes

Participation mode and Lean runtime mode are independent axes.

| Mode | Human participation | Formal-participation label |
|---|---|---|
| `autonomous` | goal and budget are fixed before start; observation only; emergency stop aborts scoring | `formal_unassisted` |
| `collaborative` | directions, worker assignments, formal contributions, and route decisions are allowed | `formal_assisted` |
| `learning` | the learner acts on a personal branch; AI explains, hints, and compares verified paths | `educational` |

All human actions are events. A run cannot be relabeled `formal_unassisted`
after an intervention.

Formal participation and semantic alignment review are separate labels. Every
alignment records `review_kind = none | machine | human`, reviewer identity,
prompt or protocol revision, and verdict. An autonomous run may use a fresh LLM
alignment auditor; that is recorded as `machine`, does not count as human
intervention, and does not imply that the formal statement's intended meaning
was human-validated. Only an explicit human review may set `human`.

Learning mode reuses the same kernel-verified trace. It supports proof replay,
goal/context explanation, pausing to attempt a step, progressively stronger
hints, and comparison with an accepted path.

## 10. Question Lab and domain packs

Question Lab is literature-aware from its first functional release. Source
adapters prioritize primary papers, authoritative problem lists, books, and
review literature; bibliographic indexes are used where access permits. Every
open or novelty assessment carries source identifiers, retrieval dates, search
queries, and an explicit confidence/status classification.

A `DomainPack` supplies:

- domain vocabulary and equivalence patterns;
- authoritative source lists and search templates;
- conjecture and counterexample heuristics;
- preferred Lean imports, namespaces, and statement templates;
- semantic alignment rules and common failure modes.

The engine is domain-general. The first pack is `algebraic_number_theory`, with
QNF-specific guidance included without coupling the core to QNF.

## 11. Resource policy for the current Mac

The observed development machine is an Apple M4 with 10 cores, 24 GiB RAM, and
approximately 8.7 GiB free disk at design time. The default configuration is:

```toml
[run]
participation = "autonomous"
lean_runtime = "shared"

[resources]
max_proof_workers = 2
max_question_workers = 3
scratch_slots = 2
lsp_instances = 1
local_loogle = false
remote_search_max_concurrency = 1
```

There is one uv environment and one shared Lean dependency cache. Isolated
runtimes, local Loogle, duplicate Mathlib trees, and large cache downloads are
disabled by default. Preflight warns on low disk and refuses an isolated runtime
when the configured free-space floor is not met.

The slice-2 project at `examples/smoke_lean/` is explicitly Mathlib-free: it
imports only Lean 4 core and the `Std` library shipped in the pinned toolchain,
has no external Lake packages, does not depend on the external `Batteries`
package, and pins `leanprover/lean4:v4.32.0`. Slice 2 must not download Mathlib or
a local Loogle index, and premise search is not exercised by its acceptance run.
This keeps the formal bridge, lease, promotion, and synchronization test
independent of the machine's limited free disk.

When remote formal search is enabled in later profiles, workers call a central
`SearchBroker` rather than endpoints directly. The broker deduplicates identical
queries, caches results with provenance, applies backoff, and shares one token
bucket across all workers. For the pinned lean-lsp-mcp adapter, remote Loogle is
capped at no more than its configured three requests per 30 seconds. Endpoint
limits are manifest data, not assumed per-worker allowances.

## 12. Autonomous evaluation protocol

The default evaluation profile is `autonomous + shared`. It measures the Aizim
swarm as a system: workers may share newly promoted facts during the run. It
does not claim process-level isolation.

Every evaluation manifest freezes and records:

- starting `base_epoch` and `knowledge_epoch`;
- event schema version and `environment_fingerprint`;
- Lean, toolchain, and dependency versions; `mathlib_version` is nullable and is
  `null` for the foundation smoke project;
- lean-lsp-mcp, leanclient, and optional REPL revisions;
- agent harness name, version and binary hash, plus model backend, model
  identifier, prompts, and reasoning settings;
- worker count, budgets, timeouts, and search permissions;
- agent capability and process-isolation profiles;
- warm/cold cache state;
- runtime mode;
- environment-transition policy and any rejected transition requests;
- human intervention count, which must be zero for `formal_unassisted`;
- alignment-review kind, auditor identity, and verdict.

Cross-run comparison starts from an explicitly selected knowledge snapshot so a
prior run cannot silently improve the next run.

## 13. Failure handling

- **Worker crash or timeout:** record the terminal event, preserve Local Memory
  and artifacts, release the lease, and allow a later round to resume.
- **Shared LSP crash:** pause proof workers, restart the runtime, reopen worker
  documents from persisted snapshots, and continue without inventing success.
- **Stale contribution:** retain it as a candidate and schedule replay/rebase;
  never promote it against a mismatched formal epoch pair.
- **Promotion failure:** attach Lean diagnostics and keep the branch available
  for repair.
- **Conflicting names or placement:** allocate a stable research name during
  promotion; defer human-facing renaming to final assembly.
- **Literature outage or weak retrieval:** record `unresolved_search`; do not
  infer openness.
- **Database interruption:** transactions prevent partial graph updates;
  append-only events remain the recovery source.
- **Low disk:** stop new artifact-heavy work before existing state is endangered.

After restart, status must be reconstructed from durable state rather than
worker heartbeats alone.

## 14. Security and trust

The trusted computing base is deliberately small: the version-recorded,
isolation-probed agent harness and sandbox launcher, Capability Gateway,
`StateService`, `DocumentBroker`, promotion service, pinned Lean toolchain, and
Lean kernel. Model outputs and all model-controlled tool calls are untrusted.

The foundation accepts only trusted local dependency trees, but still treats
agent-generated Lean as active input because tactics and meta-programs can
execute code.

Model-controlled command subprocesses and Lean child processes run without
access to secrets or the state database and without writable access to the
canonical project. The broker resolves document identifiers through a canonical
allowlist, rejects absolute paths, traversal and symlink escapes, and performs
all physical edits itself. Disabling `lean_run_code`, source-pattern checks, and
restricted imports are defense in depth; they do not replace process isolation.
Outbound access is limited to launcher-owned model transport plus trusted
gateway/broker channels; model-controlled commands have no arbitrary network.
Prompts and event artifacts never contain credentials.

Every denied capability, lease mismatch, path violation, and sandbox failure is
an auditable event. Startup fails closed if the gateway role table, filesystem
policy, or isolation probe is unavailable. No benchmark may silently fall back
to a more permissive agent launch.

High-risk or externally supplied proofs require an isolated runtime. Final
verification includes whole-project build, axiom inspection, source-pattern
checks, and optional `lean4checker`; stronger comparator/external-checker
workflows are reserved for high-stakes publication or competition settings.

## 15. Foundation repository shape

```text
Aizim/
├── pyproject.toml
├── uv.lock
├── README.md
├── docs/
│   └── superpowers/specs/
├── src/aizim/
│   ├── cli/
│   ├── config/
│   ├── orchestration/
│   ├── agents/
│   ├── gateway/
│   ├── state/
│   ├── lean/
│   ├── knowledge/
│   ├── modes/
│   ├── question_lab/
│   └── domains/algebraic_number_theory/
├── tests/
└── examples/smoke_lean/
```

The initial Python backends are a deterministic `FakeAgentBackend` and a minimal
`CodexBackend` behind a typed `AgentBackend` interface. Both use the same
capability manifest; the real backend additionally must use the hardened child
launcher and pass its isolation probe. Model identifiers and credentials are
configuration, never hardcoded.

## 16. Delivery decomposition

The complete system is too large for one safe implementation batch. It is
delivered through ordered vertical slices:

1. **Authority foundation:** uv package, CLI, configuration, versioned SQLite
   event store, typed domain and epoch model, fail-closed Capability Gateway,
   hardened agent launcher, minimal real Codex backend, deterministic fake
   backend, isolation probes, and status/doctor commands.
2. **Shared formal loop:** Aizim `DocumentBroker`, pinned upstream lean-lsp-mcp
   adapter, shared LSP runtime, file leases, virtual tactic attempts, two proof
   workers, serialized contribution promotion, and the toolchain-Std-only Lean
   smoke project.
3. **Agentic modes:** persistent worker memory, richer agent roles,
   `autonomous`, `collaborative`, and trace-based `learning` flows.
4. **Question Lab:** literature adapters, evidence statuses, conjecture agents,
   formalization handoff, and the algebraic-number-theory domain pack.
5. **Isolation and assembly:** isolated runtime, stronger verification, final
   placement/formatting, and export.

Each slice must leave an executable end-to-end path. Later slices extend the
same state model and interfaces rather than replacing the foundation.

## 17. Foundation acceptance criteria

The first implementation plan covers slices 1 and 2. It is complete only when:

1. `uv sync` installs the project from a clean checkout.
2. `uv run aizim doctor` reports Python, uv, Lean, Lake, project, disk, and
   runtime readiness without exposing secrets.
3. `uv run aizim init <lean-project>` creates only Aizim-owned runtime state.
4. Missing, unknown, forged, and role-incompatible capabilities fail closed:
   disallowed tools are absent from discovery and server-side authorization
   independently rejects the calls with durable denial events.
5. A deterministic attack probe launched through the exact real-backend sandbox
   cannot read or directly modify `.aizim/state.sqlite3`, modify an unleased Lean
   file or shared artifact, escape through a symlink/path traversal, or make a
   non-allowlisted direct network call. SHA-256 digests of the unleased file and
   shared artifact remain identical; the canonical logical digest of protected
   state projections is unchanged except for the expected appended denial
   events.
6. Two proof workers can use one shared Lean runtime on separate leases without
   modifying each other's documents.
7. Virtual tactic trials do not edit physical source. An explicitly accepted
   action changes only the leased document through a version-checked broker CAS.
8. The smoke project builds with pinned Lean `v4.32.0`, Lean core, and the `Std`
   library shipped in that toolchain only; its manifest and Lake state contain no
   Mathlib, `Batteries`, or other external package dependency.
9. A contribution pinned to a stale epoch pair cannot publish directly. A stale
   file version additionally blocks patch-form contributions, while a valid
   immutable snapshot remains promotable after later worker edits. Replay creates
   a separately identified rebased contribution and preserves the original.
10. A promoted smoke declaration passes Lean diagnostics, whole-project build,
    and axiom checks before entering VerifiedFormalGraph.
11. Two racing valid contributions pass through the single-writer queue without
    a lost update and publish a deterministic sequence of epoch pairs.
12. The second worker receives the corresponding `KnowledgeDelta` and can use
    the promoted declaration as a prerequisite.
13. Worker, LSP, authorization, stale-epoch, interrupted-publication, and failed-
    promotion paths produce durable, inspectable events; replaying schema-v1
    events reconstructs the same visible state after restart.
14. `uv run aizim status` reports workers, leases, epochs, candidates, verified
    declarations, denied capabilities, and resource state.
15. An end-to-end `autonomous + shared` smoke run with the real `CodexBackend`
    completes with zero human intervention, emits a replayable formal trace, and
    labels formal participation and machine alignment review separately.
16. The run is described only as an engineering smoke test, not as evidence of
    open-problem or novelty capability.

## 18. Verification strategy

- Unit tests cover state transitions, epoch invariants, role permissions, lease
  exclusivity, event replay/upcasting, status classification, and contribution
  validation.
- Authorization tests enumerate every role/tool pair and also send forged,
  expired, missing, cross-worker, traversal, and symlink-escape requests against
  the real gateway and broker.
- Credential-free process-isolation tests run attack probes through the exact
  child launcher used by `CodexBackend`; a fake model is sufficient because the
  tested authority is the launcher and broker, not model compliance.
- Concurrency tests run separate worker documents through one shared runtime and
  assert that content and diagnostics never cross branches, then race promotion
  requests and assert a total epoch order.
- Integration tests use a real Lean installation for goal inspection,
  multi-attempt trials, promotion, knowledge synchronization, and restart.
- A deterministic fake agent backend keeps orchestration tests reproducible.
- Standard CI runs Python tests from `uv.lock`. A separate real-Lean workflow
  installs `elan` and pinned Lean `v4.32.0`, asserts the smoke Lake manifest has
  no external packages, and runs the toolchain-Std-only LSP/promotion integration
  suite.
  Mathlib-heavy tests are out of scope and later run only in an explicitly
  provisioned scheduled/manual job.
- A real autonomous Codex run, with credentials supplied outside the repository,
  is the manual QA gate for the executable vertical slice. It cannot be replaced
  by a fake-backend result.
- The final acceptance run records exact versions, configuration, commands,
  exit statuses, and generated artifact paths.

The formal trace, not a prose claim that the system should work, is the evidence
that the foundation works.

## 19. References and pins

Foundation dependencies:

- [Lean 4 `v4.32.0`](https://github.com/leanprover/lean4/tree/8c9756b28d64dab099da31a4c09229a9e6a2ef35), commit
  `8c9756b28d64dab099da31a4c09229a9e6a2ef35`.
- [lean-lsp-mcp `0.28.1`](https://github.com/oOo0oOo/lean-lsp-mcp/tree/15766fef24246f2159f55a5f6897126a26916ae8), commit
  `15766fef24246f2159f55a5f6897126a26916ae8`, and
  [leanclient `0.12.1`](https://pypi.org/project/leanclient/0.12.1/) are both
  direct Aizim dependency pins.
- [Codex CLI `0.144.6`](https://github.com/openai/codex/tree/5d1fbf26c43abc65a203928b2e31561cb039e06d), commit
  `5d1fbf26c43abc65a203928b2e31561cb039e06d`, is the foundation agent-harness
  and platform-sandbox baseline.
- [leanprover-community/repl](https://github.com/leanprover-community/repl/tree/68a3b3a059787a7db44fb1e6281e4a657efee470)
  tag `v4.32.0`, commit `68a3b3a059787a7db44fb1e6281e4a657efee470`,
  optional and disabled for slices 1 and 2.

Inspected architecture baselines, not vendored dependencies:

- [Archon](https://github.com/frenzymath/Archon/tree/5e9ae7615efa0aa2cff11edabd5fbc0d45308fd5), commit
  `5e9ae7615efa0aa2cff11edabd5fbc0d45308fd5`; its bundled modified
  lean-lsp-mcp identifies as `0.25.1.post1`.
- [Danus](https://github.com/frenzymath/Danus/tree/3663b82e5ac7a60d994bbdda947a99293b5e2e22), commit
  `3663b82e5ac7a60d994bbdda947a99293b5e2e22`, and
  [arXiv:2607.06447](https://arxiv.org/abs/2607.06447).
- [Rethlas](https://github.com/frenzymath/Rethlas/tree/974b82ccfbcb07c405a233886e6e8d1d4db84828), commit
  `974b82ccfbcb07c405a233886e6e8d1d4db84828`, and the associated
  [formal-verification paper, arXiv:2604.03789](https://arxiv.org/abs/2604.03789).
