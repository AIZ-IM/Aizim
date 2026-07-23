# Foreground Controller Execution Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `aizim controller start --project PROJECT --foreground` execute each durable worker assignment at most once through a Codex- or Claude-controlled planning loop and the existing sandboxed Codex worker path.

**Architecture:** The foreground supervisor owns the sole live `StateService`, while public same-UID RPC exposes only three validated control mutations. A provider-specific controller backend receives bounded canonical JSON and returns one shared strict decision union; trusted code claims the assignment, registers the directive artifact, and runs one worker through the existing lease, capability, gateway, sandbox, and cleanup path. Durable controller and assignment-execution projections make restart behavior fail-closed and observable.

**Tech Stack:** Python 3.14 with `asyncio`, `jsonschema`, `pytest`, Ruff, and ty; Rust 2024 launcher with Cargo; Node.js 22.22.2 and npm 12.0.1; Lean 4.32.1; exact npm pins `@openai/codex@0.145.0` and `@anthropic-ai/claude-code@2.1.218`.

## Global Constraints

- Work directly in the current checkout. Do not create or use a git worktree.
- Preserve one SQLite writer: only the foreground `StateService` may mutate state while the controller is running.
- Keep generic `append_event` RPC service-session-only. Model processes never receive the service session.
- Keep controller models away from the canonical project, `.aizim`, state socket, gateway, shell authority, Lean authority, and raw credentials.
- Keep worker execution Codex-backed in this slice. `controller.provider` selects only the main controller backend.
- Read the worker Codex model from existing `AizimConfig.model` or `AIZIM_MODEL`; do not reuse a Claude controller model as the worker model.
- Support only `darwin-arm64`, `linux-arm64` glibc, and `linux-x64` glibc. Continue rejecting Intel macOS.
- Keep Aizim npm packages free of lifecycle scripts. Resolve Claude's native optional package directly so `npm install --ignore-scripts` remains supported.
- Use exact provider pins checked on 2026-07-24: Codex `0.145.0` and Claude Code `2.1.218`.
- Keep production Python modules at or below the repository's 250 pure-line architecture limit.
- Do not add a daemon, retry policy, concurrent scheduler, dashboard, per-worker provider selector, or Rust supervisor in this slice.
- Persist only the validated directive instruction, never controller system/input prompts, chain-of-thought, raw provider envelopes, credentials, capability tokens, authenticated URLs, or full environments.
- Use fixed public failure codes. Attach sensitive subprocess details only as in-memory exception causes.
- Add one focused failing test before each implementation increment, then run the narrowest passing gate before committing.

---

## Durable Contract

The implementation adds these event-to-projection transitions without changing `EVENT_SCHEMA_VERSION = 1`:

| Event | Projection key | Resulting status |
|---|---|---|
| `ControllerStarted` | `controller_runtime/primary` | `running` |
| `ControllerStopped` | `controller_runtime/primary` | `stopped` |
| `ControllerCrashed` | `controller_runtime/primary` | `crashed` |
| `WorkerTaskClaimed` | `worker_executions/{assignment_id}` | `claimed` |
| `WorkerTaskDispatchPlanned` | `worker_executions/{assignment_id}` | `planned` |
| `WorkerTaskCompleted` | `worker_executions/{assignment_id}` | `completed` |
| `WorkerTaskFailed` | `worker_executions/{assignment_id}` | `failed` |
| `WorkerTaskInterrupted` | `worker_executions/{assignment_id}` | `interrupted` |

`completed`, `failed`, and `interrupted` are terminal. A terminal assignment is never claimed again. An operator retries by creating a new `WorkerTaskAssigned` version, which has a new immutable `assignment_id`.

Event payloads are closed and exact:

| Event | Required payload fields |
|---|---|
| `ControllerStarted` | `controller_id`, `controller_session_id`, `controller_version`, `provider`, `backend_version`, `executable_hash` |
| `ControllerStopped` | `controller_id`, `controller_session_id`, `reason_code` |
| `ControllerCrashed` | `controller_id`, `controller_session_id`, `reason_code` |
| `WorkerTaskClaimed` | `assignment_id`, `controller_id`, `controller_session_id`, `controller_version`, `worker_id`, `task_version`, `execution_id` |
| `WorkerTaskDispatchPlanned` | `assignment_id`, `execution_id`, `directive_id`, `directive_artifact_hash`, `instruction_hash`, `budget`, `timeout_milliseconds` |
| `WorkerTaskCompleted` | `assignment_id`, `execution_id`, `result_hash` |
| `WorkerTaskFailed` | `assignment_id`, `execution_id`, `reason_code` |
| `WorkerTaskInterrupted` | `assignment_id`, `execution_id`, `reason_code` |

Controller stop reasons are `OPERATOR_SIGNAL`, `PREFLIGHT_FAILED`, and `CONTROLLER_FAILED`; crash recovery uses `UNCLEAN_SHUTDOWN`. Failed-task reasons are `CONTROLLER_BLOCKED`, `CONTROLLER_REJECTED`, `CONTROLLER_DECISION_INVALID`, `CONTROLLER_TIMEOUT`, `WORKER_FAILED`, and `WORKER_ROLE_UNSUPPORTED`. Interrupted-task reasons are `OPERATOR_SIGNAL` and `CONTROLLER_RESTART`.

The controller decision is exactly one of:

```python
@dataclass(frozen=True, slots=True)
class DispatchDecision:
    action: Literal["dispatch"]
    worker_id: str
    instruction: str = field(repr=False)
    budget: int
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class BlockedDecision:
    action: Literal["blocked"]
    reason_code: Literal["DEPENDENCY_UNAVAILABLE", "NO_SAFE_ACTION"]


@dataclass(frozen=True, slots=True)
class RejectDecision:
    action: Literal["reject"]
    reason_code: Literal["TASK_UNSAFE", "TASK_UNSUPPORTED"]


type ControllerDecision = DispatchDecision | BlockedDecision | RejectDecision
```

Trusted ceilings are `64 KiB` UTF-8 instruction bytes, budget `1..12`, and timeout `0 < seconds <= 60`. A dispatch may name only the already assigned worker and cannot name a role or add tools.

### Task 1: Add narrow live control RPC mutations

**Files:**

- Create: `src/aizim/state/control_operations.py`
- Modify: `src/aizim/orchestration/control_plane.py`
- Modify: `src/aizim/state/operations.py`
- Modify: `src/aizim/state/service.py`
- Modify: `src/aizim/cli/state_client.py`
- Modify: `src/aizim/cli/control_command.py`
- Test: `tests/integration/test_state_service_rpc.py`
- Test: `tests/integration/test_cli_control.py`
- Test: `tests/unit/test_control_plane_replay.py`

**Interfaces:**

```python
class ControlOperationTarget(Protocol):
    def append_event(self, command: AppendEventCommand) -> EventRecord: ...
    def query_projection(
        self, name: str, entity_id: str
    ) -> ProjectionRecord | None: ...


class ControlOperationError(RuntimeError):
    code: str


def configure_controller(
    target: ControlOperationTarget, provider: str, model: str | None
) -> int: ...


def register_worker(
    target: ControlOperationTarget, worker_id: str, role: AgentRole
) -> int: ...


def assign_task(
    target: ControlOperationTarget, worker_id: str, task: str
) -> int: ...
```

Public RPC operation names are:

```text
control.configure_controller
control.register_worker
control.assign_task
```

The success result is `{"version": positive_integer}`. RPC maps `ControlOperationError.code` to the existing fixed CLI messages and never includes an exception string.

- [ ] Add a failing RPC test proving an unauthenticated same-UID client can call `control.configure_controller`, `control.register_worker`, and `control.assign_task`, while the same client still receives `NOT_AUTHORIZED` for `append_event`.

```python
response = await rpc_call(
    service.socket_path,
    RpcRequest(
        operation="control.configure_controller",
        params={"provider": "codex", "model": "gpt-5.6-sol"},
        session_id=None,
    ),
)
assert response == RpcSuccess({"version": 1})
assert state.query_projection("controller", "primary") is not None
```

- [ ] Run the focused test and confirm it fails because the control operation is unknown:

```sh
/opt/homebrew/bin/uv run --frozen pytest \
  tests/integration/test_state_service_rpc.py \
  -k public_control_mutations -q
```

Expected result: one failed test with `UNKNOWN_OPERATION`.

- [ ] Move the existing validation and event-building rules into `state/control_operations.py`; keep `ControllerProvider` in `orchestration/control_plane.py` and make its three public functions thin typed delegates. Re-export `ControlOperationError` there as `ControlPlaneError` so the CLI error contract does not change.

- [ ] In `state/operations.py`, parse each control operation with an exact key set, call the state-local control function, and return only the committed projection version. Add a single handler function for the three names rather than expanding generic write authority.

```python
_HANDLERS.update(
    {
        "control.configure_controller": _control,
        "control.register_worker": _control,
        "control.assign_task": _control,
    }
)
```

- [ ] Add `control_committed: Callable[[str], None]` to `StateDependencies`, defaulting to a no-op. In `StateService._dispatch`, call it only after a successful `control.*` response. This callback becomes the foreground supervisor's in-process wake signal.

- [ ] Add `call_control_operation()` to `cli/state_client.py`. Use live RPC only when both the private PID record and socket validate. Use a short-lived `StateService` only when both are absent; treat partial, stale, replaced, or unsafe ownership records as an error.

- [ ] Update `cli/control_command.py` so configure/register/assign share that live-or-short-lived path. Do not send or load a service session in the CLI process.

- [ ] Add an integration test that starts the state server, invokes the three public CLI commands as subprocesses, verifies the live owner remains up, and checks the committed projections through read-only RPC.

- [ ] Run the focused control gates:

```sh
/opt/homebrew/bin/uv run --frozen pytest \
  tests/unit/test_control_plane_replay.py \
  tests/integration/test_state_service_rpc.py \
  tests/integration/test_cli_control.py -q
```

Expected result: all selected tests pass; the live mutation test observes exactly three wake callback invocations.

- [ ] Run style and type checks for the touched Python surface:

```sh
/opt/homebrew/bin/uv run --frozen ruff check \
  src/aizim/state/control_operations.py \
  src/aizim/orchestration/control_plane.py \
  src/aizim/state/operations.py \
  src/aizim/state/service.py \
  src/aizim/cli/state_client.py \
  src/aizim/cli/control_command.py \
  tests/integration/test_state_service_rpc.py \
  tests/integration/test_cli_control.py
/opt/homebrew/bin/uv run --frozen ty check
```

Expected result: both commands exit `0`.

- [ ] Commit the task:

```sh
git add src/aizim/state/control_operations.py \
  src/aizim/orchestration/control_plane.py \
  src/aizim/state/operations.py \
  src/aizim/state/service.py \
  src/aizim/cli/state_client.py \
  src/aizim/cli/control_command.py \
  tests/integration/test_state_service_rpc.py \
  tests/integration/test_cli_control.py \
  tests/unit/test_control_plane_replay.py
git commit -m "Add narrow live control operations"
```

### Task 2: Persist controller lifecycle and guarded assignment execution

**Files:**

- Create: `src/aizim/orchestration/controller_lifecycle.py`
- Create: `src/aizim/orchestration/controller_execution.py`
- Modify: `src/aizim/state/schema_v1_orchestration.py`
- Create: `src/aizim/state/projections_orchestration.py`
- Modify: `src/aizim/state/projections.py`
- Modify: `src/aizim/state/reducers.py`
- Create: `tests/unit/test_controller_execution_replay.py`
- Test: `tests/unit/test_event_contract.py`
- Test: `tests/unit/test_event_replay.py`

**Interfaces:**

```python
class ControllerExecutionError(RuntimeError):
    code: str


@dataclass(frozen=True, slots=True)
class ClaimedAssignment:
    assignment_id: str
    worker_id: str
    role: AgentRole
    task: str = field(repr=False)
    task_version: int
    execution_id: str
    controller_version: int


def start_controller(
    state: StateService,
    *,
    session_id: str,
    controller_version: int,
    provider: ControllerProvider,
    backend_version: str,
    executable_hash: str,
) -> None: ...


def recover_unclean_controller(state: StateService) -> str | None: ...
def stop_controller(state: StateService, session_id: str) -> None: ...


def claim_assignment(
    state: StateService,
    *,
    worker_id: str,
    controller_session_id: str,
    controller_version: int,
    execution_id: str,
) -> ClaimedAssignment: ...


def record_dispatch_planned(
    state: StateService,
    claim: ClaimedAssignment,
    *,
    directive_id: str,
    directive_artifact_hash: str,
    instruction_hash: str,
    budget: int,
    timeout_milliseconds: int,
) -> None: ...


def complete_assignment(
    state: StateService, claim: ClaimedAssignment, result_hash: str
) -> None: ...
def fail_assignment(
    state: StateService, claim: ClaimedAssignment, reason_code: str
) -> None: ...
def interrupt_assignment(
    state: StateService, claim: ClaimedAssignment, reason_code: str
) -> None: ...
```

- [ ] Write a failing replay test that configures a controller, registers and assigns one worker, starts a controller session, claims the assignment, plans it, completes it, closes the store, reopens it, and asserts the exact terminal projection.

```python
assert execution_payload == {
    "assignment_id": assignment_id,
    "budget": 12,
    "controller_id": "primary",
    "controller_session_id": "controller-session-1",
    "controller_version": 1,
    "directive_artifact_hash": "d" * 64,
    "directive_id": "directive-1",
    "execution_id": "execution-1",
    "instruction_hash": "a" * 64,
    "result_hash": "b" * 64,
    "status": "completed",
    "task_version": 1,
    "timeout_milliseconds": 60_000,
    "worker_id": "proof-a",
}
```

- [ ] Run the replay test and confirm it fails because the event codecs and projections are absent:

```sh
/opt/homebrew/bin/uv run --frozen pytest \
  tests/unit/test_controller_execution_replay.py -q
```

Expected result: failure naming `ControllerStarted` or `WorkerTaskClaimed` as an unknown event.

- [ ] Add strict codecs for all eight approved events. Require SHA-256 validators for executable, artifact, instruction, and result hashes; positive integers for versions and timeout milliseconds; exact status/reason enums; and no unknown payload fields.

- [ ] Add `controller_runtime` and `worker_executions` to `PROJECTION_NAMES`. Extend reducer entity routing so controller lifecycle events key on `controller_id` and task execution events key on `assignment_id`.

- [ ] Implement reducers that accept only these state transitions:

```text
missing|stopped|crashed -> running
running -> stopped|crashed
missing -> claimed
claimed -> planned|failed|interrupted
planned -> completed|failed|interrupted
```

Every other transition must raise `ProjectionAuthorityError`.

- [ ] Implement `claim_assignment()` with no `await` boundary between validation and `WorkerTaskClaimed`. Validate the latest assignment version, current controller projection version, worker roster role, absence of an execution for that assignment, absence of any non-terminal execution for the worker, and absence of an active document lease for the worker.

- [ ] Add tests for stale controller version, stale task version, duplicate claim, another active execution, active lease, terminal replay, and new task-version retry. Assert each rejected claim appends no event.

- [ ] Implement startup recovery helpers. `recover_unclean_controller()` records `ControllerCrashed(reason_code="UNCLEAN_SHUTDOWN")` only for a prior `running` session. `interrupt_nonterminal_executions()` records `WorkerTaskInterrupted(reason_code="CONTROLLER_RESTART")` for every `claimed` or `planned` projection in stable assignment-ID order.

- [ ] Run event and replay gates:

```sh
/opt/homebrew/bin/uv run --frozen pytest \
  tests/unit/test_event_contract.py \
  tests/unit/test_event_replay.py \
  tests/unit/test_controller_execution_replay.py -q
```

Expected result: all selected tests pass and replay produces the same logical digest before and after restart.

- [ ] Run architecture, style, and type checks:

```sh
/opt/homebrew/bin/uv run --frozen pytest \
  tests/architecture/test_authority_imports.py \
  tests/architecture/test_trusted_boundary.py -q
/opt/homebrew/bin/uv run --frozen ruff check \
  src/aizim/orchestration/controller_lifecycle.py \
  src/aizim/orchestration/controller_execution.py \
  src/aizim/state/schema_v1_orchestration.py \
  src/aizim/state/projections_orchestration.py \
  src/aizim/state/projections.py \
  src/aizim/state/reducers.py \
  tests/unit/test_controller_execution_replay.py
/opt/homebrew/bin/uv run --frozen ty check
```

Expected result: all commands exit `0`; no production module exceeds 250 pure lines.

- [ ] Commit the task:

```sh
git add src/aizim/orchestration/controller_lifecycle.py \
  src/aizim/orchestration/controller_execution.py \
  src/aizim/state/schema_v1_orchestration.py \
  src/aizim/state/projections_orchestration.py \
  src/aizim/state/projections.py \
  src/aizim/state/reducers.py \
  tests/unit/test_controller_execution_replay.py \
  tests/unit/test_event_contract.py \
  tests/unit/test_event_replay.py
git commit -m "Persist controller assignment execution"
```

### Task 3: Define the bounded controller decision contract

**Files:**

- Create: `src/aizim/orchestration/controller_backend.py`
- Create: `src/aizim/orchestration/controller_decision.schema.json`
- Create: `src/aizim/orchestration/fake_controller_backend.py`
- Modify: `src/aizim/orchestration/__init__.py`
- Create: `tests/unit/test_controller_decision.py`

**Interfaces:**

```python
MAX_CONTROLLER_INSTRUCTION_BYTES: Final = 64 * 1024
CONTROLLER_PLAN_TIMEOUT_SECONDS: Final = 60.0
MAX_WORKER_BUDGET: Final = 12
MAX_WORKER_TIMEOUT_SECONDS: Final = 60.0


@dataclass(frozen=True, slots=True)
class ControllerContext:
    assignment_id: str
    task_version: int
    task: str = field(repr=False)
    worker_id: str
    role: AgentRole
    project_id: str
    base_epoch: str
    knowledge_epoch: int
    allowed_operations: tuple[str, ...]
    max_budget: int
    max_timeout_seconds: float
    controller_version: int


class ControllerBackend(Protocol):
    @property
    def identity(self) -> BackendIdentity: ...
    async def preflight(self) -> None: ...
    async def plan(self, context: ControllerContext) -> ControllerDecision: ...


def controller_context_bytes(context: ControllerContext) -> bytes: ...
def parse_controller_decision(
    raw: bytes, context: ControllerContext
) -> ControllerDecision: ...
```

- [ ] Write failing tests for canonical context bytes, all three valid actions, every unknown key, wrong worker ID, role/tool injection, non-finite timeout, over-budget values, oversized UTF-8 instruction, free-form text, and malformed JSON.

- [ ] Add this closed schema shape to `controller_decision.schema.json`:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "oneOf": [
    {
      "type": "object",
      "additionalProperties": false,
      "required": ["action", "worker_id", "instruction", "budget", "timeout_seconds"],
      "properties": {
        "action": {"const": "dispatch"},
        "worker_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "instruction": {"type": "string", "minLength": 1},
        "budget": {"type": "integer", "minimum": 1, "maximum": 12},
        "timeout_seconds": {
          "type": "number",
          "exclusiveMinimum": 0,
          "maximum": 60
        }
      }
    },
    {
      "type": "object",
      "additionalProperties": false,
      "required": ["action", "reason_code"],
      "properties": {
        "action": {"const": "blocked"},
        "reason_code": {
          "enum": ["DEPENDENCY_UNAVAILABLE", "NO_SAFE_ACTION"]
        }
      }
    },
    {
      "type": "object",
      "additionalProperties": false,
      "required": ["action", "reason_code"],
      "properties": {
        "action": {"const": "reject"},
        "reason_code": {"enum": ["TASK_UNSAFE", "TASK_UNSUPPORTED"]}
      }
    }
  ]
}
```

- [ ] Run the decision tests and confirm they fail because the module does not exist:

```sh
/opt/homebrew/bin/uv run --frozen pytest \
  tests/unit/test_controller_decision.py -q
```

Expected result: collection fails with `ModuleNotFoundError`.

- [ ] Implement canonical context serialization using `canonical_json()`. Emit only the eleven declared fields; convert role and operations to strings; never include a filesystem path, socket, session, environment, capability, event history, or credential.

- [ ] Validate raw output with `jsonschema`, then apply semantic checks for UTF-8 byte length, `math.isfinite`, exact assigned worker, and trusted ceilings from the context. Raise only `ControllerBackendError` fixed codes.

- [ ] Implement `FakeControllerBackend` with an injected immutable decision and a list of received canonical context byte strings. Give it `BackendIdentity("fake", "deterministic-controller-v1", sha256_bytes(b"deterministic-controller-v1"))` and a no-op `preflight()`.

- [ ] Run the focused tests:

```sh
/opt/homebrew/bin/uv run --frozen pytest \
  tests/unit/test_controller_decision.py -q
/opt/homebrew/bin/uv run --frozen ruff check \
  src/aizim/orchestration/controller_backend.py \
  src/aizim/orchestration/fake_controller_backend.py \
  tests/unit/test_controller_decision.py
/opt/homebrew/bin/uv run --frozen ty check
```

Expected result: all commands exit `0`.

- [ ] Commit the task:

```sh
git add src/aizim/orchestration/controller_backend.py \
  src/aizim/orchestration/controller_decision.schema.json \
  src/aizim/orchestration/fake_controller_backend.py \
  src/aizim/orchestration/__init__.py \
  tests/unit/test_controller_decision.py
git commit -m "Define the controller decision contract"
```

### Task 4: Extract a reusable one-worker execution host

**Files:**

- Create: `src/aizim/orchestration/worker_host.py`
- Modify: `src/aizim/orchestration/conductor.py`
- Modify: `src/aizim/lean/documents.py`
- Modify: `src/aizim/orchestration/worker_gateway.py`
- Create: `tests/integration/test_worker_host.py`
- Test: `tests/integration/test_two_worker_shared_run.py`
- Test: `tests/integration/test_conductor_failure_lifecycle.py`

**Interfaces:**

```python
CONTROLLER_WORKER_TOOLS: Final = (
    GatewayTool.PROJECT_READ,
    GatewayTool.LEAN_GOAL,
    GatewayTool.LEAN_MULTI_ATTEMPT,
    GatewayTool.LEAN_DIAGNOSTICS,
    GatewayTool.DOCUMENT_APPLY,
    GatewayTool.CONTRIBUTION_SUBMIT,
    GatewayTool.KNOWLEDGE_READ,
)


def controller_worker_tools(role: AgentRole) -> tuple[GatewayTool, ...]:
    return tuple(
        tool for tool in advertised_tools(role)
        if tool in CONTROLLER_WORKER_TOOLS
    )


class WorkerExecutionHost:
    @classmethod
    async def open(
        cls,
        state: StateService,
        project_root: Path,
        smoke_root: Path,
        governor: ResourceGovernor,
        run_id: str,
    ) -> WorkerExecutionHost: ...

    async def run(
        self, directive: WorkerDirective, backend: WorkerBackend
    ) -> WorkerCursor: ...

    async def aclose(self) -> None: ...
```

- [ ] Write a failing integration test that opens one host, runs a recording submitted backend, and asserts `ScheduleProposed`, `WorkerStarted`, `AgentRunCompleted`, `WorkerStopped`, and `LeaseReleased` are durable while the active lease set and gateway socket are empty after close.

- [ ] Run the focused test and confirm it fails because `WorkerExecutionHost` is absent:

```sh
/opt/homebrew/bin/uv run --frozen pytest \
  tests/integration/test_worker_host.py -q
```

Expected result: collection fails with `ModuleNotFoundError`.

- [ ] Add public `DocumentBroker.recover()` as a narrow wrapper around its existing guarded recovery path. Recovery must restore prepared documents, record `LeaseRecovered`, and revoke lease-bound capabilities through the existing transactional document operation.

- [ ] Extract the shared `DocumentBroker`, `SharedLeanRuntime`, `KnowledgeStream`, `ArtifactStore`, `WorkerGatewayActions`, `CapabilityGateway`, `ProjectSocketAlias`, `GatewaySessionBroker`, `BrokerWorkerAuthority`, `WorkerRunner`, `PromotionConsumer`, and cleanup composition into `WorkerExecutionHost`.

- [ ] Start the session broker and promotion consumer before `run()`. Race the worker against `PromotionConsumer.wait_for_failure()`. Reuse the conductor's existing cancellation-resistant cleanup ordering for consumer, sessions, runtime, socket alias, and worker resources.

- [ ] Change `ResearchConductor` to use the extracted host for its existing two-worker flow. Preserve event order, result shape, prewarming, and failure behavior; do not alter the public `run_fake()` or `run_two_worker()` signatures.

- [ ] Add `controller_worker_tools()` and test that it is an ordered intersection of the role matrix and actual `WorkerGatewayActions.targets()`. It may be empty for unsupported roles and must never include trusted-only operations.

- [ ] Run host and conductor regression gates:

```sh
/opt/homebrew/bin/uv run --frozen pytest \
  tests/integration/test_worker_host.py \
  tests/integration/test_two_worker_shared_run.py \
  tests/integration/test_conductor_failure_lifecycle.py -q
```

Expected result: all selected tests pass with unchanged two-worker assertions.

- [ ] Run style, type, and architecture gates:

```sh
/opt/homebrew/bin/uv run --frozen ruff check \
  src/aizim/orchestration/worker_host.py \
  src/aizim/orchestration/conductor.py \
  src/aizim/lean/documents.py \
  src/aizim/orchestration/worker_gateway.py \
  tests/integration/test_worker_host.py
/opt/homebrew/bin/uv run --frozen ty check
/opt/homebrew/bin/uv run --frozen pytest \
  tests/architecture/test_authority_imports.py -q
```

Expected result: all commands exit `0`.

- [ ] Commit the task:

```sh
git add src/aizim/orchestration/worker_host.py \
  src/aizim/orchestration/conductor.py \
  src/aizim/lean/documents.py \
  src/aizim/orchestration/worker_gateway.py \
  tests/integration/test_worker_host.py \
  tests/integration/test_two_worker_shared_run.py \
  tests/integration/test_conductor_failure_lifecycle.py
git commit -m "Extract single worker execution host"
```

### Task 5: Run the foreground supervisor with deterministic backends

**Files:**

- Create: `src/aizim/orchestration/controller_dispatcher.py`
- Create: `src/aizim/orchestration/controller_supervisor.py`
- Create: `src/aizim/cli/controller_command.py`
- Modify: `src/aizim/cli/main.py`
- Modify: `src/aizim/cli/control_command.py`
- Create: `tests/integration/test_controller_supervisor.py`
- Create: `tests/integration/test_cli_controller_start.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class ControllerSupervisorDependencies:
    controller_backend: Callable[
        [ControllerProvider, str | None], ControllerBackend
    ]
    worker_backend: Callable[[], AgentBackend]
    worker_preflight: Callable[[], Awaitable[None]]
    session_ids: Callable[[], str]
    execution_ids: Callable[[], str]
    directive_ids: Callable[[], str]


class ControllerSupervisor:
    def __init__(
        self,
        project: Path,
        dependencies: ControllerSupervisorDependencies | None = None,
    ) -> None: ...

    async def run(self) -> None: ...
    def request_stop(self) -> None: ...


def run_controller_start(project: Path, foreground: bool) -> int: ...
```

- [ ] Write a failing integration test that initializes a project, configures `codex`, registers `proof-a`, assigns one task, injects a fake dispatch decision and submitted worker backend, runs the supervisor, waits for `completed`, stops it, restarts it, and asserts neither backend is invoked a second time.

- [ ] Add failing tests for live RPC wake-up, provider failure before claim, stale controller version, blocked/reject decisions without worker launch, malformed decision failure, cancellation cleanup, and startup recovery of an unclean controller plus a planned execution.

- [ ] Run the supervisor tests and confirm they fail because the supervisor is absent:

```sh
/opt/homebrew/bin/uv run --frozen pytest \
  tests/integration/test_controller_supervisor.py -q
```

Expected result: collection fails with `ModuleNotFoundError`.

- [ ] Implement the trusted dispatcher. Build a deterministic valid Lean seed:

```python
name = f"aizim_assignment_{claim.assignment_id[:16]}"
initial_source = (
    f"import Std\n\n"
    f"theorem {name} : True := by\n"
    f"  sorry\n"
).encode()
```

Build a `WorkerDirective` using the claimed worker role, decision instruction, exact role/tool intersection, trusted budget/timeout ceilings, and a random trusted directive ID. Derive `execution_run_id = f"task-{claim.execution_id}"`, use it for `ArtifactStore` and `WorkerExecutionHost`, and store canonical directive JSON with `ArtifactStore.store(execution_run_id, "controller-directive", ...)`; then record `WorkerTaskDispatchPlanned` before launching the worker. A distinct execution run ID prevents two sequential assignments for the same worker from sharing a document path.

- [ ] Use `AizimConfig.model` for `CodexWorkspaceBackend`, never the controller's configured model. If the worker model is absent, fail preflight with `WORKER_MODEL_REQUIRED` before claiming.

- [ ] Require trusted ID factories to return `controller-{hex}`, `execution-{hex}`, and `directive-{hex}` identifiers that satisfy the existing artifact, run, and worker-path contracts. Reject an empty controller tool intersection with `WORKER_ROLE_UNSUPPORTED` after claim and before constructing `WorkerDirective`.

- [ ] Implement this foreground order:

```text
validate layout and fixed runtime
acquire state.pid ownership
start StateService with assignment-wake callback
recover document leases and lease-bound capabilities
record prior ControllerCrashed and WorkerTaskInterrupted states
load the unique ProjectInitialized projection plus epochs/global for bounded context
load controller configuration and instantiate both required backends
record ControllerStarted
run provider, worker, resource, sandbox, and authentication preflights
scan workers in stable worker_id order
claim one eligible assignment
plan and validate one decision
record terminal execution result
repeat scan, otherwise await wake or stop
on signal cancel and reap active work
record ControllerStopped, checkpoint, close state, release PID ownership
```

- [ ] Map `blocked` to `WorkerTaskFailed/CONTROLLER_BLOCKED`, `reject` to `WorkerTaskFailed/CONTROLLER_REJECTED`, malformed output to `CONTROLLER_DECISION_INVALID`, timeout to `CONTROLLER_TIMEOUT`, worker failure to `WORKER_FAILED`, and cancellation to `WorkerTaskInterrupted/OPERATOR_SIGNAL`.

- [ ] Add `controller start --project PROJECT --foreground` to `cli/main.py`. Reject an omitted `--foreground`; do not add detached mode or `controller stop`. Install `SIGINT` and `SIGTERM` handlers only in the top-level CLI runner.

- [ ] Keep the existing configure/show implementations in `control_command.py`; put only start lifecycle and safe error reporting in `cli/controller_command.py`.

- [ ] Run deterministic end-to-end gates:

```sh
/opt/homebrew/bin/uv run --frozen pytest \
  tests/integration/test_controller_supervisor.py \
  tests/integration/test_cli_controller_start.py \
  tests/integration/test_cli_control.py -q
```

Expected result: all selected tests pass; restart leaves one terminal execution event and zero duplicate model calls.

- [ ] Run style, type, and architecture gates:

```sh
/opt/homebrew/bin/uv run --frozen ruff check \
  src/aizim/orchestration/controller_dispatcher.py \
  src/aizim/orchestration/controller_supervisor.py \
  src/aizim/cli/controller_command.py \
  src/aizim/cli/main.py \
  tests/integration/test_controller_supervisor.py \
  tests/integration/test_cli_controller_start.py
/opt/homebrew/bin/uv run --frozen ty check
/opt/homebrew/bin/uv run --frozen pytest \
  tests/architecture/test_authority_imports.py \
  tests/architecture/test_trusted_boundary.py -q
```

Expected result: all commands exit `0`.

- [ ] Commit the task:

```sh
git add src/aizim/orchestration/controller_dispatcher.py \
  src/aizim/orchestration/controller_supervisor.py \
  src/aizim/cli/controller_command.py \
  src/aizim/cli/main.py \
  src/aizim/cli/control_command.py \
  tests/integration/test_controller_supervisor.py \
  tests/integration/test_cli_controller_start.py
git commit -m "Run the foreground controller loop"
```

### Task 6: Connect the sandboxed Codex controller backend

**Files:**

- Create: `src/aizim/orchestration/controller_process.py`
- Create: `src/aizim/orchestration/codex_controller.py`
- Create: `tests/unit/test_codex_controller.py`
- Create: `tests/security/test_controller_provider_isolation.py`
- Modify: `src/aizim/orchestration/controller_supervisor.py`
- Modify: `src/aizim/orchestration/codex_worker.py`
- Modify: `src/aizim/orchestration/__init__.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class ControllerLaunchSpec:
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str] = field(repr=False)
    stdin: bytes = field(repr=False)
    timeout_seconds: float
    output_limit: int


@dataclass(frozen=True, slots=True)
class ControllerLaunchOutcome:
    stdout: bytes = field(repr=False)
    stderr_hash: str
    exit_code: int


class CodexControllerBackend:
    def __init__(
        self,
        executable: Path,
        model: str | None,
        project_root: Path,
        parent_environment: Mapping[str, str],
        launch: ControllerLauncher = launch_controller_process,
    ) -> None: ...
```

- [ ] Write failing unit tests for exact Codex argv, version/image pinning, login-status preflight, bounded stdin/output, timeout reaping, final JSON parsing, and executable replacement after construction.

- [ ] Write a failing security test that plants a project secret, `.aizim` secret, state socket path, fake capability, `NPM_TOKEN`, and unrelated environment secret, then asserts none appears in controller argv, private view, filtered environment, events, artifacts, or error text.

- [ ] Run the tests and confirm they fail because `codex_controller.py` is absent:

```sh
/opt/homebrew/bin/uv run --frozen pytest \
  tests/unit/test_codex_controller.py \
  tests/security/test_controller_provider_isolation.py -q
```

Expected result: collection fails with `ModuleNotFoundError`.

- [ ] Build the Codex launch spec over a private mode-`0700` temporary controller directory outside the canonical project. Compile the existing OS sandbox with the canonical project as the denied root and only the empty view, scratch, Python runtime, Codex runtime, CA roots, and required dynamic-loader roots readable. Delete the temporary directory after every outcome.

- [ ] Use this Codex command shape:

```text
codex
  -c 'shell_environment_policy.inherit="none"'
  --strict-config
  exec
  --ignore-user-config
  --ignore-rules
  --ephemeral
  --skip-git-repo-check
  --sandbox read-only
  --json
  --output-schema ABSOLUTE_SCHEMA_PATH
  --output-last-message PRIVATE_FINAL_PATH
  -C PRIVATE_EMPTY_VIEW
  --model CONFIGURED_MODEL
  -
```

Omit the two model arguments when the configured controller model is `None`. Do not add an Aizim MCP override, gateway session, capability, canonical project view, or writable canonical path.

Codex `0.145.0` has no stable empty-built-in-tool CLI flag. Enforce the approved boundary with no MCP configuration, `--sandbox read-only`, an empty private working tree, no inherited shell-tool environment, and the outer OS sandbox; the provider process may use its authentication environment, but model-invoked commands must not inherit it.

- [ ] Reuse `communicate_bounded()` and cancellation-resistant process draining. Cap combined provider output at `1 MiB`; hash stderr; reject nonzero exit, missing final file, oversized file, non-JSON output, or a changed executable with fixed codes.

- [ ] Filter the child environment to the minimal locale, temporary-directory, CA, `HOME`, `PATH`, and OpenAI authentication variables required by the CLI. Remove every `AIZIM_*` distribution variable and all unrelated variables.

- [ ] Implement `preflight()` as exact executable/version/hash validation, OS sandbox compilation, and `codex login status`. Require `codex --version` to equal `codex-cli 0.145.0`. Discard readiness output after checking the exit code.

- [ ] Add `preflight_codex_worker()` in `codex_worker.py`. It must resolve and version-check the worker executable, build a private empty probe view, compile and validate the existing worker sandbox, and remove the probe view without calling a model. Wire it as `ControllerSupervisorDependencies.worker_preflight`.

- [ ] Wire `ControllerProvider.CODEX` in the production supervisor factory while retaining dependency injection for deterministic tests.

- [ ] Run provider and security gates:

```sh
/opt/homebrew/bin/uv run --frozen pytest \
  tests/unit/test_codex_controller.py \
  tests/security/test_controller_provider_isolation.py \
  tests/integration/test_controller_supervisor.py -q
/opt/homebrew/bin/uv run --frozen ruff check \
  src/aizim/orchestration/controller_process.py \
  src/aizim/orchestration/codex_controller.py \
  src/aizim/orchestration/codex_worker.py \
  tests/unit/test_codex_controller.py \
  tests/security/test_controller_provider_isolation.py
/opt/homebrew/bin/uv run --frozen ty check
```

Expected result: all commands exit `0`; the isolation test finds no secret or authority-bearing value.

- [ ] Commit the task:

```sh
git add src/aizim/orchestration/controller_process.py \
  src/aizim/orchestration/codex_controller.py \
  src/aizim/orchestration/controller_supervisor.py \
  src/aizim/orchestration/codex_worker.py \
  src/aizim/orchestration/__init__.py \
  tests/unit/test_codex_controller.py \
  tests/security/test_controller_provider_isolation.py
git commit -m "Connect the Codex controller backend"
```

### Task 7: Carry the exact Claude executable through npm and Rust

**Files:**

- Create: `lib/claude.mjs`
- Create: `tests/npm/claude-resolution.test.mjs`
- Modify: `package.json`
- Modify: `package-lock.json`
- Modify: `lib/platform.mjs`
- Modify: `lib/assets.mjs`
- Modify: `lib/launch.mjs`
- Modify: `lib/errors.mjs`
- Modify: `scripts/npm/build.mjs`
- Modify: `scripts/npm/assemble.mjs`
- Modify: `scripts/npm/pack.mjs`
- Modify: `scripts/npm/install-smoke.mjs`
- Modify: `scripts/npm/write-ci-evidence.mjs`
- Modify: `tests/npm/assets.test.mjs`
- Modify: `tests/npm/launch.test.mjs`
- Modify: `tests/npm/package-contract.test.mjs`
- Modify: `tests/npm/package-contents.test.mjs`
- Modify: `tests/npm/ci-evidence.test.mjs`
- Modify: `crates/aizim-launcher/src/args.rs`
- Modify: `crates/aizim-launcher/src/manifest.rs`
- Modify: `crates/aizim-launcher/src/provision.rs`
- Modify: `crates/aizim-launcher/src/main.rs`
- Modify: `crates/aizim-launcher/tests/manifest_contract.rs`
- Modify: `crates/aizim-launcher/tests/provision_contract.rs`
- Modify: `src/aizim/runtime/distribution.py`
- Modify: `src/aizim/cli/doctor_command.py`
- Modify: `tests/unit/test_distribution_context.py`
- Modify: `tests/integration/test_cli_doctor.py`

**Native Claude package mapping:**

| Aizim target | Claude optional package | Binary |
|---|---|---|
| `darwin-arm64` | `@anthropic-ai/claude-code-darwin-arm64@2.1.218` | `claude` |
| `linux-arm64` | `@anthropic-ai/claude-code-linux-arm64@2.1.218` | `claude` |
| `linux-x64` | `@anthropic-ai/claude-code-linux-x64@2.1.218` | `claude` |

- [ ] Add a failing Node test that constructs exact meta/native Claude packages, resolves the native `claude` file, and rejects wrong versions, a missing optional package, a missing executable, a non-executable file, and every PATH fallback.

- [ ] Run the resolver test and confirm it fails because `lib/claude.mjs` is absent:

```sh
node --test tests/npm/claude-resolution.test.mjs
```

Expected result: test collection fails with `ERR_MODULE_NOT_FOUND`.

- [ ] Add the exact dependency without running third-party install scripts:

```sh
npm install --save-exact --ignore-scripts \
  @anthropic-ai/claude-code@2.1.218
```

Expected result: `package.json` and `package-lock.json` pin `2.1.218`; npm installs the matching native optional package for the host.

- [ ] In `platform.mjs`, add `claudeAlias` and `claudeVersion: "2.1.218"` to the three existing targets. Do not add `darwin-x64`.

- [ ] Implement `resolveClaudeExecutable()` like the existing Codex resolver, but resolve the matching platform package directly and verify its root-level `claude` binary. Do not use `@anthropic-ai/claude-code/bin/claude.exe`, because that wrapper depends on `postinstall`.

```javascript
const metaPath = requireFromMeta.resolve(
  "@anthropic-ai/claude-code/package.json",
);
const meta = readJson(metaPath);
const nativePath = createRequire(metaPath).resolve(
  `${target.claudeAlias}/package.json`,
);
const native = readJson(nativePath);
const executable = realpathSync(join(dirname(nativePath), "claude"));
```

- [ ] Add `claudeExecutable` to `resolveAssets()`, pass `--claude-executable ABSOLUTE_PATH` through `launch.mjs`, and classify `CLAUDE_PACKAGE_INVALID` as exit `78`.

- [ ] Add `claude: "2.1.218"` to the locked build expectations in `scripts/npm/build.mjs` and require `package.json` to carry that exact dependency before producing any artifact.

- [ ] Bump only the npm distribution manifest schema from `1` to `2`. Add required `claude_version: "2.1.218"` and set the platform manifest's `distribution_schema_version` to `2`. Keep the event schema and platform manifest schema unchanged.

- [ ] Add `claude_executable: PathBuf` to `LauncherArgs`, `VerifiedDistribution`, and `ProvisionRequest`. Verify it as a canonical executable, carry it into `AIZIM_CLAUDE_EXECUTABLE`, and strip any inherited value before inserting the verified path.

- [ ] Extend Python `DistributionContext` with redacted `claude_executable`. Add `AIZIM_CLAUDE_EXECUTABLE` to the all-or-none npm environment contract and `without_distribution_environment()`. In source mode use `shutil.which("claude")`; in npm mode never use PATH.

- [ ] Add a stable `claude` doctor check beside `codex`. It must resolve through `DistributionContext`, require exact `2.1.218 (Claude Code)` output, scrub secrets, and use no authentication or model request. Extend source-mode, npm-mode, invalid-project, and stable-check-ID doctor tests.

- [ ] Add `lib/claude.mjs` to the exact meta-package allowlist and assembly copies. Update package contract, staged package, launch argv, distribution manifest, Rust argument, Rust environment, and Python environment tests.

- [ ] Extend install smoke evidence to run the directly resolved native Claude executable with `--version` under `--ignore-scripts` installation and assert the exact output is `2.1.218 (Claude Code)`. Add exact `local_claude_21218` and `global_claude_21218` checks to `write-ci-evidence.mjs` and its exhaustive test fixture. Do not invoke authentication or a model in CI.

- [ ] Run Node, Rust, and Python distribution gates:

```sh
npm run test:node
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --locked
/opt/homebrew/bin/uv run --frozen pytest \
  tests/unit/test_distribution_context.py \
  tests/integration/test_cli_doctor.py -q
/opt/homebrew/bin/uv run --frozen ruff check \
  src/aizim/runtime/distribution.py \
  src/aizim/cli/doctor_command.py \
  tests/unit/test_distribution_context.py \
  tests/integration/test_cli_doctor.py
/opt/homebrew/bin/uv run --frozen ty check
```

Expected result: all commands exit `0`; Node resolver tests prove no lifecycle script or PATH fallback is required.

- [ ] Commit the task:

```sh
git add package.json package-lock.json \
  lib/claude.mjs lib/platform.mjs lib/assets.mjs lib/launch.mjs lib/errors.mjs \
  scripts/npm/build.mjs scripts/npm/assemble.mjs scripts/npm/pack.mjs \
  scripts/npm/install-smoke.mjs scripts/npm/write-ci-evidence.mjs \
  tests/npm/claude-resolution.test.mjs tests/npm/assets.test.mjs \
  tests/npm/launch.test.mjs tests/npm/package-contract.test.mjs \
  tests/npm/package-contents.test.mjs tests/npm/ci-evidence.test.mjs \
  crates/aizim-launcher/src/args.rs \
  crates/aizim-launcher/src/manifest.rs \
  crates/aizim-launcher/src/provision.rs \
  crates/aizim-launcher/src/main.rs \
  crates/aizim-launcher/tests/manifest_contract.rs \
  crates/aizim-launcher/tests/provision_contract.rs \
  src/aizim/runtime/distribution.py \
  src/aizim/cli/doctor_command.py \
  tests/unit/test_distribution_context.py \
  tests/integration/test_cli_doctor.py
git commit -m "Carry the Claude executable through npm"
```

### Task 8: Connect Claude to the shared controller contract

**Files:**

- Create: `src/aizim/orchestration/claude_controller.py`
- Create: `tests/unit/test_claude_controller.py`
- Modify: `src/aizim/orchestration/controller_supervisor.py`
- Modify: `tests/security/test_controller_provider_isolation.py`
- Modify: `tests/integration/test_controller_supervisor.py`
- Modify: `src/aizim/orchestration/__init__.py`

**Interface:**

```python
class ClaudeControllerBackend:
    def __init__(
        self,
        executable: Path,
        model: str | None,
        project_root: Path,
        parent_environment: Mapping[str, str],
        launch: ControllerLauncher = launch_controller_process,
    ) -> None: ...

    @property
    def identity(self) -> BackendIdentity: ...
    async def preflight(self) -> None: ...
    async def plan(self, context: ControllerContext) -> ControllerDecision: ...
```

- [ ] Write failing tests for the exact Claude argv, version/hash pinning, auth-status preflight, structured-output envelope parsing, timeout/reaping, malformed output, and executable replacement.

- [ ] Add a provider parity test that passes the same `ControllerContext` to recording Codex and Claude launchers and asserts their stdin bytes are identical.

- [ ] Run the focused tests and confirm they fail because `claude_controller.py` is absent:

```sh
/opt/homebrew/bin/uv run --frozen pytest \
  tests/unit/test_claude_controller.py -q
```

Expected result: collection fails with `ModuleNotFoundError`.

- [ ] Build the Claude launch spec in the same private empty controller directory and OS sandbox profile as Codex. Use this command shape:

```text
claude -p
  --output-format json
  --json-schema CANONICAL_SCHEMA_JSON
  --safe-mode
  --disable-slash-commands
  --setting-sources ""
  --permission-mode plan
  --no-chrome
  --tools ""
  --disallowedTools "mcp__*"
  --strict-mcp-config
  --no-session-persistence
  --model CONFIGURED_MODEL
```

Omit the two model arguments when the configured controller model is `None`. Never use `bypassPermissions`.

- [ ] Parse only the returned `structured_output` object through `parse_controller_decision()`. Hash and discard stderr and the rest of the output envelope. Never persist a session identifier or raw response.

- [ ] Filter the child environment to locale, temporary-directory, CA, `HOME`, `PATH`, and direct Anthropic authentication variables. Remove every `AIZIM_*` distribution variable and unrelated secret.

- [ ] Implement `preflight()` as exact executable/hash validation, `claude --version` equal to `2.1.218 (Claude Code)`, OS sandbox compilation, and `claude auth status --json`. Discard auth output after checking the exit code.

- [ ] Wire `ControllerProvider.CLAUDE` in the production backend factory. Keep the worker factory Codex-only and continue requiring `AizimConfig.model`.

- [ ] Extend isolation tests to assert both providers lack tools, MCP, canonical project access, state socket access, capability values, and unrelated environment secrets.

- [ ] Run parity, supervisor, and security gates:

```sh
/opt/homebrew/bin/uv run --frozen pytest \
  tests/unit/test_codex_controller.py \
  tests/unit/test_claude_controller.py \
  tests/security/test_controller_provider_isolation.py \
  tests/integration/test_controller_supervisor.py -q
/opt/homebrew/bin/uv run --frozen ruff check \
  src/aizim/orchestration/claude_controller.py \
  src/aizim/orchestration/controller_supervisor.py \
  tests/unit/test_claude_controller.py \
  tests/security/test_controller_provider_isolation.py
/opt/homebrew/bin/uv run --frozen ty check
```

Expected result: all commands exit `0`; provider parity uses byte-identical canonical context.

- [ ] Commit the task:

```sh
git add src/aizim/orchestration/claude_controller.py \
  src/aizim/orchestration/controller_supervisor.py \
  src/aizim/orchestration/__init__.py \
  tests/unit/test_claude_controller.py \
  tests/security/test_controller_provider_isolation.py \
  tests/integration/test_controller_supervisor.py
git commit -m "Connect the Claude controller backend"
```

### Task 9: Expose execution status, document operation, and prove packaged use

**Files:**

- Create: `scripts/qa/controller_smoke.py`
- Create: `tests/e2e/test_real_controller_smoke.py`
- Modify: `src/aizim/cli/control_projection.py`
- Modify: `src/aizim/cli/control_command.py`
- Modify: `README.md`
- Modify: `npm/README.md`
- Modify: `docs/operations/npm-distribution.md`
- Modify: `docs/security/authority-boundary.md`
- Modify: `scripts/npm/install-smoke.mjs`
- Modify: `scripts/npm/write-ci-evidence.mjs`
- Modify: `tests/integration/test_cli_control.py`
- Modify: `tests/npm/install-smoke.test.mjs`
- Modify: `tests/npm/ci-evidence.test.mjs`

**Status JSON:**

```json
{
  "controller_id": "primary",
  "model": "configured-model",
  "provider": "codex",
  "runtime": {
    "backend_version": "codex-cli 0.145.0",
    "controller_session_id": "controller-session-id",
    "controller_version": 1,
    "status": "stopped"
  },
  "version": 1
}
```

Each worker document gains:

```json
{
  "execution": {
    "assignment_id": "sha256",
    "execution_id": "execution-id",
    "reason_code": null,
    "status": "completed"
  }
}
```

- [ ] Add failing CLI projection tests for no runtime, running/stopped/crashed controller states, no execution, claimed/planned/terminal execution states, and malformed projection rejection.

- [ ] Run the focused CLI tests and confirm the new lifecycle fields are absent:

```sh
/opt/homebrew/bin/uv run --frozen pytest \
  tests/integration/test_cli_control.py -q
```

Expected result: the new JSON assertions fail.

- [ ] Join `controller_runtime/primary` into `controller_document()` and join `worker_executions/{assignment_id}` into each current assignment. Expose only fixed status, IDs, controller version, backend version, and reason code; do not expose prompts, task responses, artifact paths, or raw failures.

- [ ] Update human-readable `controller show` and `worker list` output with compact lifecycle/execution status while preserving existing JSON keys and stable ordering.

- [ ] Document source and npm workflows:

```sh
npm install -g @aiz.im/aizim
aizim init /absolute/lean/project
aizim controller configure \
  --project /absolute/lean/project \
  --provider codex \
  --model gpt-5.6-sol
aizim worker register \
  --project /absolute/lean/project \
  --worker-id proof-a \
  --role proof_explorer
aizim worker assign \
  --project /absolute/lean/project \
  --worker-id proof-a \
  --task "prove the current Lean target"
AIZIM_MODEL=gpt-5.6-sol aizim controller start \
  --project /absolute/lean/project \
  --foreground
```

Add the equivalent Claude controller command and state clearly that the worker remains Codex-backed.

- [ ] Add `scripts/qa/controller_smoke.py`. It must use an initialized temporary Lean fixture, call the public control commands, inject deterministic controller/worker backends only at the Python composition boundary, observe one terminal projection, restart, assert no duplicate execution, and print exactly:

```text
CONTROLLER SMOKE PASS
assignment_executions=1
restart_duplicates=0
```

- [ ] Extend `scripts/npm/install-smoke.mjs` to run that smoke script with the managed Python interpreter from the installed wheel, not the repository's uv environment. Add `controller_loop: true` to install evidence and to the exhaustive `INSTALL_CHECK_NAMES` validation in `scripts/npm/write-ci-evidence.mjs`.

- [ ] Add `tests/e2e/test_real_controller_smoke.py` marked `manual_real_controller`. It reads provider, project, controller model, and worker model from environment; skips with `AUTH_UNAVAILABLE` when absent; and otherwise verifies one real terminal assignment for both configured providers through parametrization.

- [ ] Run the deterministic source smoke:

```sh
/opt/homebrew/bin/uv run --frozen python scripts/qa/controller_smoke.py
```

Expected output:

```text
CONTROLLER SMOKE PASS
assignment_executions=1
restart_duplicates=0
```

- [ ] Run the complete Python gates:

```sh
/opt/homebrew/bin/uv run --frozen pytest -m "not manual_real_codex and not manual_real_controller" -q
/opt/homebrew/bin/uv run --frozen ruff check .
/opt/homebrew/bin/uv run --frozen ty check
```

Expected result: all commands exit `0`.

- [ ] Run the complete Node and Rust gates:

```sh
npm run test:node
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --locked
```

Expected result: all commands exit `0`.

- [ ] Commit documentation, status, and smoke changes before building so build evidence names a clean exact commit:

```sh
git add scripts/qa/controller_smoke.py \
  tests/e2e/test_real_controller_smoke.py \
  src/aizim/cli/control_projection.py \
  src/aizim/cli/control_command.py \
  README.md npm/README.md \
  docs/operations/npm-distribution.md \
  docs/security/authority-boundary.md \
  scripts/npm/install-smoke.mjs \
  scripts/npm/write-ci-evidence.mjs \
  tests/integration/test_cli_control.py \
  tests/npm/install-smoke.test.mjs \
  tests/npm/ci-evidence.test.mjs
git commit -m "Document and verify controller operation"
```

- [ ] Build, test, check, and pack from the committed source:

```sh
npm run build
npm test
npm run check
npm run pack
```

Expected result: all commands exit `0`; install smoke prints `CONTROLLER SMOKE PASS`; `dist/npm/pack-summary.json` lists the host platform tarball and `@aiz.im/aizim`.

- [ ] Install the generated platform and meta tarballs into a fresh mode-`0700` temporary consumer directory with `npm install --ignore-scripts`, then run:

```sh
./node_modules/.bin/aizim --version
./node_modules/.bin/aizim doctor --project ./lean-project --json
```

Expected results: version output is `aizim 0.1.0`; doctor JSON reports both exact provider executables available without a global fallback.

- [ ] If real Codex and Claude credentials are available, run:

```sh
/opt/homebrew/bin/uv run --frozen pytest \
  tests/e2e/test_real_controller_smoke.py \
  -m manual_real_controller -q -s
```

Expected result: the selected credentialed provider cases pass. If credentials are absent, preserve the deterministic gates and record the real-provider gate as `SKIPPED_AUTH_UNAVAILABLE`, not as a pass.

## Final Completion Checklist

- [ ] `git status --short --branch` shows only intentional post-build ignored artifacts and no uncommitted tracked source changes.
- [ ] `aizim controller start --project PROJECT --foreground` owns one live state service and accepts live configure/register/assign RPC mutations.
- [ ] Codex and Claude receive byte-identical bounded context and the same validated decision union.
- [ ] Provider readiness fails before `WorkerTaskClaimed`.
- [ ] A claimed assignment reaches exactly one terminal execution state.
- [ ] Restart never reruns a terminal assignment.
- [ ] Restart marks a non-terminal claim interrupted and requires a new task version.
- [ ] Signal cancellation reaps provider and worker children, revokes capability, releases lease, records interruption, checkpoints, and exits.
- [ ] Controller model processes cannot access canonical project state, `.aizim`, state RPC, gateway authority, undeclared tools, or unrelated credentials.
- [ ] `npm install --ignore-scripts` resolves exact package-local Codex and Claude native executables on all three supported targets.
- [ ] `npm run build`, `npm test`, `npm run check`, and `npm run pack` all exit `0`.
- [ ] Deterministic source and installed-wheel controller smoke each report one execution and zero restart duplicates.
- [ ] Credentialed provider QA is either observed passing or explicitly recorded as `SKIPPED_AUTH_UNAVAILABLE`.
