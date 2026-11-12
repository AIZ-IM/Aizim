from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ResolvedExecutable:
    """Executable identity pinned at the trusted host boundary."""

    path: Path
    version: str
    sha256: str
