from __future__ import annotations

from typing import cast

import pytest

from aizim.domain import ControllerProviderId
from aizim.orchestration.control_plane import ControlPlaneError, configure_controller
from aizim.state.control_operations import ControlOperationTarget


@pytest.mark.parametrize("value", ("codex", "claude", "future_provider-1"))
def test_controller_provider_id_accepts_well_formed_values(value: str) -> None:
    assert ControllerProviderId(value).value == value


@pytest.mark.parametrize(
    "value",
    (
        "",
        "Codex",
        "1codex",
        "contains.dot",
        "contains/slash",
        "a" * 65,
        1,
    ),
)
def test_controller_provider_id_rejects_malformed_values(value: object) -> None:
    with pytest.raises(ValueError, match=r"^CONTROLLER_PROVIDER_INVALID$"):
        ControllerProviderId(cast(str, value))


def test_control_plane_rejects_a_value_that_is_not_a_provider_id() -> None:
    with pytest.raises(ControlPlaneError, match=r"^CONTROLLER_PROVIDER_INVALID$"):
        configure_controller(
            cast(ControlOperationTarget, object()),
            cast(ControllerProviderId, "codex"),
            None,
        )
