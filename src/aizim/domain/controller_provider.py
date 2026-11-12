"""Historical controller identifiers accepted when replaying persisted events."""

from __future__ import annotations

import re
from typing import TypeGuard

_CONTROLLER_PROVIDER_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")


def is_controller_provider_id(value: object) -> TypeGuard[str]:
    return type(value) is str and _CONTROLLER_PROVIDER_ID.fullmatch(value) is not None


