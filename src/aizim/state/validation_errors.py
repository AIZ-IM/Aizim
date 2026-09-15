from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class EventValidationError(ValueError):
    location: str
    reason: str

    def __str__(self) -> str:
        return f"{self.location}: {self.reason}"
