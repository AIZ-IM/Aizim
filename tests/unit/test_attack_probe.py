from __future__ import annotations

import errno

import pytest

from aizim.agents.attack_probe import _attempt


@pytest.mark.parametrize("error_number", (errno.ENOENT, errno.ENOTDIR))
def test_hidden_protected_paths_are_sandbox_denials(error_number: int) -> None:
    def hidden_path() -> None:
        raise OSError(error_number, "hidden by sandbox")

    assert _attempt("read_state_database", hidden_path) == {
        "operation": "read_state_database",
        "verdict": "denied",
        "reason_code": "SANDBOX_ENFORCED",
    }
