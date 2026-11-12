from __future__ import annotations

import os
from collections.abc import Mapping

from aizim.runtime.distribution import DISTRIBUTION_ENVIRONMENT

_SECRET_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL")


def scrubbed_command_environment(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if environ is None else environ
    return {
        name: value
        for name, value in source.items()
        if name not in DISTRIBUTION_ENVIRONMENT
        and not any(marker in name.upper() for marker in _SECRET_MARKERS)
    }
