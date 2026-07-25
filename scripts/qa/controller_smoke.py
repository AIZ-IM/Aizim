# /// script
# requires-python = ">=3.14"
# dependencies = []
# ///
# ─── How to run ───
# /opt/homebrew/bin/uv run --frozen python scripts/qa/controller_smoke.py

from __future__ import annotations

import asyncio  # noqa: ANYIO_OK
import hashlib
import json
import os
import shutil
import subprocess
import sys
from itertools import count
from pathlib import Path
from tempfile import TemporaryDirectory

from aizim.agents import AgentRequest, AgentResult, BackendIdentity
from aizim.domain import ControllerProviderId, sha256_json
from aizim.domain.serialization import JsonValue
from aizim.orchestration.controller_backend import DispatchDecision
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

REPOSITORY_ROOT = Path(__file__).parents[2]
FIXTURE = REPOSITORY_ROOT / "examples" / "smoke_lean"


class ControllerSmokeError(RuntimeError):
    pass


class RecordingWorkerBackend:
    def __init__(self) -> None:
        self.requests: list[AgentRequest] = []

    @property
    def identity(self) -> BackendIdentity:
        return BackendIdentity("fake", "deterministic-worker-v1", None)

    async def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        return AgentResult(
            request.worker_id,
            "submitted",
            "deterministic completion",
            "a" * 64,
            "b" * 64,
            0,
        )


def _run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "aizim", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def _require_cli(label: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    result = _run_cli(*arguments)
    if result.returncode != 0:
        raise ControllerSmokeError(label)
    return result


def _initialize_project(directory: Path) -> tuple[Path, str]:
    root = Path(
        shutil.copytree(
            FIXTURE,
            directory / "lean-project",
            ignore=shutil.ignore_patterns(".aizim", ".lake"),
        )
    )
    _require_cli("INIT_FAILED", "init", str(root))
    _require_cli(
        "CONFIGURE_FAILED",
        "controller",
        "configure",
        "--project",
        str(root),
        "--provider",
        "codex",
        "--model",
        "deterministic-controller",
    )
    _require_cli(
        "REGISTER_FAILED",
        "worker",
        "register",
        "--project",
        str(root),
        "--worker-id",
        "proof-a",
        "--role",
        "formalizer",
    )
    _require_cli(
        "ASSIGN_FAILED",
        "worker",
        "assign",
        "--project",
        str(root),
        "--worker-id",
        "proof-a",
        "--task",
        "prove the deterministic smoke target",
    )
    assignment_id = sha256_json(
        {
            "controller_id": "primary",
            "worker_id": "proof-a",
            "task_hash": hashlib.sha256(b"prove the deterministic smoke target").hexdigest(),
            "task_version": 1,
        }
    )
    return root, assignment_id


def _dependencies(
    controller: FakeControllerBackend,
    worker: RecordingWorkerBackend,
) -> ControllerSupervisorDependencies:
    sessions = (f"controller-{value:032x}" for value in count(1))
    executions = (f"execution-{value:032x}" for value in count(1))
    directives = (f"directive-{value:032x}" for value in count(1))
    executable = ResolvedExecutable(
        Path(sys.executable).resolve(strict=True),
        "codex-cli 0.145.0",
        "0" * 64,
    )
    runtime = ResolvedControllerRuntime(
        ControllerProviderId("codex"),
        executable,
        executable,
    )

    async def worker_preflight(_executable: ResolvedExecutable) -> None:
        return None

    return ControllerSupervisorDependencies(
        resolve_runtime=lambda _provider: runtime,
        controller_backend=lambda _runtime, _model: controller,
        worker_backend=lambda _executable: worker,
        worker_preflight=worker_preflight,
        session_ids=sessions.__next__,
        execution_ids=executions.__next__,
        directive_ids=directives.__next__,
    )


async def _wait_for_terminal(root: Path, assignment_id: str) -> None:
    for _attempt in range(2_000):
        try:
            response = await rpc_call(
                root / ".aizim/run/state.sock",
                RpcRequest(
                    "query_projection",
                    {
                        "projection_name": "worker_executions",
                        "entity_id": assignment_id,
                    },
                    None,
                ),
            )
        except OSError:
            await asyncio.sleep(0)
            continue
        if isinstance(response, RpcSuccess) and _completed(response.result):
            return
        await asyncio.sleep(0)
    raise ControllerSmokeError("TERMINAL_PROJECTION_UNAVAILABLE")


def _completed(value: JsonValue) -> bool:
    if type(value) is not dict:
        return False
    state = value.get("state")
    if type(state) is not dict:
        return False
    payload = state.get("payload")
    return type(payload) is dict and payload.get("status") == "completed"


async def _exercise(root: Path, assignment_id: str) -> tuple[int, int]:
    controller = FakeControllerBackend(
        DispatchDecision(
            "dispatch",
            "proof-a",
            "Complete the deterministic smoke assignment.",
            1,
            5.0,
        )
    )
    worker = RecordingWorkerBackend()
    dependencies = _dependencies(controller, worker)
    first = ControllerSupervisor(root, dependencies)
    active = asyncio.create_task(first.run())
    await _wait_for_terminal(root, assignment_id)
    first.request_stop()
    await active

    restarted = ControllerSupervisor(root, dependencies)
    restarted.request_stop()
    await restarted.run()

    listed = _require_cli("STATUS_FAILED", "worker", "list", "--project", str(root), "--json")
    if not _public_completed(listed.stdout, assignment_id):
        raise ControllerSmokeError("PUBLIC_PROJECTION_INVALID")
    with StateService(StateServiceConfig(root, "smoke-observer")) as state:
        executions = state.projections("worker_executions")
    if len(executions) != 1:
        raise ControllerSmokeError("EXECUTION_COUNT_INVALID")
    return len(worker.requests), len(controller.received_context_bytes) - 1


def _public_completed(raw: str, assignment_id: str) -> bool:
    try:
        value: JsonValue = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if type(value) is not dict:
        return False
    workers = value.get("workers")
    if type(workers) is not list or len(workers) != 1 or type(workers[0]) is not dict:
        return False
    assignment = workers[0].get("assignment")
    if type(assignment) is not dict or assignment.get("assignment_id") != assignment_id:
        return False
    execution = assignment.get("execution")
    return type(execution) is dict and execution.get("status") == "completed"


def _main() -> int:
    os.environ["AIZIM_MODEL"] = "deterministic-worker"
    try:
        with TemporaryDirectory(prefix="aizim-controller-smoke-", dir="/tmp") as directory:
            root, assignment_id = _initialize_project(Path(directory))
            executions, duplicates = asyncio.run(_exercise(root, assignment_id))
        if (executions, duplicates) != (1, 0):
            raise ControllerSmokeError("DISPATCH_COUNT_INVALID")
    except Exception:  # noqa: BROAD_EXCEPT_OK -- top-level smoke boundary
        print("CONTROLLER_SMOKE_FAILED", file=sys.stderr)
        return 1
    print("CONTROLLER SMOKE PASS")
    print("assignment_executions=1")
    print("restart_duplicates=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
