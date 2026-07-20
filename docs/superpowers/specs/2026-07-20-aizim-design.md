# Aizim: Formal-Native Autonomous Mathematical Research

**Status:** Draft for user review

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
- From lean-lsp-mcp: structured goal inspection, virtual scratch documents,
  multi-attempt tactic screening, diagnostics, premise search, and soundness
  checks.

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
      |                |
Literature          Lean Bridge Supervisor
adapters            /                 \
      |       SharedLeanRuntime   IsolatedLeanRuntime
      |                |                 |
      |          Lean LSP/REPL      worker LSP/REPL
      |                \                 /
      |                 Lean kernel
      |                      |
      v                      v
EvidenceGraph     VerifiedFormalGraph
      \                      /
       \--- AlignmentEdge --/
                |
       Knowledge Promotion
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

The supervisor owns Lean process lifecycle and exposes a stable `LeanRuntime`
interface:

- inspect a goal or term goal;
- retrieve structured local context;
- screen several tactics or terms without modifying source;
- apply an accepted action to a worker-owned document;
- create and check a candidate declaration;
- inspect diagnostics and dependencies;
- verify axioms and project build status;
- snapshot, reopen, or discard a branch.

The first adapter wraps a pinned lean-lsp-mcp release and its Lean client. Aizim
records every tool action and result rather than treating MCP transcripts as the
canonical state.

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
- imports, Lean toolchain, and environment hash;
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
and promoted Lean artifacts can be exported from the database; agents do not
write the database directly.

The database contains append-only events plus materialized views. Important
objects are `Run`, `Worker`, `ResearchDirective`, `FileLease`, `EvidenceNode`,
`CandidateClaim`, `AlignmentEdge`, `FormalAction`, `Contribution`,
`VerifiedDeclaration`, `KnowledgeDelta`, and `Intervention`.

## 6. Agent model

Agent identities and Local Memory persist across rounds, as in Danus. One model
session does not have to persist forever: a new execution round can resume from
the worker's typed state and event history.

| Agent role | Responsibility | Permitted durable writes |
|---|---|---|
| Research Conductor | resources, routing, worker lifecycle | scheduling events |
| Literature Scout | primary-source and citation search | evidence proposals |
| Boundary Mapper | reconstruct known theorem boundaries | evidence proposals |
| Conjecture Generator | propose precise mathematical candidates | candidate proposals |
| Novelty Auditor | seek equivalent results and counterexamples | audit proposals |
| Formalizer | create Lean statements and alignment records | candidate declarations |
| Formal Planner | create a forest of typed subgoals | candidate task edges |
| Proof Explorer | inspect and modify its formal branch | formal action proposals |
| Lemma Inventor | propose auxiliary formal declarations | contributions |
| Counterexample Agent | test and formalize counterexamples | evidence/contributions |
| Learning Agent | explain and replay verified traces | educational events only |

Agents submit proposals. Evidence rules, Lean, and the promotion service perform
durable state transitions.

## 7. Divergence, files, and runtime isolation

### 7.1 Shared runtime, the default

`SharedLeanRuntime` uses one Lean project root and one `lake serve` process.
Workers share immutable prerequisites, caches, verified knowledge, and search
services, but never share mutable proof documents.

Each proof worker receives a `FileLease` containing:

- `worker_id` and `run_id`;
- owned physical file, if materialized;
- unique virtual-document namespace;
- `base_epoch` and environment hash;
- expiration and recovery metadata.

Only the lease owner may write the physical file. Other agents may inspect it or
fork virtual copies. `lean_multi_attempt`-style scratch slots let workers screen
alternatives without editing the original document. Public builds and common
imports are controlled by the promotion service.

### 7.2 Isolated runtime, available on demand

`IsolatedLeanRuntime` creates a worker-specific source snapshot, Lean process,
REPL, and build directory. It is selected for same-file competition, cross-file
refactoring, incompatible imports or toolchains, unstable heavy experiments, or
explicit process-isolated evaluation.

The interface and contribution format are identical in both runtime modes.
Switching isolation must not alter mathematical authority.

### 7.3 Escalation policy

Shared mode never silently becomes isolated during a benchmark. Outside a
benchmark, the conductor may propose escalation, but the resource policy must
approve it and the event log records it.

## 8. Knowledge promotion and final formatting

Research-time promotion and final presentation are separate.

### 8.1 Contribution

A worker submits a `Contribution` with:

```text
worker_id, run_id, base_epoch
candidate declaration and proof
formal dependencies
imports and environment hash
source patch or virtual-document snapshot
assumptions and axioms
formal action trace
research notes and evidence links
```

### 8.2 Knowledge Promotion Layer

Promotion replays the contribution, checks diagnostics and axioms, resolves
names and duplicates, and places the accepted result in an immutable research
knowledge module. It then updates VerifiedFormalGraph and publishes a
`KnowledgeDelta` containing the theorem name, complete type, module,
dependencies, assumptions, evidence links, and new `knowledge_epoch`.

Workers may consume a delta or continue from their previous epoch. Existing
branches are not force-updated, preserving divergence.

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

| Mode | Human participation | Result label |
|---|---|---|
| `autonomous` | goal and budget are fixed before start; observation and emergency stop only | `unassisted` |
| `collaborative` | directions, worker assignments, formal contributions, and route decisions are allowed | `assisted` |
| `learning` | the learner acts on a personal branch; AI explains, hints, and compares verified paths | `educational` |

All human actions are events. A run cannot be relabeled `unassisted` after an
intervention.

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
```

There is one uv environment and one shared Lean dependency cache. Isolated
runtimes, local Loogle, duplicate Mathlib trees, and large cache downloads are
disabled by default. Preflight warns on low disk and refuses an isolated runtime
when the configured free-space floor is not met.

## 12. Autonomous evaluation protocol

The default evaluation profile is `autonomous + shared`. It measures the Aizim
swarm as a system: workers may share newly promoted facts during the run. It
does not claim process-level isolation.

Every evaluation manifest freezes and records:

- starting `base_epoch` and `knowledge_epoch`;
- Lean, toolchain, dependency, and mathlib versions;
- model backend, model identifier, prompts, and reasoning settings;
- worker count, budgets, timeouts, and search permissions;
- warm/cold cache state;
- runtime mode;
- intervention count, which must be zero for `unassisted`.

Cross-run comparison starts from an explicitly selected knowledge snapshot so a
prior run cannot silently improve the next run.

## 13. Failure handling

- **Worker crash or timeout:** record the terminal event, preserve Local Memory
  and artifacts, release the lease, and allow a later round to resume.
- **Shared LSP crash:** pause proof workers, restart the runtime, reopen worker
  documents from persisted snapshots, and continue without inventing success.
- **Stale contribution:** retain it as a candidate and schedule replay/rebase;
  never promote it against a mismatched environment.
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

Lean tactics and meta-programs can execute code. The foundation assumes trusted
local Lean projects. Workers do not receive unrestricted shell access through
the Lean bridge, `lean_run_code` is disabled by default, path access is confined
to the active project and dependencies, and secrets never enter prompts or
event artifacts.

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
│   ├── lean/
│   ├── knowledge/
│   ├── modes/
│   ├── question_lab/
│   └── domains/algebraic_number_theory/
├── tests/
└── examples/smoke_lean/
```

The initial Python backend is a `CodexBackend` behind a typed `AgentBackend`
interface. Model identifiers and credentials are configuration, never hardcoded.

## 16. Delivery decomposition

The complete system is too large for one safe implementation batch. It is
delivered through ordered vertical slices:

1. **Foundation:** uv package, CLI, configuration, SQLite event store, typed
   domain model, status/doctor commands, and deterministic fake adapters.
2. **Shared formal loop:** shared lean-lsp-mcp runtime, file leases, virtual
   tactic attempts, two proof workers, contribution promotion, and a real Lean
   smoke project.
3. **Agentic modes:** real Codex backend, persistent worker memory,
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
4. Two proof workers can use one shared Lean runtime on separate leases without
   modifying each other's documents.
5. Virtual tactic trials do not edit the leased physical source until an action
   is explicitly accepted.
6. A promoted smoke declaration passes Lean diagnostics, whole-project build,
   and axiom checks before entering VerifiedFormalGraph.
7. The second worker receives the corresponding `KnowledgeDelta` and can use
   the promoted declaration as a prerequisite.
8. Worker, LSP, stale-epoch, and failed-promotion paths produce durable,
   inspectable failure events.
9. `uv run aizim status` reports workers, leases, epochs, candidates, verified
   declarations, and resource state.
10. An end-to-end `autonomous + shared` smoke run completes with zero human
    intervention and emits a replayable formal trace.
11. The run is described only as an engineering smoke test, not as evidence of
    open-problem or novelty capability.

## 18. Verification strategy

- Unit tests cover state transitions, role permissions, lease exclusivity,
  event replay, status classification, and contribution validation.
- Concurrency tests run separate worker documents through one shared runtime and
  assert that content and diagnostics never cross branches.
- Integration tests use a real Lean installation for goal inspection,
  multi-attempt trials, promotion, knowledge synchronization, and restart.
- A deterministic fake agent backend keeps orchestration tests reproducible.
- A real autonomous agent run is the manual QA gate for the executable vertical
  slice.
- The final acceptance run records exact versions, configuration, commands,
  exit statuses, and generated artifact paths.

The formal trace, not a prose claim that the system should work, is the evidence
that the foundation works.
