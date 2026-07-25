from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TypeGuard

_CONTROLLER_PROVIDER_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")


def is_controller_provider_id(value: object) -> TypeGuard[str]:
    return type(value) is str and _CONTROLLER_PROVIDER_ID.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class ControllerProviderId:
    value: str

    def __post_init__(self) -> None:
        if not is_controller_provider_id(self.value):
            raise ValueError("CONTROLLER_PROVIDER_INVALID")
