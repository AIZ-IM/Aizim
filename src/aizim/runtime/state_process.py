from __future__ import annotations

import errno
import os
import stat
from dataclasses import dataclass
from pathlib import Path


class StateProcessError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FileIdentity:
    device: int
    inode: int


def _identity(path: Path) -> FileIdentity | None:
    try:
        status = path.lstat()
    except FileNotFoundError:
        return None
    return FileIdentity(status.st_dev, status.st_ino)


def _read_pid(path: Path) -> tuple[int, FileIdentity]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise StateProcessError("state PID record is not safe") from error
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_nlink != 1
            or stat.S_IMODE(status.st_mode) != 0o600
        ):
            raise StateProcessError("state PID record is not a private regular file")
        body = os.read(descriptor, 64).decode()
    except (OSError, UnicodeDecodeError) as error:
        raise StateProcessError("state PID record is invalid") from error
    finally:
        os.close(descriptor)
    try:
        pid = int(body.strip())
    except ValueError as error:
        raise StateProcessError("state PID record is invalid") from error
    if pid <= 0:
        raise StateProcessError("state PID record is invalid")
    return pid, FileIdentity(status.st_dev, status.st_ino)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def validate_live_state_process(pid_path: Path, socket_path: Path) -> None:
    pid, pid_identity = _read_pid(pid_path)
    if not _alive(pid) or _identity(pid_path) != pid_identity:
        raise StateProcessError("state service PID is not live and stable")
    try:
        socket_status = socket_path.lstat()
    except OSError as error:
        raise StateProcessError("state socket is unavailable") from error
    if (
        not stat.S_ISSOCK(socket_status.st_mode)
        or stat.S_IMODE(socket_status.st_mode) != 0o600
    ):
        raise StateProcessError("state socket is not a private Unix socket")


def _remove_owned(path: Path, identity: FileIdentity) -> None:
    if _identity(path) == identity:
        path.unlink()


class StateProcessOwnership:
    def __init__(self, pid_path: Path, identity: FileIdentity, descriptor: int) -> None:
        self._pid_path = pid_path
        self._identity: FileIdentity | None = identity
        self._descriptor: int | None = descriptor

    def close(self) -> None:
        identity = self._identity
        descriptor = self._descriptor
        if identity is None or descriptor is None:
            return
        self._identity = None
        self._descriptor = None
        try:
            _remove_owned(self._pid_path, identity)
        finally:
            os.close(descriptor)


def acquire_state_process(pid_path: Path, socket_path: Path) -> StateProcessOwnership:
    pid_entry = _identity(pid_path)
    checked_dead_pid = False
    if pid_entry is not None:
        pid, identity = _read_pid(pid_path)
        if _alive(pid):
            raise StateProcessError("state service process is already live")
        if _identity(pid_path) != identity:
            raise StateProcessError("state PID record changed during validation")
        _remove_owned(pid_path, identity)
        checked_dead_pid = True
    if _identity(socket_path) is not None and not checked_dead_pid:
        raise StateProcessError("state socket has no verified stale PID record")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        descriptor = os.open(pid_path, flags, 0o600)
    except OSError as error:
        if error.errno == errno.EEXIST:
            raise StateProcessError("state PID record is already owned") from error
        raise StateProcessError("state PID record could not be created") from error
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, f"{os.getpid()}\n".encode())
        os.fsync(descriptor)
        status = os.fstat(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return StateProcessOwnership(
        pid_path,
        FileIdentity(status.st_dev, status.st_ino),
        descriptor,
    )
