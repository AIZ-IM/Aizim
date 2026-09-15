from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aizim.agents import ViewBuildError
from aizim.agents.codex_backend import CodexBackendError
from aizim.agents.launcher import AgentLaunchError
from aizim.config.model import LeanRuntimeMode, ResourcePolicy
from aizim.orchestration.resources import ResourceGovernor, ResourcePolicyError


def test_shared_resource_policy_requires_slice_two_limits(tmp_path: Path) -> None:
    governor = ResourceGovernor(ResourcePolicy(), disk_free=lambda _path: 3_000_000_000)

    governor.validate(tmp_path, LeanRuntimeMode.SHARED)

    assert governor.max_proof_workers == 2
    assert governor.scratch_slots == 2
    assert governor.lsp_instances == 1


@pytest.mark.parametrize(
    ("policy", "mode", "code"),
    [
        (ResourcePolicy(max_proof_workers=0), LeanRuntimeMode.SHARED, "PROOF_WORKER_LIMIT"),
        (ResourcePolicy(scratch_slots=0), LeanRuntimeMode.SHARED, "SCRATCH_SLOT_LIMIT"),
        (ResourcePolicy(lsp_instances=2), LeanRuntimeMode.SHARED, "LSP_INSTANCE_LIMIT"),
        (ResourcePolicy(), LeanRuntimeMode.ISOLATED, "ISOLATED_RUNTIME_UNAVAILABLE"),
    ],
)
def test_resource_policy_rejects_unsupported_shared_requests(
    tmp_path: Path, policy: ResourcePolicy, mode: LeanRuntimeMode, code: str
) -> None:
    governor = ResourceGovernor(policy, disk_free=lambda _path: 3_000_000_000)

    with pytest.raises(ResourcePolicyError, match=code):
        governor.validate(tmp_path, mode)


def test_resource_policy_rejects_low_disk_before_starting(tmp_path: Path) -> None:
    governor = ResourceGovernor(ResourcePolicy(), disk_free=lambda _path: 1)

    with pytest.raises(ResourcePolicyError, match="DISK_FLOOR_NOT_MET"):
        governor.validate(tmp_path, LeanRuntimeMode.SHARED)


@pytest.mark.asyncio
async def test_proof_slots_bound_concurrent_workers() -> None:
    governor = ResourceGovernor(ResourcePolicy(), disk_free=lambda _path: 3_000_000_000)
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0
    peak = 0

    async def occupy() -> None:
        nonlocal active, peak
        async with governor.proof_slot():
            active += 1
            peak = max(peak, active)
            if active == 2:
                entered.set()
            await release.wait()
            active -= 1

    first, second, third = (asyncio.create_task(occupy()) for _ in range(3))
    await entered.wait()
    await asyncio.sleep(0)
    assert peak == 2
    release.set()
    await asyncio.gather(first, second, third)


@pytest.mark.parametrize(
    "error",
    (
        AgentLaunchError("CODEX_PROCESS_FAILED"),
        CodexBackendError("CODEX_IMAGE_CHANGED"),
        ViewBuildError("workspace view construction failed"),
    ),
)
async def test_proof_slot_preserves_backend_error(error: RuntimeError) -> None:
    governor = ResourceGovernor(ResourcePolicy(), disk_free=lambda _path: 3_000_000_000)

    with pytest.raises(type(error), match=str(error)):
        async with governor.proof_slot():
            raise error
