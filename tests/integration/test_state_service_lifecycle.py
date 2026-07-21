from __future__ import annotations

import socket
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final

import pytest

from aizim.state import StateService, StateServiceConfig
from aizim.state.rpc import SocketPathError
from aizim.state.service import StateServiceLifecycleError

_CRASH_EXIT: Final = 47
_CRASH_SCRIPT: Final = (
    "import os\n"
    "import sys\n"
    "from pathlib import Path\n"
    "from aizim.state import StateDependencies, StateService, StateServiceConfig\n"
    "def crash() -> None:\n"
    "    os._exit(47)\n"
    "root = Path(sys.argv[1])\n"
    "StateService(StateServiceConfig(root, 'service-session'), "
    "StateDependencies(before_initialization_commit=crash))\n"
)
_LOCK_HOLDER_SCRIPT: Final = (
    "import fcntl\n"
    "import sys\n"
    "from pathlib import Path\n"
    "root = Path(sys.argv[1])\n"
    "lock_path = root / '.aizim' / 'run' / 'state.lock'\n"
    "lock_path.parent.mkdir(parents=True, exist_ok=True)\n"
    "with lock_path.open('a+b') as lock:\n"
    "    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
    "    print('READY', flush=True)\n"
    "    sys.stdin.read(1)\n"
)
_SOCKET_HOLDER_SCRIPT: Final = (
    "import socket\n"
    "import sys\n"
    "from pathlib import Path\n"
    "socket_path = Path(sys.argv[1]) / '.aizim' / 'run' / 'state.sock'\n"
    "socket_path.parent.mkdir(parents=True, exist_ok=True)\n"
    "with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:\n"
    "    listener.bind(str(socket_path))\n"
    "    listener.listen()\n"
    "    print('READY', flush=True)\n"
    "    sys.stdin.read(1)\n"
)


@pytest.fixture
def project_root() -> Iterator[Path]:
    with TemporaryDirectory(prefix="aizim-lifecycle-", dir="/tmp") as directory:
        yield Path(directory)


def _service(project_root: Path) -> StateService:
    return StateService(StateServiceConfig(project_root, "service-session"))


@contextmanager
def _holder(script: str, project_root: Path) -> Iterator[subprocess.Popen[str]]:
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(project_root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline() == "READY\n"
    try:
        yield process
    finally:
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)


def test_foundation_initialization_rolls_back_after_precommit_process_crash(
    project_root: Path,
) -> None:
    # Given / When
    crashed = subprocess.run(
        [sys.executable, "-c", _CRASH_SCRIPT, str(project_root)],
        check=False,
        capture_output=True,
        text=True,
    )

    # Then
    assert crashed.returncode == _CRASH_EXIT
    database = project_root / ".aizim" / "state.sqlite3"
    state = subprocess.run(
        [
            "/usr/bin/sqlite3",
            str(database),
            "PRAGMA user_version; SELECT COUNT(*) FROM sqlite_master WHERE type='table';",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert state.stdout.splitlines() == ["0", "0"]
    with _service(project_root) as restarted:
        assert restarted.health().event_schema_version == 1


def test_cross_process_owner_is_rejected_before_database_creation(
    project_root: Path,
) -> None:
    # Given
    service: StateService | None = None
    with _holder(_LOCK_HOLDER_SCRIPT, project_root):
        try:
            # When / Then
            with pytest.raises(StateServiceLifecycleError, match="already owned"):
                service = _service(project_root)
        finally:
            if service is not None:
                service.close()

    assert not (project_root / ".aizim" / "state.sqlite3").exists()


@pytest.mark.asyncio
async def test_live_cross_process_socket_is_refused_and_preserved(
    project_root: Path,
) -> None:
    # Given
    socket_path = project_root / ".aizim" / "run" / "state.sock"
    with _holder(_SOCKET_HOLDER_SCRIPT, project_root) as holder:
        service = _service(project_root)
        try:
            # When / Then
            with pytest.raises(SocketPathError, match="live"):
                await service.start()
            assert holder.poll() is None
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(socket_path))
        finally:
            await service.aclose()
    socket_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_stale_socket_is_reclaimed(project_root: Path) -> None:
    # Given
    socket_path = project_root / ".aizim" / "run" / "state.sock"
    socket_path.parent.mkdir(parents=True)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
        stale.bind(str(socket_path))

    # When
    async with _service(project_root):
        # Then
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(socket_path))


@pytest.mark.asyncio
async def test_close_preserves_replacement_socket_inode(project_root: Path) -> None:
    # Given
    service = _service(project_root)
    await service.start()
    socket_path = service.socket_path
    socket_path.unlink()
    replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    replacement.bind(str(socket_path))
    replacement.listen()
    replacement_inode = (socket_path.stat().st_dev, socket_path.stat().st_ino)

    try:
        # When
        await service.aclose()

        # Then
        current = socket_path.stat()
        assert (current.st_dev, current.st_ino) == replacement_inode
    finally:
        await service.aclose()
        replacement.close()
        socket_path.unlink(missing_ok=True)
