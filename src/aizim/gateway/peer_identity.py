from __future__ import annotations

import asyncio
import ctypes
import json
import os
import re
import secrets
import stat
import struct
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from aizim.domain import AgentRole, canonical_json
from aizim.domain.serialization import JsonValue

MAX_FRAME_BYTES = 4096

_SOL_LOCAL = 0
_LOCAL_PEERCRED = 1
_LOCAL_PEERTOKEN = 6
_AUDIT_WORDS = 8
_MAX_PATH = 4096


@dataclass(frozen=True, slots=True)
class PeerIdentity:
    euid: int
    executable_sha256: str


@dataclass(frozen=True, slots=True)
class PeerIdentityError(RuntimeError):
    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class BrokerLifecycleError(RuntimeError):
    reason: str

    def __str__(self) -> str:
        return self.reason


class SessionDeniedError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("SESSION_DENIED")


@dataclass(frozen=True, slots=True, repr=False)
class BrokerRegistration:
    run_id: str
    worker_id: str
    role: AgentRole
    sidecar_executable_sha256: str
    expires_at: datetime
    raw_token: str

    def __post_init__(self) -> None:
        for name, value in (("run_id", self.run_id), ("worker_id", self.worker_id)):
            if type(value) is not str or not value:
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.role, AgentRole):
            raise ValueError("role must be an AgentRole")
        if re.fullmatch(r"[0-9a-f]{64}", self.sidecar_executable_sha256) is None:
            raise ValueError("sidecar_executable_sha256 must be a lowercase SHA-256 hash")
        if (
            type(self.expires_at) is not datetime
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() != timedelta(0)
        ):
            raise ValueError("expires_at must be a timezone-aware UTC datetime")
        if type(self.raw_token) is not str or not self.raw_token:
            raise ValueError("raw_token must be a non-empty string")

    def __repr__(self) -> str:
        return "BrokerRegistration(raw_token=<redacted>, claims=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class RedeemedSession:
    raw_token: str
    run_id: str
    worker_id: str
    role: AgentRole

    def __repr__(self) -> str:
        return "RedeemedSession(raw_token=<redacted>, claims=<redacted>)"


@dataclass(frozen=True, slots=True)
class BrokerDependencies:
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    session_ids: Callable[[], str] = lambda: secrets.token_urlsafe(32)
    peer_identity: Callable[[PeerSocket], PeerIdentity] = field(
        default_factory=lambda: DarwinPeerIdentityVerifier()
    )


def _text_field(document: dict[str, JsonValue], name: str) -> str:
    value = document.get(name)
    if type(value) is not str or not value:
        raise SessionDeniedError
    return value


async def write_frame(writer: asyncio.StreamWriter, document: object) -> None:
    encoded = canonical_json(document)
    writer.write(len(encoded).to_bytes(4, "big") + encoded)
    await writer.drain()


async def redeem_session(socket_path: Path, session_id: str) -> RedeemedSession:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    try:
        await write_frame(writer, {"session_id": session_id})
        size = int.from_bytes(await reader.readexactly(4), "big")
        if size > MAX_FRAME_BYTES:
            raise SessionDeniedError
        value: JsonValue = json.loads((await reader.readexactly(size)).decode())
        if type(value) is not dict or value.get("ok") is not True:
            raise SessionDeniedError
        session = value.get("session")
        if type(session) is not dict or session.keys() != {
            "raw_token", "run_id", "worker_id", "role"
        }:
            raise SessionDeniedError
        return RedeemedSession(
            _text_field(session, "raw_token"),
            _text_field(session, "run_id"),
            _text_field(session, "worker_id"),
            AgentRole(_text_field(session, "role")),
        )
    except (asyncio.IncompleteReadError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise SessionDeniedError from error
    finally:
        writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()


class PeerSocket(Protocol):
    def getsockopt(self, level: int, option: int, size: int = 0) -> bytes | int: ...


def _peer_credentials(peer_socket: PeerSocket) -> int:
    raw = peer_socket.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, 128)
    if type(raw) is not bytes or len(raw) < 8:
        raise PeerIdentityError("peer credentials are unavailable")
    version, euid = struct.unpack_from("=II", raw)
    if version != 0:
        raise PeerIdentityError("peer credential version is unsupported")
    return euid


def _audit_token(peer_socket: PeerSocket) -> tuple[bytes, int]:
    raw = peer_socket.getsockopt(_SOL_LOCAL, _LOCAL_PEERTOKEN, _AUDIT_WORDS * 4)
    if type(raw) is not bytes or len(raw) != _AUDIT_WORDS * 4:
        raise PeerIdentityError("peer audit token is unavailable")
    words = struct.unpack("=8I", raw)
    return raw, words[1]


def _process_image_path(raw_token: bytes) -> str:
    token_type = ctypes.c_uint32 * _AUDIT_WORDS
    token = token_type.from_buffer_copy(raw_token)
    buffer = ctypes.create_string_buffer(_MAX_PATH)
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    function = library.proc_pidpath_audittoken
    function.argtypes = [ctypes.POINTER(token_type), ctypes.c_void_p, ctypes.c_uint32]
    function.restype = ctypes.c_int
    result = function(ctypes.byref(token), buffer, len(buffer))
    if result <= 0:
        raise PeerIdentityError("peer process image is unavailable")
    try:
        return buffer.raw[:result].rstrip(b"\0").decode()
    except UnicodeDecodeError as error:
        raise PeerIdentityError("peer process image path is invalid") from error


def _hash_regular_file(path: str) -> str:
    flags = os.O_RDONLY | os.O_CLOEXEC
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags | nofollow)
    digest = sha256()
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PeerIdentityError("peer process image is not a regular file")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


class DarwinPeerIdentityVerifier:
    def __call__(self, peer_socket: PeerSocket) -> PeerIdentity:
        if sys.platform != "darwin":
            raise PeerIdentityError("Darwin peer identity is unavailable")
        try:
            euid = _peer_credentials(peer_socket)
            raw_token, token_euid = _audit_token(peer_socket)
            if token_euid != euid:
                raise PeerIdentityError("peer identities disagree")
            image_hash = _hash_regular_file(_process_image_path(raw_token))
        except (OSError, AttributeError) as error:
            raise PeerIdentityError("peer identity verification failed") from error
        return PeerIdentity(euid, image_hash)
