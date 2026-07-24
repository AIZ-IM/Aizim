from __future__ import annotations

import asyncio  # noqa: ANYIO_OK - exercises the asyncio supervisor lifecycle
import shutil
from collections.abc import Iterator
from itertools import count
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar

import pytest

import aizim.cli.controller_command as controller_command
import aizim.cli.main as cli_main
from aizim.agents import AgentRequest, AgentResult, BackendIdentity
from aizim.cli.init_command import run_init
from aizim.domain import AgentRole, sha256_bytes, sha256_json
from aizim.orchestration.control_plane import (
    ControllerProvider,
    assign_task,
    configure_controller,
    register_worker,
)
from aizim.orchestration.controller_backend import (
    BlockedDecision,
    ControllerContext,
    DispatchDecision,
)
from aizim.orchestration.controller_execution import (
    ControllerExecutionError,
    claim_assignment,
    record_dispatch_planned,
)
from aizim.orchestration.controller_lifecycle import start_controller
from aizim.orchestration.controller_supervisor import (
    ControllerSupervisor,
    ControllerSupervisorDependencies,
)
from aizim.orchestration.fake_controller_backend import FakeControllerBackend
from aizim.state import StateService, StateServiceConfig

SMOKE_ROOT = Path(__file__).parents[2] / "examples" / "smoke_lean"


@pytest.fixture
def short_tmp(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("AIZIM_MODEL", "worker-model")
    with TemporaryDirectory(prefix="aizim-controller-cli-", dir="/tmp") as directory:
        yield Path(directory)


class RecordingSubmittedBackend:
    @property
    def identity(self) -> BackendIdentity:
        return BackendIdentity("fake", "deterministic-v1", None)

    async def run(self, request: AgentRequest) -> AgentResult:
        return AgentResult(request.worker_id, "submitted", "done", "a" * 64, "b" * 64, 0)


def initialized_assignment(tmp_path: Path) -> tuple[Path, str]:
    root = Path(
        shutil.copytree(
            SMOKE_ROOT,
            tmp_path / "lean-project",
            ignore=shutil.ignore_patterns(".aizim"),
        )
    )
    assert run_init(root) == 0
    with StateService(StateServiceConfig(root, "setup-session")) as state:
        configure_controller(state, ControllerProvider.CODEX, "controller-model")
        register_worker(state, "proof-a", AgentRole.FORMALIZER)
        assign_task(state, "proof-a", "prove True")
    assignment_id = sha256_json(
        {
            "controller_id": "primary",
            "worker_id": "proof-a",
            "task_hash": sha256_bytes(b"prove True"),
            "task_version": 1,
        }
    )
    return root, assignment_id


def dependencies(
    controller: FakeControllerBackend,
    worker: RecordingSubmittedBackend,
) -> ControllerSupervisorDependencies:
    return ControllerSupervisorDependencies(
        controller_backend=lambda _provider, _model: controller,
        worker_backend=lambda: worker,
        worker_preflight=lambda: asyncio.sleep(0),
        session_ids=(f"controller-{value:032x}" for value in count(1)).__next__,
        execution_ids=(f"execution-{value:032x}" for value in count(1)).__next__,
        directive_ids=(f"directive-{value:032x}" for value in count(1)).__next__,
    )


class ImmediateSupervisor:
    failure: BaseException | None = None
    projects: ClassVar[list[Path]] = []

    def __init__(self, project: Path) -> None:
        self.projects.append(project)

    async def run(self) -> None:
        failure = self.failure
        if failure is not None:
            raise failure

    def request_stop(self) -> None:
        return None


def test_controller_start_requires_foreground(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # When
    result = cli_main.main(["controller", "start", "--project", str(tmp_path)])

    # Then
    assert result == 2
    assert capsys.readouterr().err == ("aizim controller start: --foreground is required\n")


def test_controller_start_runs_supervisor_in_foreground(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    ImmediateSupervisor.failure = None
    ImmediateSupervisor.projects.clear()
    monkeypatch.setattr(controller_command, "ControllerSupervisor", ImmediateSupervisor)

    # When
    result = controller_command.run_controller_start(tmp_path, True)

    # Then
    assert result == 0
    assert ImmediateSupervisor.projects == [tmp_path]


@pytest.mark.parametrize(
    ("failure", "exit_code"),
    [
        (ControllerExecutionError("CONTROLLER_NOT_CONFIGURED"), 4),
        (RuntimeError("secret provider output"), 6),
    ],
)
def test_controller_start_scrubs_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: BaseException,
    exit_code: int,
) -> None:
    # Given
    ImmediateSupervisor.failure = failure
    monkeypatch.setattr(controller_command, "ControllerSupervisor", ImmediateSupervisor)

    # When
    result = controller_command.run_controller_start(tmp_path, True)

    # Then
    assert result == exit_code
    assert capsys.readouterr().err == "aizim controller start: controller failed\n"


async def test_stop_interrupts_active_plan_and_releases_runtime(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    root, assignment_id = initialized_assignment(short_tmp)
    controller = FakeControllerBackend(BlockedDecision("blocked", "NO_SAFE_ACTION"))
    entered = asyncio.Event()

    async def block_plan(_context: ControllerContext) -> BlockedDecision:
        entered.set()
        return await asyncio.Future()

    monkeypatch.setattr(controller, "plan", block_plan)
    supervisor = ControllerSupervisor(root, dependencies(controller, RecordingSubmittedBackend()))

    # When
    task = asyncio.create_task(supervisor.run())
    await entered.wait()
    supervisor.request_stop()
    await task

    # Then
    with StateService(StateServiceConfig(root, "inspect")) as state:
        execution = state.query_projection("worker_executions", assignment_id)
        assert execution is not None
        assert b'"status":"interrupted"' in execution.state_json
        assert b'"reason_code":"OPERATOR_SIGNAL"' in execution.state_json
    assert not (root / ".aizim/run/state.pid").exists()
    assert not (root / ".aizim/run/state.sock").exists()


async def test_startup_recovers_unclean_controller_and_planned_execution(
    short_tmp: Path,
) -> None:
    # Given
    root, assignment_id = initialized_assignment(short_tmp)
    old_session = f"controller-{'0' * 32}"
    with StateService(StateServiceConfig(root, "unclean-setup")) as state:
        start_controller(
            state,
            session_id=old_session,
            controller_version=1,
            provider=ControllerProvider.CODEX,
            backend_version="old-controller",
            executable_hash="a" * 64,
        )
        claim = claim_assignment(
            state,
            worker_id="proof-a",
            controller_session_id=old_session,
            controller_version=1,
            execution_id=f"execution-{'0' * 32}",
        )
        record_dispatch_planned(
            state,
            claim,
            directive_id=f"directive-{'0' * 32}",
            directive_artifact_hash="b" * 64,
            instruction_hash="c" * 64,
            budget=1,
            timeout_milliseconds=1_000,
        )
    controller = FakeControllerBackend(
        DispatchDecision("dispatch", "proof-a", "unreachable", 1, 1.0)
    )
    supervisor = ControllerSupervisor(root, dependencies(controller, RecordingSubmittedBackend()))

    # When
    supervisor.request_stop()
    await supervisor.run()

    # Then
    with StateService(StateServiceConfig(root, "inspect")) as state:
        execution = state.query_projection("worker_executions", assignment_id)
        assert execution is not None
        assert b'"reason_code":"CONTROLLER_RESTART"' in execution.state_json
        event_types = tuple(item.envelope.event_type for item in state.query_events())
        assert "ControllerCrashed" in event_types


async def test_missing_worker_model_fails_before_claim(
    short_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given
    root, assignment_id = initialized_assignment(short_tmp)
    monkeypatch.delenv("AIZIM_MODEL")
    controller = FakeControllerBackend(BlockedDecision("blocked", "NO_SAFE_ACTION"))

    # When / Then
    with pytest.raises(ControllerExecutionError, match="WORKER_MODEL_REQUIRED"):
        await ControllerSupervisor(
            root, dependencies(controller, RecordingSubmittedBackend())
        ).run()
    with StateService(StateServiceConfig(root, "inspect")) as state:
        assert state.query_projection("worker_executions", assignment_id) is None
