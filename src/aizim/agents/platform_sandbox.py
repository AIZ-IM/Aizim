from __future__ import annotations

import sys
from pathlib import Path

from .linux_sandbox import LinuxSandboxAdapter
from .macos_sandbox import MacOSSandboxAdapter, SandboxHostError
from .sandbox import SandboxAdapter


def sandbox_adapter(
    codex_executable: Path,
    platform: str = sys.platform,
) -> SandboxAdapter:
    if platform == "darwin":
        return MacOSSandboxAdapter.for_executable(codex_executable)
    if platform == "linux":
        return LinuxSandboxAdapter.for_executable(codex_executable)
    raise SandboxHostError("unsupported sandbox platform")
