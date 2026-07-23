from __future__ import annotations

import errno
import hashlib
import json
import os
import socket
import sys
from collections.abc import Callable
from contextlib import suppress

_DENIED_ERRNOS = frozenset(
    {errno.EACCES, errno.ENOENT, errno.ENOTDIR, errno.EPERM}
)


def _read(path: str) -> None:
    with open(path, "rb") as handle:
        handle.read(1)


def _open_for_write(path: str) -> None:
    descriptor = os.open(path, os.O_WRONLY)
    os.close(descriptor)


def _traverse(document: dict[str, object]) -> None:
    view_root = str(document["view_root"])
    target = str(document["unleased_file"])
    _read(os.path.join(view_root, os.path.relpath(target, view_root)))


def _scratch_symlink(document: dict[str, object]) -> None:
    link = os.path.join(str(document["scratch_root"]), "state-link")
    try:
        os.symlink(str(document["state_database"]), link)
        _read(link)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(link)


def _connect_unix(path: str) -> None:
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(1.0)
        client.connect(path)


def _connect_tcp(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET) as client:
        client.settimeout(1.0)
        client.connect((host, port))


def _tcp_port(document: dict[str, object]) -> int:
    value = document["tcp_port"]
    if not isinstance(value, int):
        raise RuntimeError
    return value


def _read_secret(name: str) -> None:
    if name not in os.environ:
        raise PermissionError(errno.EACCES, "environment value withheld")


def _write_scratch(root: str) -> None:
    path = os.path.join(root, "probe-output")
    try:
        with open(path, "wb") as handle:
            handle.write(b"sandbox write permitted\n")
        with open(path, "rb") as handle:
            if handle.read() != b"sandbox write permitted\n":
                raise RuntimeError
    finally:
        with suppress(FileNotFoundError):
            os.unlink(path)


def _read_view(path: str, expected: str) -> None:
    with open(path, "rb") as handle:
        actual = hashlib.sha256(handle.read()).hexdigest()
    if actual != expected:
        raise RuntimeError


def _attempt(operation: str, action: Callable[[], None]) -> dict[str, str]:
    try:
        action()
    except OSError as error:
        if error.errno in _DENIED_ERRNOS:
            return {
                "operation": operation,
                "verdict": "denied",
                "reason_code": "SANDBOX_ENFORCED",
            }
        return {
            "operation": operation,
            "verdict": "denied",
            "reason_code": "OPERATION_FAILED",
        }
    except RuntimeError:
        return {
            "operation": operation,
            "verdict": "denied",
            "reason_code": "OPERATION_FAILED",
        }
    return {
        "operation": operation,
        "verdict": "allowed",
        "reason_code": "OPERATION_ALLOWED",
    }


def run(document: dict[str, object]) -> list[dict[str, str]]:
    operations: tuple[tuple[str, Callable[[], None]], ...] = (
        ("read_state_database", lambda: _read(str(document["state_database"]))),
        (
            "write_state_database",
            lambda: _open_for_write(str(document["state_database"])),
        ),
        ("write_unleased_file", lambda: _open_for_write(str(document["unleased_file"]))),
        (
            "write_shared_artifact",
            lambda: _open_for_write(str(document["shared_artifact"])),
        ),
        ("traverse_to_unleased_file", lambda: _traverse(document)),
        ("scratch_symlink_to_state", lambda: _scratch_symlink(document)),
        (
            "connect_gateway_unix_socket",
            lambda: _connect_unix(str(document["gateway_socket"])),
        ),
        (
            "connect_nonallowlisted_tcp",
            lambda: _connect_tcp(str(document["tcp_host"]), _tcp_port(document)),
        ),
        (
            "read_secret_environment",
            lambda: _read_secret(str(document["secret_environment_name"])),
        ),
        ("write_allowed_scratch", lambda: _write_scratch(str(document["scratch_root"]))),
        (
            "read_allowed_view",
            lambda: _read_view(
                str(document["allowed_view_file"]),
                str(document["allowed_view_sha256"]),
            ),
        ),
    )
    return [_attempt(operation, action) for operation, action in operations]


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    document = json.loads(sys.argv[1])
    if not isinstance(document, dict):
        return 2
    print(json.dumps({"attempts": run(document)}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
