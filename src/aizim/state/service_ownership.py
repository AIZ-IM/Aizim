from __future__ import annotations

import errno
import fcntl
import os
import socket
import stat
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType


@dataclass(frozen=True, slots=True)
class StateOwnershipError(RuntimeError):
    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class SocketOwnershipError(RuntimeError):
    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class SocketIdentity:
    device: int
    inode: int


class StateOwnership:
    def __init__(self, descriptor: int) -> None:
        self._descriptor: int | None = descriptor

    def __enter__(self) -> StateOwnership:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception_type, exception, traceback
        self.close()

    def close(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def acquire_state_ownership(lock_path: Path) -> StateOwnership:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
    except OSError:
        os.close(descriptor)
        raise
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        os.close(descriptor)
        if error.errno in {errno.EACCES, errno.EAGAIN}:
            raise StateOwnershipError("state service is already owned") from error
        raise
    return StateOwnership(descriptor)


def _identity(path: Path) -> SocketIdentity | None:
    try:
        status = path.lstat()
    except FileNotFoundError:
        return None
    return SocketIdentity(status.st_dev, status.st_ino)


def _socket_identity(path: Path) -> SocketIdentity | None:
    try:
        status = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISSOCK(status.st_mode):
        raise SocketOwnershipError("state socket path is occupied by a non-socket entry")
    return SocketIdentity(status.st_dev, status.st_ino)


def reclaim_stale_socket(path: Path) -> None:
    original = _socket_identity(path)
    if original is None:
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        result = probe.connect_ex(str(path))
    if result == 0:
        raise SocketOwnershipError("state socket path belongs to a live service")
    if result == errno.ENOENT:
        return
    if result != errno.ECONNREFUSED:
        message = os.strerror(result) if result else "unknown error"
        raise SocketOwnershipError(f"state socket path cannot be safely probed: {message}")
    current = _socket_identity(path)
    if current is None:
        return
    if current != original:
        raise SocketOwnershipError("state socket path changed while checking staleness")
    path.unlink()


def record_owned_socket(path: Path) -> SocketIdentity:
    identity = _socket_identity(path)
    if identity is None:
        raise SocketOwnershipError("state socket was not created")
    return identity


def remove_owned_socket(path: Path, owned: SocketIdentity) -> None:
    if _identity(path) == owned:
        path.unlink()
