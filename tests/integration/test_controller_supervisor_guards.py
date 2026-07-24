from __future__ import annotations

import asyncio  # noqa: ANYIO_OK - exercises the asyncio supervisor lifecycle
import shutil
from collections.abc import Iterator
from dataclasses import replace
from itertools import count
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

import aizim.orchestration.controller_supervisor as supervisor_module
from aizim.agents import AgentRequest, AgentResult, BackendIdentity
from aizim.cli.init_command import run_init
from aizim.domain import AgentRole, sha256_bytes, sha256_json
from aizim.domain.serialization import JsonValue
from aizim.lean.project import smoke_base_epoch
from aizim.orchestration.control_plane import (
    ControllerProvider,
    assign_task,
    configure_controller,
    register_worker,
)
from aizim.orchestration.controller_backend import BlockedDecision
from aizim.orchestration.controller_execution import ControllerExecutionError
from aizim.orchestration.controller_supervisor import (
    ControllerSupervisor,
    ControllerSupervisorDependencies,
    _default_dependencies,
)
from aizim.orchestration.fake_controller_backend import FakeControllerBackend
from aizim.runtime.layout import ProjectLayout
from aizim.state import AppendEventCommand, StateService, StateServiceConfig

SMOKE_ROOT = Path(__file__).parents[2] / "examples" / "smoke_lean"


@pytest.fixture
def short_tmp(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("AIZIM_MODEL", "worker-model")
    with TemporaryDirectory(prefix="aizim-controller-guards-", dir="/tmp") as directory:
        yield Path(directory)


class UnusedWorker:
    @property
    def identity(self) -> BackendIdentity:
        return BackendIdentity("fake", "deterministic-v1", None)

    async def run(self, request: AgentRequest) -> AgentResult:
        raise AssertionError(f"worker unexpectedly launched: {request.worker_id}")


def initialized(tmp_path: Path, identities: int) -> tuple[Path, str]:
    root = Path(
        shutil.copytree(
            SMOKE_ROOT,
            tmp_path / "lean-project",
            ignore=shutil.ignore_patterns(".aizim"),
        )
    )
    if identities:
        assert run_init(root) == 0
    else:
        ProjectLayout.from_lean_project(root).prepare_runtime()
    with StateService(StateServiceConfig(root, "setup-session")) as state:
        for index in range(1, identities):
            payload: dict[str, JsonValue] = {
                "project_id": f"duplicate-{index}",
                "base_epoch": smoke_base_epoch(root),
                "knowledge_epoch": 0,
            }
            state.append_event(
                AppendEventCommand("ProjectInitialized", "supervisor", None, None, payload)
            )
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


def dependencies(controller: FakeControllerBackend) -> ControllerSupervisorDependencies:
    worker = UnusedWorker()
    return ControllerSupervisorDependencies(
        controller_backend=lambda _provider, _model: controller,
        worker_backend=lambda: worker,
        worker_preflight=lambda: asyncio.sleep(0),
        session_ids=(f"controller-{value:032x}" for value in count(1)).__next__,
        execution_ids=(f"execution-{value:032x}" for value in count(1)).__next__,
        directive_ids=(f"directive-{value:032x}" for value in count(1)).__next__,
    )


@pytest.mark.parametrize(
    ("guard", "code"),
    [
        ("model", "WORKER_MODEL_REQUIRED"),
        ("identity", "PROJECT_IDENTITY_UNAVAILABLE"),
        ("duplicate", "PROJECT_IDENTITY_INVALID"),
        ("epoch", "PROJECT_EPOCH_INVALID"),
        ("worker_preflight", "WORKER_PREFLIGHT_UNAVAILABLE"),
        ("checkpoint", "CHECKPOINT_FAILED"),
        ("execution", "TRUSTED_ID_INVALID"),
        ("directive", "TRUSTED_ID_INVALID"),
    ],
)
async def test_guards_precede_claim_and_cleanup(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    guard: str,
    code: str,
) -> None:
    identities = 0 if guard == "identity" else 2 if guard == "duplicate" else 1
    root, assignment_id = initialized(short_tmp, identities)
    controller = FakeControllerBackend(BlockedDecision("blocked", "NO_SAFE_ACTION"))
    injected = dependencies(controller)
    if guard == "model":
        monkeypatch.delenv("AIZIM_MODEL")
    elif guard == "worker_preflight":
        injected = replace(
            _default_dependencies(),
            controller_backend=injected.controller_backend,
            worker_backend=injected.worker_backend,
        )
        monkeypatch.setattr(
            supervisor_module,
            "record_controller_stop",
            lambda *_args: (_ for _ in ()).throw(ControllerExecutionError("STOP_FAILED")),
        )
    elif guard == "checkpoint":
        monkeypatch.setattr(
            StateService,
            "checkpoint",
            lambda *_args: (_ for _ in ()).throw(ControllerExecutionError("CHECKPOINT_FAILED")),
        )
    elif guard == "epoch":
        invalid = supervisor_module.ControllerRun(0, "", "", "invalid", 0)
        monkeypatch.setattr(supervisor_module, "current_epoch", lambda _state: invalid)
    elif guard in {"execution", "directive"}:
        injected = replace(injected, **{f"{guard}_ids": lambda: "invalid"})
    supervisor = ControllerSupervisor(root, injected)
    if guard not in {"execution", "directive"}:
        supervisor.request_stop()

    with pytest.raises(ControllerExecutionError, match=code):
        await supervisor.run()
    with StateService(StateServiceConfig(root, "inspect")) as state:
        assert state.query_projection("worker_executions", assignment_id) is None
    if guard in {"worker_preflight", "checkpoint"}:
        probe = StateService(StateServiceConfig(root, "probe"))
        await probe.start()
        await probe.aclose()
    assert not tuple((root / ".aizim/run").rglob("*.sock"))
