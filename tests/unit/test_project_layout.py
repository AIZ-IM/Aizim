from __future__ import annotations

import os
import socket
import stat
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from aizim.runtime.layout import CONFIG_TEMPLATE, LayoutError, ProjectLayout
from aizim.runtime.state_process import StateProcessError, acquire_state_process


def lean_project(root: Path) -> Path:
    root.mkdir()
    (root / "lean-toolchain").write_text("leanprover/lean4:v4.32.1\n")
    (root / "lakefile.toml").write_text('name = "fixture"\n')
    return root


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_layout_resolves_project_once_and_exposes_only_owned_paths(tmp_path: Path) -> None:
    root = lean_project(tmp_path / "project")

    layout = ProjectLayout.from_lean_project(root / ".")

    assert layout.root == root.resolve()
    assert layout.state_root == layout.root / ".aizim"
    assert layout.config_path == layout.state_root / "config.toml"
    assert layout.database_path == layout.state_root / "state.sqlite3"
    assert layout.run_root == layout.state_root / "run"
    assert layout.artifact_root == layout.state_root / "artifacts"
    assert layout.promotion_root == layout.state_root / "promoted"
    assert layout.document_root == layout.state_root / "documents"


@pytest.mark.parametrize("missing", ["lean-toolchain", "lakefile.toml"])
def test_layout_rejects_non_lean_project_without_creating_state(
    tmp_path: Path, missing: str
) -> None:
    root = lean_project(tmp_path / "project")
    (root / missing).unlink()

    with pytest.raises(LayoutError):
        ProjectLayout.from_lean_project(root)

    assert not (root / ".aizim").exists()


def test_prepare_runtime_is_private_and_idempotent(tmp_path: Path) -> None:
    layout = ProjectLayout.from_lean_project(lean_project(tmp_path / "project"))

    assert layout.prepare_runtime()
    assert not layout.prepare_runtime()

    for directory in (
        layout.state_root,
        layout.run_root,
        layout.artifact_root,
        layout.promotion_root,
        layout.document_root,
    ):
        assert mode(directory) == 0o700
    assert mode(layout.config_path) == 0o600
    assert layout.config_path.read_bytes() == CONFIG_TEMPLATE
    layout.config_path.chmod(0o644)
    assert not layout.prepare_runtime()
    assert mode(layout.config_path) == 0o600


@pytest.mark.parametrize("child", [None, "run", "artifacts", "config.toml"])
def test_prepare_runtime_refuses_symlinked_owned_components(
    tmp_path: Path, child: str | None
) -> None:
    root = lean_project(tmp_path / "project")
    outside = tmp_path / "outside"
    outside.mkdir()
    state_root = root / ".aizim"
    if child is None:
        state_root.symlink_to(outside, target_is_directory=True)
    else:
        state_root.mkdir()
        (state_root / child).symlink_to(outside, target_is_directory=child != "config.toml")
    layout = ProjectLayout.from_lean_project(root)

    with pytest.raises(LayoutError):
        layout.prepare_runtime()


def test_validate_runtime_rejects_symlinked_config(tmp_path: Path) -> None:
    layout = ProjectLayout.from_lean_project(lean_project(tmp_path / "project"))
    layout.prepare_runtime()
    layout.config_path.unlink()
    layout.config_path.symlink_to(tmp_path / "outside-config")

    with pytest.raises(LayoutError):
        layout.validate_runtime()


def test_state_process_rejects_live_pid(tmp_path: Path) -> None:
    pid_path = tmp_path / "state.pid"
    pid_path.write_text(f"{os.getpid()}\n")
    pid_path.chmod(0o600)

    with pytest.raises(StateProcessError, match="already live"):
        acquire_state_process(pid_path, tmp_path / "state.sock")


def test_state_process_rejects_socket_without_verified_stale_pid() -> None:
    with TemporaryDirectory(prefix="aizim-state-", dir="/tmp") as temporary:
        root = Path(temporary)
        socket_path = root / "state.sock"
        server = socket.socket(socket.AF_UNIX)
        try:
            server.bind(str(socket_path))
        finally:
            server.close()

        with pytest.raises(StateProcessError, match="no verified stale PID"):
            acquire_state_process(root / "state.pid", socket_path)


def test_state_process_rejects_dangling_socket_symlink_without_pid(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "state.sock"
    socket_path.symlink_to(tmp_path / "missing")

    with pytest.raises(StateProcessError, match="no verified stale PID"):
        acquire_state_process(tmp_path / "state.pid", socket_path)


def test_state_process_rejects_non_private_stale_pid(tmp_path: Path) -> None:
    pid_path = tmp_path / "state.pid"
    pid_path.write_text("2147483647\n")
    pid_path.chmod(0o644)

    with pytest.raises(StateProcessError, match="private regular file"):
        acquire_state_process(pid_path, tmp_path / "state.sock")


def test_state_process_reclaims_dead_pid_and_preserves_replacement() -> None:
    with TemporaryDirectory(prefix="aizim-state-", dir="/tmp") as temporary:
        root = Path(temporary)
        pid_path = root / "state.pid"
        socket_path = root / "state.sock"
        pid_path.write_text("2147483647\n")
        pid_path.chmod(0o600)
        server = socket.socket(socket.AF_UNIX)
        try:
            server.bind(str(socket_path))
        finally:
            server.close()

        ownership = acquire_state_process(pid_path, socket_path)

        assert pid_path.read_text() == f"{os.getpid()}\n"
        assert mode(pid_path) == 0o600
        pid_path.unlink()
        pid_path.write_text("42\n")
        ownership.close()
        assert pid_path.read_text() == "42\n"


def test_state_process_preserves_owned_pid_changed_in_place(tmp_path: Path) -> None:
    # Given
    pid_path = tmp_path / "state.pid"
    ownership = acquire_state_process(pid_path, tmp_path / "state.sock")
    original_change_time = pid_path.stat().st_ctime_ns
    pid_path.write_text("42\n")
    assert pid_path.stat().st_ctime_ns != original_change_time

    # When
    ownership.close()

    # Then
    assert pid_path.read_text() == "42\n"
