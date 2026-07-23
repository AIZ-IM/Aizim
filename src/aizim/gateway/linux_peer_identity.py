from __future__ import annotations

import os
import struct
import sys
from collections.abc import Callable

from .peer_identity import (
    PeerIdentity,
    PeerIdentityError,
    PeerSocket,
    _hash_regular_descriptor,
)

_SOL_SOCKET = 1
_SO_PEERCRED = 17
_UCRED_FORMAT = "=iii"
_UCRED_SIZE = struct.calcsize(_UCRED_FORMAT)


def _peer_credentials(peer_socket: PeerSocket) -> tuple[int, int]:
    raw = peer_socket.getsockopt(_SOL_SOCKET, _SO_PEERCRED, _UCRED_SIZE)
    if type(raw) is not bytes or len(raw) != _UCRED_SIZE:
        raise PeerIdentityError("peer credentials are unavailable")
    pid, euid, _gid = struct.unpack(_UCRED_FORMAT, raw)
    if pid <= 0 or euid < 0:
        raise PeerIdentityError("peer credentials are invalid")
    return pid, euid


def _open_process_image(pid: int) -> int:
    return os.open(f"/proc/{pid}/exe", os.O_RDONLY | os.O_CLOEXEC)


class LinuxPeerIdentityVerifier:
    def __init__(
        self,
        *,
        platform: str = sys.platform,
        open_process_image: Callable[[int], int] = _open_process_image,
    ) -> None:
        self._platform = platform
        self._open_process_image = open_process_image

    def __call__(self, peer_socket: PeerSocket) -> PeerIdentity:
        if self._platform != "linux":
            raise PeerIdentityError("Linux peer identity is unavailable")
        descriptor = -1
        try:
            pid, euid = _peer_credentials(peer_socket)
            descriptor = self._open_process_image(pid)
            image_hash = _hash_regular_descriptor(descriptor)
        except (OSError, AttributeError, struct.error) as error:
            raise PeerIdentityError("peer identity verification failed") from error
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return PeerIdentity(euid, image_hash)
