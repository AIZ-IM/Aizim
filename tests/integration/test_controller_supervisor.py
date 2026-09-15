from __future__ import annotations

import asyncio  # noqa: ANYIO_OK - exercises the asyncio supervisor contract
import shutil
from collections.abc import Iterator
from dataclasses import replace
from itertools import count
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

import pytest

from aizim.agents import AgentRequest, AgentResult, BackendIdentity
from aizim.domain import (
    AgentRole,
    ControllerProviderId,
    canonical_json,
    sha256_bytes,
    sha256_json,
)
from aizim.orchestration.control_plane import (
    assign_task,
    configure_controller,
    register_worker,
)
from aizim.orchestration.controller_backend import (
    BlockedDecision,
    ControllerBackendError,
    ControllerContext,
    DispatchDecision,
    RejectDecision,
)
from aizim.orchestration.controller_execution import ControllerExecutionError
from aizim.orchestration.controller_providers import ResolvedControllerRuntime
from aizim.orchestration.controller_supervisor import (
    ControllerSupervisor,
    ControllerSupervisorDependencies,
)
from aizim.orchestration.fake_controller_backend import FakeControllerBackend
from aizim.runtime.provider_executables import ResolvedExecutable
from aizim.state import StateService, StateServiceConfig
from aizim.state.operations import RpcRequest, RpcSuccess
from aizim.state.rpc import rpc_call

SMOKE_ROOT = Path(__file__).parents[2] / "examples" / "smoke_lean"
INVALID = "CONTROLLER_DECISION_INVALID"
OVER_BUDGET = DispatchDecision("dispatch", "proof-a", "too much", 13, 1.0)


@pytest.fixture
def short_tmp(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("AIZIM_MODEL", "worker-model")
    with TemporaryDirectory(prefix="aizim-controller-", dir="/tmp") as directory:
        yield Path(directory)


class RecordingSubmittedBackend:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.requests: list[AgentRequest] = []

    @property
    def identity(self) -> BackendIdentity:
        return BackendIdentity("fake", "deterministic-v1", None)

    async def run(self, request: AgentRequest) -> AgentResult:
        assert tuple(
            (self.root / ".aizim/artifacts" / request.run_id / "controller-directive").iterdir()
        )
        response = await rpc_call(
            self.root / ".aizim/run/state.sock",
            RpcRequest(
                "query_projections",
                {"projection_name": "worker_executions"},
                None,
            ),
        )
        assert isinstance(response, RpcSuccess)
        assert b'"status":"planned"' in canonical_json(response.result)
        self.requests.append(request)
        return AgentResult(request.worker_id, "submitted", "done", "a" * 64, "b" * 64, 0)


def initialized_assignment(tmp_path: Path, *, assigned: bool = True) -> tuple[Path, str]:
    root = Path(
        shutil.copytree(
            SMOKE_ROOT,
            tmp_path / "lean-project",
            ignore=shutil.ignore_patterns(".aizim"),
        )
    )
    from aizim.cli.init_command import run_init

    assert run_init(root) == 0
    with StateService(StateServiceConfig(root, "setup-session")) as state:
        configure_controller(state, ControllerProviderId("claude"), "controller-model")
        register_worker(state, "proof-a", AgentRole.FORMALIZER)
        if assigned:
            assign_task(state, "proof-a", "prove True")
        else:
            return root, ""
    return root, _assignment_id()


def _assignment_id() -> str:
    return sha256_json(
        {
            "controller_id": "primary",
            "worker_id": "proof-a",
            "task_hash": sha256_bytes(b"prove True"),
            "task_version": 1,
        }
    )


async def wait_for_status(
    root: Path,
    assignment_id: str,
    expected: str,
    reason: str | None = None,
) -> None:
    for _attempt in range(2_000):
        try:
            response = await rpc_call(
                root / ".aizim/run/state.sock",
                RpcRequest(
                    "query_projection",
                    {"projection_name": "worker_executions", "entity_id": assignment_id},
                    None,
                ),
            )
        except OSError:
            await asyncio.sleep(0)
            continue
        if isinstance(response, RpcSuccess) and type(response.result) is dict:
            document = response.result.get("state")
            if type(document) is dict:
                payload = document.get("payload")
                if type(payload) is dict and payload.get("status") == expected:
                    assert reason is None or payload.get("reason_code") == reason
                    return
        await asyncio.sleep(0)
    pytest.fail(f"execution did not reach {expected}")


def dependencies(
    controller: FakeControllerBackend,
    worker: RecordingSubmittedBackend,
    observed: list[tuple[str, object]] | None = None,
) -> ControllerSupervisorDependencies:
    codex = ResolvedExecutable(
        Path("/opt/aizim-test/codex"),
        "codex-cli 0.154.0",
        "a" * 64,
    )
    selected = ResolvedExecutable(
        Path("/opt/aizim-test/claude"),
        "2.1.218 (Claude Code)",
        "b" * 64,
    )

    def resolve_runtime(provider: ControllerProviderId) -> ResolvedControllerRuntime:
        runtime = ResolvedControllerRuntime(provider, codex, selected)
        if observed is not None:
            observed.append(("resolve", runtime))
        return runtime

    def controller_backend(
        runtime: ResolvedControllerRuntime,
        _model: str | None,
    ) -> FakeControllerBackend:
        if observed is not None:
            observed.append(("controller", runtime))
        return controller

    def worker_backend(executable: ResolvedExecutable) -> RecordingSubmittedBackend:
        if observed is not None:
            observed.append(("worker", executable))
        return worker

    async def worker_preflight(executable: ResolvedExecutable) -> None:
        if observed is not None:
            observed.append(("preflight", executable))

    return ControllerSupervisorDependencies(
        resolve_runtime=resolve_runtime,
        controller_backend=controller_backend,
        worker_backend=worker_backend,
        worker_preflight=worker_preflight,
        session_ids=(f"controller-{value:032x}" for value in count(1)).__next__,
        execution_ids=(f"execution-{value:032x}" for value in count(1)).__next__,
        directive_ids=(f"directive-{value:032x}" for value in count(1)).__next__,
    )


async def test_supervisor_injects_one_runtime_snapshot_into_every_role(
    short_tmp: Path,
) -> None:
    root, _assignment_id = initialized_assignment(short_tmp)
    controller = FakeControllerBackend(BlockedDecision("blocked", "NO_SAFE_ACTION"))
    observed: list[tuple[str, object]] = []
    supervisor = ControllerSupervisor(
        root,
        dependencies(
            controller,
            RecordingSubmittedBackend(root),
            observed,
        ),
    )

    supervisor.request_stop()
    await supervisor.run()

    runtime = cast(ResolvedControllerRuntime, observed[0][1])
    assert type(runtime) is ResolvedControllerRuntime
    assert observed == [
        ("resolve", runtime),
        ("controller", runtime),
        ("preflight", runtime.codex),
        ("worker", runtime.codex),
    ]


async def test_completed_assignment_is_not_dispatched_after_restart(
    short_tmp: Path,
) -> None:
    # Given
    root, assignment_id = initialized_assignment(short_tmp)
    controller = FakeControllerBackend(
        DispatchDecision("dispatch", "proof-a", "Use the assigned document.", 3, 15.0)
    )
    worker = RecordingSubmittedBackend(root)
    injected = dependencies(controller, worker)
    first = ControllerSupervisor(root, injected)

    # When
    first_run = asyncio.create_task(first.run())
    await wait_for_status(root, assignment_id, "completed")
    first.request_stop()
    await first_run
    second = ControllerSupervisor(root, injected)
    second.request_stop()
    await second.run()

    # Then
    assert len(controller.received_context_bytes) == 1
    assert [request.model for request in worker.requests] == ["worker-model"]


async def test_live_control_assignment_wakes_idle_supervisor(
    short_tmp: Path,
) -> None:
    # Given
    root, _unused = initialized_assignment(short_tmp, assigned=False)
    controller = FakeControllerBackend(
        DispatchDecision("dispatch", "proof-a", "Use the assigned document.", 3, 15.0)
    )
    supervisor = ControllerSupervisor(
        root, dependencies(controller, RecordingSubmittedBackend(root))
    )
    task = asyncio.create_task(supervisor.run())
    assignment_id = _assignment_id()

    # When
    await supervisor._idle.wait()
    socket = root / ".aizim/run/state.sock"
    response = await rpc_call(
        socket,
        RpcRequest("control.assign_task", {"worker_id": "proof-a", "task": "prove True"}, None),
    )
    assert isinstance(response, RpcSuccess)
    await wait_for_status(root, assignment_id, "completed")
    supervisor.request_stop()
    await task

    # Then
    assert len(controller.received_context_bytes) == 1


@pytest.mark.parametrize(
    ("decision", "reason"),
    [
        (BlockedDecision("blocked", "NO_SAFE_ACTION"), "CONTROLLER_BLOCKED"),
        (RejectDecision("reject", "TASK_UNSAFE"), "CONTROLLER_REJECTED"),
        (ControllerBackendError(INVALID), INVALID),
        (TimeoutError(), "CONTROLLER_TIMEOUT"),
        (OVER_BUDGET, INVALID),
        (DispatchDecision("dispatch", "proof-a", "too long", 1, 61.0), INVALID),
    ],
)
async def test_non_dispatch_decisions_never_launch_worker(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    decision: BlockedDecision | DispatchDecision | RejectDecision | BaseException,
    reason: str,
) -> None:
    # Given
    root, assignment_id = initialized_assignment(short_tmp)
    worker = RecordingSubmittedBackend(root)
    fallback = BlockedDecision("blocked", "NO_SAFE_ACTION")
    controller = FakeControllerBackend(
        fallback if isinstance(decision, BaseException) else decision
    )
    if isinstance(decision, BaseException):

        async def fail_plan(_context: ControllerContext) -> BlockedDecision:
            raise decision

        monkeypatch.setattr(controller, "plan", fail_plan)
    supervisor = ControllerSupervisor(root, dependencies(controller, worker))

    # When
    task = asyncio.create_task(supervisor.run())
    await wait_for_status(root, assignment_id, "failed", reason)
    supervisor.request_stop()
    await task

    # Then
    assert worker.requests == []


@pytest.mark.parametrize(
    "guard,expected", [("provider", ControllerBackendError), ("stale", ControllerExecutionError)]
)
async def test_startup_guard_happens_before_claim(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    guard: str,
    expected: type[Exception],
) -> None:
    # Given
    root, assignment_id = initialized_assignment(short_tmp)
    controller = FakeControllerBackend(
        DispatchDecision("dispatch", "proof-a", "unreachable", 1, 1.0)
    )
    injected = dependencies(controller, RecordingSubmittedBackend(root))

    async def guard_preflight(_executable: ResolvedExecutable | None = None) -> None:
        if guard == "provider":
            raise ControllerBackendError("CONTROLLER_DECISION_INVALID")
        result = await rpc_call(
            root / ".aizim/run/state.sock",
            RpcRequest(
                "control.configure_controller",
                {"provider": "codex", "model": "new-controller"},
                None,
            ),
        )
        assert isinstance(result, RpcSuccess)

    if guard == "provider":
        monkeypatch.setattr(controller, "preflight", guard_preflight)
    else:
        injected = replace(injected, worker_preflight=guard_preflight)

    # When / Then
    with pytest.raises(expected):
        await ControllerSupervisor(root, injected).run()
    with StateService(StateServiceConfig(root, "inspect")) as state:
        assert state.query_projection("worker_executions", assignment_id) is None
        assert state.query_projection("controller_runtime", "primary") is None
        assert "ControllerStarted" not in {
            record.envelope.event_type for record in state.query_events()
        }
