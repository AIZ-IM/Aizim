from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from aizim.config.model import LeanRuntimeMode, ResourcePolicy


class ResourcePolicyError(RuntimeError):
    pass


class ResourceGovernor:
    def __init__(self, policy: ResourcePolicy, *, disk_free: Callable[[Path], int]) -> None:
        if type(policy) is not ResourcePolicy or not callable(disk_free):
            raise ResourcePolicyError("INVALID_RESOURCE_POLICY")
        self._policy = policy
        self._disk_free = disk_free
        self._proof_slots = asyncio.BoundedSemaphore(policy.max_proof_workers)
        self._scratch_slots = asyncio.BoundedSemaphore(policy.scratch_slots)
        self._lsp_slots = asyncio.BoundedSemaphore(policy.lsp_instances)

    @property
    def max_proof_workers(self) -> int:
        return self._policy.max_proof_workers

    @property
    def scratch_slots(self) -> int:
        return self._policy.scratch_slots

    @property
    def lsp_instances(self) -> int:
        return self._policy.lsp_instances

    def validate(self, project_root: Path, mode: LeanRuntimeMode) -> None:
        if not isinstance(project_root, Path) or type(mode) is not LeanRuntimeMode:
            raise ResourcePolicyError("INVALID_RESOURCE_REQUEST")
        if mode is not LeanRuntimeMode.SHARED:
            raise ResourcePolicyError("ISOLATED_RUNTIME_UNAVAILABLE")
        if not 1 <= self.max_proof_workers <= 64:
            raise ResourcePolicyError("PROOF_WORKER_LIMIT")
        if not 1 <= self.scratch_slots <= 64:
            raise ResourcePolicyError("SCRATCH_SLOT_LIMIT")
        if self.lsp_instances != 1:
            raise ResourcePolicyError("LSP_INSTANCE_LIMIT")
        if self._disk_free(project_root) < self._policy.min_free_disk_bytes:
            raise ResourcePolicyError("DISK_FLOOR_NOT_MET")

    @asynccontextmanager
    async def proof_slot(self) -> AsyncIterator[None]:
        async with self._proof_slots, self._scratch_slots:
            yield

    @asynccontextmanager
    async def lsp_slot(self) -> AsyncIterator[None]:
        async with self._lsp_slots:
            yield
