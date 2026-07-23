# Foreground Controller Execution Loop

Status: approved direction

Date: 2026-07-23

Selected approach: executable vertical slice before daemonization or multi-worker scheduling

## Context

Aizim already persists one primary controller configuration, a worker roster, and
versioned worker assignments. Those records survive process restarts, but they do
not cause work to run. `aizim run` remains a separate fixed two-worker smoke path,
and control commands cannot mutate state while the state service owns the store.

The next slice turns the persisted control plane into one observable execution
loop. It must preserve Aizim's existing authority boundary: `StateService` remains
the only SQLite writer; controller and worker model output remains untrusted; only
the trusted supervisor may claim assignments, issue capabilities, launch workers,
or append lifecycle events.

## Goal

After an operator configures a primary controller, registers a worker, and assigns
a task, this command runs the pending assignment once:

```sh
aizim controller start \
  --project /absolute/path/to/lean-project \
  --foreground
```

The selected primary controller can be Codex or Claude. The controller model
returns a strictly validated worker directive. A trusted dispatcher then runs the
registered worker through the existing materialized-view, lease, capability,
gateway, sandbox, and worker-lifecycle path.

The first controller phase is complete only after both controller providers use
the same validated decision contract. Codex is connected first to prove the whole
path; Claude follows in the same phase. Worker execution remains Codex-backed in
this slice. Per-worker provider selection is deferred.

## Non-goals

This slice does not add:

- a detached daemon or `controller stop`;
- automatic retries after an interrupted or failed model call;
- concurrent or priority-based multi-worker scheduling;
- per-worker model/provider configuration;
- a dashboard;
- direct controller access to SQLite, canonical project files, shell, Lean, or
  gateway capabilities;
- a rewrite of orchestration or state logic in Rust.

Foreground `SIGINT` and `SIGTERM` are the stop interface. The existing Rust npm
launcher continues to replace itself with Python so signals and exit status reach
the controller. A native detached-process supervisor may be added after the
foreground lifecycle is proven.

## Operator Surface

Existing commands retain their syntax:

```sh
aizim controller configure --project PROJECT --provider codex --model MODEL
aizim controller configure --project PROJECT --provider claude --model MODEL
aizim worker register --project PROJECT --worker-id WORKER --role ROLE
aizim worker assign --project PROJECT --worker-id WORKER --task TASK
```

While no state process is live, these commands use a short-lived `StateService` as
they do today. While the foreground controller is live, they use narrow local RPC
operations on the private mode-`0600` Unix socket. They never receive the service
session secret and cannot invoke the generic trusted `append_event` RPC.

`aizim controller show --json` and `aizim worker list --json` continue to use the
read-only RPC surface and gain lifecycle/execution fields from projections.

## Components

### Controller supervisor

`ControllerSupervisor` is the trusted foreground composition root. It:

1. validates the project layout and runtime;
2. acquires the existing state-process PID and socket ownership;
3. creates and starts the sole `StateService`;
4. records controller lifecycle events;
5. checks provider readiness before claiming work;
6. runs the assignment loop;
7. handles cancellation and closes state, gateway, lease, view, and subprocess
   resources before returning.

Only one controller supervisor can own a project. Existing PID, socket, and
SQLite ownership checks remain the enforcement mechanism.

### Narrow control RPC

The live service exposes validated domain operations rather than broad database
or event access:

- configure the primary controller;
- register one worker;
- assign a new task version;
- query controller, roster, assignment, and execution projections.

The socket's existing same-UID trust assumption and mode `0600` protect operator
access. Sandboxed model-controlled processes cannot read `.aizim`, reach the state
socket, or call these operations. The generic `append_event` operation remains
service-session-only.

### Controller backend

Both providers implement one interface:

```text
plan(ControllerContext) -> ControllerDecision
```

`ControllerContext` is a bounded, canonical JSON document containing only:

- assignment ID, task version, and operator task;
- registered worker ID and role;
- project ID and current base/knowledge epochs;
- the role-derived allowed gateway operation names;
- configured budget and timeout ceilings.

It contains no service session, raw capability, credential, environment dump,
canonical project path, SQLite path, or unfiltered event history.

`ControllerDecision` is JSON-schema validated and has three possible actions:

- `dispatch`: identify the assigned worker and provide its instruction, budget,
  and timeout within trusted ceilings;
- `blocked`: provide a fixed public reason code;
- `reject`: reject an invalid or unsafe assignment with a fixed public reason
  code.

Free-form output, unknown keys, another worker ID, a role change, new tool
permissions, non-finite budgets, oversized instructions, or values above trusted
ceilings fail closed. Model reasoning and chain-of-thought are neither requested
nor persisted.

The Codex adapter reuses the package-local, version-checked Codex executable and
launcher-owned transport, but supplies no gateway session, tools, MCP servers, or
canonical project view; trusted code validates its final JSON against the same
decision schema. The Claude adapter uses non-interactive print mode, JSON-schema
output, an empty built-in tool set, no MCP configuration, a private empty working
directory, and a filtered environment. Current Claude Code supports `--tools ""`,
`--disallowedTools "mcp__*"`, `--strict-mcp-config`, and `--json-schema` for this
shape. No `bypassPermissions` mode is permitted.

At implementation time the npm `latest` versions of `@openai/codex` and
`@anthropic-ai/claude-code` are resolved, compatibility-tested, and then pinned
exactly in the lockfile and runtime checks. Runtime selection never falls back to
an unverified global executable in npm mode.

### Trusted assignment dispatcher

The dispatcher validates a `dispatch` decision, writes the canonical directive as
an immutable registered artifact, and records its SHA-256 in state. It then calls
the existing `WorkerRunner` path, which owns:

- a materialized read-only worker view;
- a private scratch directory;
- a role-bound file lease;
- a one-shot gateway session and capability;
- sandboxed Codex worker execution;
- contribution and worker lifecycle evidence;
- revocation, lease release, process reaping, and cleanup.

The controller model cannot bypass or widen this path.

## State and Idempotency

The slice adds controller lifecycle and assignment-execution evidence:

- `ControllerStarted`
- `ControllerStopped`
- `ControllerCrashed`
- `WorkerTaskClaimed`
- `WorkerTaskDispatchPlanned`
- `WorkerTaskCompleted`
- `WorkerTaskFailed`
- `WorkerTaskInterrupted`

Existing `WorkerStarted`, `WorkerStopped`, `WorkerCrashed`, lease, capability,
document, contribution, and promotion events remain authoritative for their own
subsystems.

A `worker_executions` projection is keyed by immutable `assignment_id`. A claim is
accepted only when:

- the assignment is still the latest version for that worker;
- no execution projection exists for the assignment;
- the worker has no active execution or lease;
- the configured controller version still matches the context used for planning.

The check and claim occur inside the single live `StateService` turn before any
provider call. Once an assignment has a terminal execution state, restart does not
run it again.

If the process dies after a claim but before a terminal event, startup records
`WorkerTaskInterrupted` after recovering any expired lease and revoking any
remaining capability. It does not automatically repeat the model call. The
operator creates a new task version to retry. This is fail-closed and avoids a
false exactly-once claim across an external model request.

A process cannot report its own hard death. On startup, the supervisor compares
the durable controller session with the recovered PID/socket ownership. If the
previous session never reached `ControllerStopped`, the new supervisor records
`ControllerCrashed` for that previous session before accepting new work.

## Execution Flow

1. The operator starts the foreground controller.
2. The supervisor starts `StateService` and records `ControllerStarted`.
3. Provider executable, version, authentication readiness, runtime, and sandbox
   prerequisites are checked before any assignment is claimed.
4. The loop reads the latest registered workers and assignments in stable worker
   ID order.
5. The service atomically claims one eligible assignment.
6. The selected Codex or Claude controller backend receives the bounded context.
7. The trusted dispatcher validates the decision and records the directive
   artifact and hash.
8. On `dispatch`, the existing worker authority and `WorkerRunner` execute exactly
   one sandboxed Codex worker.
9. Result and evidence hashes are persisted, followed by
   `WorkerTaskCompleted`, `WorkerTaskFailed`, or `WorkerTaskInterrupted`.
10. The loop checks for the next assignment. `SIGINT` or `SIGTERM` stops intake,
    completes cleanup, records `ControllerStopped`, checkpoints state, and exits.

An in-process wake signal is set after a live RPC assignment commit. It is only a
latency optimization: startup and every wake re-read durable projections, so a
lost signal cannot lose or duplicate work.

## Failure Handling

- Missing, mismatched, or unauthenticated provider: fail before claim.
- Security-gate or sandbox prerequisite failure: do not call a model or launch a
  worker.
- Controller timeout, malformed JSON, schema failure, or unsafe directive: record
  `WorkerTaskFailed` with a fixed code; launch no worker.
- Worker timeout or crash: revoke the capability, release the lease, persist the
  existing worker lifecycle evidence, then record `WorkerTaskFailed`.
- Cancellation: stop intake, cancel and reap the active child, revoke capability,
  release lease, and record `WorkerTaskInterrupted`.
- State RPC or replay failure: stop scheduling and exit nonzero. Never inspect or
  repair SQLite directly.

Errors exposed to the terminal use fixed categories and do not print prompts,
credentials, raw capabilities, authenticated URLs, full environments, or model
response bodies.

## Verification Strategy

The implementation uses focused evidence rather than a new acceptance matrix:

- unit coverage for event schemas, projections, guarded claims, decision-schema
  parsing, provider launch specifications, and replay;
- integration coverage for live control RPC while the controller owns state,
  one fake deterministic dispatch, terminal-state idempotency, restart recovery,
  and cancellation cleanup;
- provider contract tests proving that Codex and Claude receive the same canonical
  context and that malformed or over-authority decisions are rejected;
- security tests proving model subprocesses cannot reach canonical project state,
  `.aizim`, the state socket, credentials, or undeclared tools;
- source and npm-package manual QA that configures a controller, assigns one task,
  starts the foreground loop, observes a terminal persisted result, restarts, and
  observes no duplicate execution.

Credentialed real-provider runs remain local/manual gates. CI uses deterministic
fake providers and never receives model credentials.

## Implementation Order

1. Add guarded control operations and live narrow RPC mutations.
2. Add execution events, projections, claim/idempotency logic, and replay tests.
3. Add the foreground supervisor and deterministic fake controller backend.
4. Connect the Codex controller adapter and the existing Codex worker path.
5. Connect the Claude controller adapter to the same decision contract.
6. Extend CLI status output and documentation.
7. Run focused Python checks, npm build/tests, packaged CLI manual QA, and
   credentialed provider QA when credentials are available.

Detached lifecycle management, Rust daemonization, concurrent scheduling,
automatic retry policy, per-worker providers, and Dashboard work begin only after
this foreground slice is observed working.
