from __future__ import annotations

import os
import struct
from hashlib import sha256
from pathlib import Path

import pytest

from aizim.gateway import LinuxPeerIdentityVerifier, PeerIdentityError


class PeerCredentials:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.calls: list[tuple[int, int, int]] = []

    def getsockopt(self, level: int, option: int, size: int = 0) -> bytes:
        self.calls.append((level, option, size))
        return self.body


def test_linux_peer_identity_hashes_the_connected_process_image(tmp_path: Path) -> None:
    image = tmp_path / "python"
    image.write_bytes(b"managed python image")
    credentials = PeerCredentials(
        struct.pack("=iii", 4321, os.geteuid(), os.getegid())
    )
    verifier = LinuxPeerIdentityVerifier(
        platform="linux",
        open_process_image=lambda pid: os.open(image, os.O_RDONLY) if pid == 4321 else -1,
    )

    identity = verifier(credentials)

    assert identity.euid == os.geteuid()
    assert identity.executable_sha256 == sha256(image.read_bytes()).hexdigest()
    assert credentials.calls == [
        (1, 17, struct.calcsize("=iii"))
    ]


@pytest.mark.parametrize(
    "body",
    (
        b"",
        struct.pack("=iii", 0, os.geteuid(), os.getegid()),
        struct.pack("=iii", 4321, -1, os.getegid()),
    ),
)
def test_linux_peer_identity_rejects_invalid_credentials(body: bytes) -> None:
    verifier = LinuxPeerIdentityVerifier(
        platform="linux",
        open_process_image=lambda _pid: -1,
    )

    with pytest.raises(PeerIdentityError):
        verifier(PeerCredentials(body))


def test_linux_peer_identity_rejects_other_platforms() -> None:
    verifier = LinuxPeerIdentityVerifier(
        platform="darwin",
        open_process_image=lambda _pid: -1,
    )

    with pytest.raises(PeerIdentityError, match="Linux peer identity is unavailable"):
        verifier(PeerCredentials(b""))
