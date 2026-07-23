from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .mcp_client import LeanMcpClient
from .models import DiagnosticsResult
from .verification import BuildResult, VerificationResult

type EnsureClient = Callable[[Path], Awaitable[LeanMcpClient]]

_DIAGNOSTICS_TIMEOUT_SECONDS = 60
_DIAGNOSTICS_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class PromotionCheck:
    diagnostics: DiagnosticsResult
    build: BuildResult
    verification: VerificationResult
    type_info: str


class PromotionRuntimeMethods:
    async def _promotion_check(
        self, project_root: Path, module_path: Path, theorem_name: str, probe_path: Path
    ) -> PromotionCheck:
        client = await self._promotion_client(project_root)
        async with self._promotion_lock():
            diagnostics = await _complete_diagnostics(client, module_path)
            build = await client.build(clean=False, fetch_cache=False)
            verification = await client.verify(module_path, theorem_name, scan_source=True)
            type_info = await client.hover(probe_path, 2, 8)
        return PromotionCheck(diagnostics, build, verification, type_info)

    def _promotion_lock(self) -> asyncio.Lock:
        return cast(asyncio.Lock, object.__getattribute__(self, "_lifecycle_lock"))

    async def _promotion_client(self, project_root: Path) -> LeanMcpClient:
        ensure = cast(EnsureClient, object.__getattribute__(self, "_ensure_started"))
        return await ensure(project_root)


async def _complete_diagnostics(client: LeanMcpClient, path: Path) -> DiagnosticsResult:
    result = await client.diagnostics(
        path, None, None, timeout_seconds=_DIAGNOSTICS_TIMEOUT_SECONDS
    )
    for _ in range(_DIAGNOSTICS_ATTEMPTS - 1):
        if not result.partial and not result.timed_out:
            return result
        result = await client.diagnostics(
            path, None, None, timeout_seconds=_DIAGNOSTICS_TIMEOUT_SECONDS
        )
    return result
