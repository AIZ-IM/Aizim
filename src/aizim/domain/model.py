from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import assert_never

from .serialization import sha256_bytes


@dataclass(frozen=True, slots=True)
class _ContractError(ValueError):
    field: str
    reason: str

    def __str__(self) -> str:
        return f"{self.field}: {self.reason}"


def _require_text(value: str, field: str) -> None:
    if type(value) is not str or not value:
        raise _ContractError(field, "must be a non-empty string")


def _require_hash(value: str, field: str) -> None:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise _ContractError(field, "must be a 64-character lowercase hexadecimal hash")


def _require_sequence(value: int, field: str) -> None:
    if type(value) is not int or value < 0:
        raise _ContractError(field, "must be a non-negative integer")


def _require_strings(values: tuple[str, ...], field: str) -> None:
    if type(values) is not tuple or any(type(value) is not str for value in values):
        raise _ContractError(field, "must be a tuple of strings")


class ContributionPayloadKind(StrEnum):
    PATCH = "patch"
    SNAPSHOT = "snapshot"


class PublicationState(StrEnum):
    STAGED = "staged"
    VERIFIED = "verified"
    MATERIALIZED = "materialized"
    PUBLISHED = "published"
    QUARANTINED = "quarantined"


class AgentRole(StrEnum):
    CONDUCTOR = "conductor"
    QUESTION_WORKER = "question_worker"
    PROOF_WORKER = "proof_worker"
    PROMOTION_SERVICE = "promotion_service"
    ALIGNMENT_REVIEWER = "alignment_reviewer"


@dataclass(frozen=True, slots=True)
class EpochPair:
    base_epoch: str
    knowledge_epoch: int

    def __post_init__(self) -> None:
        _require_hash(self.base_epoch, "base_epoch")
        _require_sequence(self.knowledge_epoch, "knowledge_epoch")


@dataclass(frozen=True, slots=True)
class Staleness:
    base_epoch: bool = False
    knowledge_epoch: bool = False
    file_version: bool = False

    def __post_init__(self) -> None:
        flags = (self.base_epoch, self.knowledge_epoch, self.file_version)
        if any(type(value) is not bool for value in flags):
            raise _ContractError("staleness", "flags must be booleans")

    @property
    def is_stale(self) -> bool:
        return self.base_epoch or self.knowledge_epoch or self.file_version


@dataclass(frozen=True, slots=True)
class FileLease:
    lease_id: str
    worker_id: str
    run_id: str
    document_id: str
    virtual_document_namespace: str
    epoch_pair: EpochPair
    file_version: int
    content_hash: str
    expires_at: datetime
    physical_file: Path | None = None
    recovery_metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for field, value in (
            ("lease_id", self.lease_id),
            ("worker_id", self.worker_id),
            ("run_id", self.run_id),
            ("document_id", self.document_id),
            ("virtual_document_namespace", self.virtual_document_namespace),
        ):
            _require_text(value, field)
        if type(self.epoch_pair) is not EpochPair:
            raise _ContractError("epoch_pair", "must be an EpochPair")
        _require_sequence(self.file_version, "file_version")
        _require_hash(self.content_hash, "content_hash")
        if type(self.expires_at) is not datetime or self.expires_at.tzinfo is None:
            raise _ContractError("expires_at", "must be a timezone-aware UTC datetime")
        if self.expires_at.utcoffset() != timedelta(0):
            raise _ContractError("expires_at", "must be a timezone-aware UTC datetime")
        if self.physical_file is not None and not isinstance(self.physical_file, Path):
            raise _ContractError("physical_file", "must be a path or None")
        valid_recovery = type(self.recovery_metadata) is tuple and all(
            type(pair) is tuple
            and len(pair) == 2
            and all(type(value) is str for value in pair)
            for pair in self.recovery_metadata
        )
        if not valid_recovery:
            raise _ContractError("recovery_metadata", "must be a tuple of string pairs")


@dataclass(frozen=True, slots=True)
class Contribution:
    worker_id: str
    run_id: str
    lease_id: str
    epoch_pair: EpochPair
    file_version: int | None
    payload_kind: ContributionPayloadKind
    environment_fingerprint: str
    candidate_declaration: str
    candidate_proof: str
    patch_body: str | None = None
    snapshot_body: str | None = None
    payload_hash: str | None = None
    formal_dependencies: tuple[str, ...] = ()
    imports: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    axioms: tuple[str, ...] = ()
    formal_action_trace: tuple[str, ...] = ()
    research_notes: tuple[str, ...] = ()
    evidence_links: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field, value in (
            ("worker_id", self.worker_id),
            ("run_id", self.run_id),
            ("lease_id", self.lease_id),
            ("candidate_declaration", self.candidate_declaration),
            ("candidate_proof", self.candidate_proof),
        ):
            _require_text(value, field)
        if type(self.epoch_pair) is not EpochPair:
            raise _ContractError("epoch_pair", "must be an EpochPair")
        if type(self.payload_kind) is not ContributionPayloadKind:
            raise _ContractError("payload_kind", "must be a ContributionPayloadKind")
        if self.file_version is None:
            raise _ContractError("file_version", "must record the leased file version")
        _require_sequence(self.file_version, "file_version")
        _require_hash(self.environment_fingerprint, "environment_fingerprint")
        for field, values in (
            ("formal_dependencies", self.formal_dependencies),
            ("imports", self.imports),
            ("assumptions", self.assumptions),
            ("axioms", self.axioms),
            ("formal_action_trace", self.formal_action_trace),
            ("research_notes", self.research_notes),
            ("evidence_links", self.evidence_links),
        ):
            _require_strings(values, field)
        match self.payload_kind:
            case ContributionPayloadKind.PATCH:
                if not self.patch_body:
                    raise _ContractError("patch", "requires a body and expected file version")
                if self.snapshot_body is not None or self.payload_hash is not None:
                    raise _ContractError("patch", "cannot contain snapshot fields")
            case ContributionPayloadKind.SNAPSHOT:
                if not self.snapshot_body or self.payload_hash is None:
                    raise _ContractError("snapshot", "requires a body and payload hash")
                _require_hash(self.payload_hash, "payload_hash")
                if sha256_bytes(self.snapshot_body.encode()) != self.payload_hash:
                    raise _ContractError("payload_hash", "does not match snapshot body")
                if self.patch_body is not None:
                    raise _ContractError("snapshot", "cannot contain a patch body")
            case unreachable:
                assert_never(unreachable)

    def staleness(
        self,
        *,
        current_epoch: EpochPair,
        current_file_version: int,
    ) -> Staleness:
        _require_sequence(current_file_version, "current_file_version")
        match self.payload_kind:
            case ContributionPayloadKind.PATCH:
                file_version = self.file_version != current_file_version
            case ContributionPayloadKind.SNAPSHOT:
                file_version = False
            case unreachable:
                assert_never(unreachable)
        return Staleness(
            base_epoch=self.epoch_pair.base_epoch != current_epoch.base_epoch,
            knowledge_epoch=self.epoch_pair.knowledge_epoch != current_epoch.knowledge_epoch,
            file_version=file_version,
        )


@dataclass(frozen=True, slots=True)
class KnowledgeDelta:
    previous_epoch: EpochPair
    new_epoch: EpochPair
    theorem_name: str | None = None
    complete_type: str | None = None
    module: str | None = None
    dependencies: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    evidence_links: tuple[str, ...] = ()
    old_environment_fingerprint: str | None = None
    new_environment_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if self.new_epoch.knowledge_epoch != self.previous_epoch.knowledge_epoch + 1:
            raise _ContractError("new_epoch", "must advance knowledge_epoch exactly once")
        if self.new_epoch.base_epoch == self.previous_epoch.base_epoch:
            raise _ContractError("new_epoch", "must publish a new base_epoch")
        declaration = (self.theorem_name, self.complete_type, self.module)
        environment = (self.old_environment_fingerprint, self.new_environment_fingerprint)
        has_declaration = any(value is not None for value in declaration)
        has_environment = any(value is not None for value in environment)
        if has_declaration == has_environment:
            raise _ContractError("knowledge_delta", "must contain one delta payload kind")
        if has_declaration:
            fields = ("theorem_name", "complete_type", "module")
            for field, value in zip(fields, declaration, strict=True):
                if value is None:
                    raise _ContractError(field, "is required for a declaration delta")
                _require_text(value, field)
        if has_environment:
            for field, value in zip(
                ("old_environment_fingerprint", "new_environment_fingerprint"),
                environment,
                strict=True,
            ):
                if value is None:
                    raise _ContractError(field, "is required for an environment delta")
                _require_hash(value, field)
        for field, values in (
            ("dependencies", self.dependencies),
            ("assumptions", self.assumptions),
            ("evidence_links", self.evidence_links),
        ):
            _require_strings(values, field)
