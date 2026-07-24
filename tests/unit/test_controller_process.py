from __future__ import annotations

import sys
from pathlib import Path

import pytest

from aizim.orchestration.controller_process import (
    ControllerLaunchError,
    run_controller_host_command,
)


def test_sync_host_command_counts_output_after_other_stream_closes(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "output"
    binary.write_text(f"#!{sys.executable}\nimport os\nos.close(2)\nprint('x'*2048)\n")
    binary.chmod(0o700)

    with pytest.raises(ControllerLaunchError, match="CONTROLLER_OUTPUT_LIMIT"):
        run_controller_host_command((str(binary),), {}, 1024)
