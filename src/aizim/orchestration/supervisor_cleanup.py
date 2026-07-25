from __future__ import annotations

from aizim.runtime.state_process import StateProcessOwnership
from aizim.state import StateService

from .controller_backend import ControllerBackend
from .controller_dispatcher import record_controller_stop
from .controller_execution import ControllerExecutionError


async def finalize_controller(
    state: StateService | None,
    ownership: StateProcessOwnership,
    details: tuple[bool, str, str],
) -> None:
    started, session_id, reason_code = details
    try:
        if state is not None:
            try:
                if started:
                    record_controller_stop(state, session_id, reason_code)
            finally:
                try:
                    state.checkpoint()
                finally:
                    await state.aclose()
    finally:
        ownership.close()


def unavailable_controller() -> ControllerBackend:
    raise ControllerExecutionError("CONTROLLER_BACKEND_UNAVAILABLE")
