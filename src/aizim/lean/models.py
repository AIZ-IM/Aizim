from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import PurePosixPath

from aizim.domain import EpochPair, sha256_bytes
from aizim.state.events import utc_now


@dataclass(slots=True)
class DocumentBrokerError(RuntimeError):
    code: str

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True, slots=True)
class DocumentSnapshot:
    document_id: str
    display_name: PurePosixPath
    epoch_pair: EpochPair
    file_version: int
    content_hash: str
    content: bytes

    def __post_init__(self) -> None:
        valid = (
            type(self.document_id) is str
            and bool(self.document_id)
            and type(self.display_name) is PurePosixPath
            and not self.display_name.is_absolute()
            and bool(self.display_name.parts)
            and all(part != ".." and "\x00" not in part for part in self.display_name.parts)
            and type(self.epoch_pair) is EpochPair
            and type(self.file_version) is int
            and self.file_version >= 0
            and type(self.content_hash) is str
            and type(self.content) is bytes
            and re.fullmatch(r"[0-9a-f]{64}", self.content_hash) is not None
            and sha256_bytes(self.content) == self.content_hash
        )
        if not valid:
            raise DocumentBrokerError("INVALID_DOCUMENT_SNAPSHOT")


def _opaque_id() -> str:
    return secrets.token_hex(16)


@dataclass(frozen=True, slots=True)
class BrokerDependencies:
    clock: Callable[[], datetime] = utc_now
    lease_ids: Callable[[], str] = _opaque_id
    document_ids: Callable[[], str] = _opaque_id
    lease_lifetime: timedelta = field(default_factory=lambda: timedelta(minutes=15))

    def __post_init__(self) -> None:
        if (
            not callable(self.clock)
            or not callable(self.lease_ids)
            or not callable(self.document_ids)
            or type(self.lease_lifetime) is not timedelta
            or self.lease_lifetime <= timedelta(0)
        ):
            raise DocumentBrokerError("INVALID_BROKER_DEPENDENCIES")
