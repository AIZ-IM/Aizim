from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from aizim.domain import EpochPair, sha256_bytes
from aizim.domain.serialization import JsonValue
from aizim.state import AppendEventCommand, PublicationQueueEntry, StateService
from aizim.state.documents import DocumentStateError

from .artifacts import ArtifactReference, ArtifactStore
from .contributions import (
    ContributionValidationError,
    PatchPayload,
    SnapshotPayload,
    validate_candidate_source,
)


def _text(value: object, code: str) -> str:
    if type(value) is not str or not value:
        raise ContributionValidationError(code)
    return value


def _hash(value: object, code: str) -> str:
    value = _text(value, code)
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ContributionValidationError(code)
    return value


def _strings(value: object, code: str) -> tuple[str, ...]:
    if type(value) is not tuple or any(type(item) is not str or not item for item in value):
        raise ContributionValidationError(code)
    return cast(tuple[str, ...], value)


def _json_strings(value: tuple[str, ...]) -> list[JsonValue]:
    return [item for item in value]


@dataclass(frozen=True, slots=True)
class ContributionDraft:
    contribution_id: str
    worker_id: str
    run_id: str
    lease_id: str
    document_id: str
    epoch_pair: EpochPair
    environment_fingerprint: str
    payload: PatchPayload | SnapshotPayload
    candidate_name: str
    complete_type: str
    imports: tuple[str, ...]
    dependencies: tuple[str, ...]
    assumptions: tuple[str, ...]
    evidence_links: tuple[str, ...]

    def __post_init__(self) -> None:
        for value in (
            self.contribution_id,
            self.worker_id,
            self.run_id,
            self.lease_id,
            self.document_id,
            self.candidate_name,
            self.complete_type,
        ):
            _text(value, "INVALID_CONTRIBUTION")
        if type(self.epoch_pair) is not EpochPair:
            raise ContributionValidationError("INVALID_CONTRIBUTION")
        _hash(self.environment_fingerprint, "INVALID_CONTRIBUTION")
        if type(self.payload) not in {PatchPayload, SnapshotPayload}:
            raise ContributionValidationError("INVALID_CONTRIBUTION")
        for value in (self.imports, self.dependencies, self.assumptions, self.evidence_links):
            _strings(value, "INVALID_CONTRIBUTION")


@dataclass(frozen=True, slots=True)
class SubmittedContribution:
    contribution_id: str
    payload_hash: str
    artifact: ArtifactReference
    queue_entry: PublicationQueueEntry


class ContributionService:
    def __init__(
        self,
        state: StateService,
        artifacts: ArtifactStore,
        environment_fingerprint: str,
        allowed_imports: tuple[str, ...],
    ) -> None:
        if type(state) is not StateService or type(artifacts) is not ArtifactStore:
            raise ContributionValidationError("INVALID_CONTRIBUTION_SERVICE")
        self._state = state
        self._artifacts = artifacts
        self._environment = _hash(environment_fingerprint, "INVALID_CONTRIBUTION_SERVICE")
        self._allowed_imports = _strings(allowed_imports, "INVALID_CONTRIBUTION_SERVICE")

    def submit(
        self, draft: ContributionDraft, current_source: bytes | None = None
    ) -> SubmittedContribution:
        if type(draft) is not ContributionDraft:
            raise ContributionValidationError("INVALID_CONTRIBUTION")
        document = self._document(draft)
        source = self._source(draft, document.file_version, document.content_hash, current_source)
        if (
            draft.epoch_pair != document.epoch_pair
            or draft.environment_fingerprint != self._environment
        ):
            raise ContributionValidationError("EPOCH_MISMATCH")
        if any(item not in self._allowed_imports for item in draft.imports):
            raise ContributionValidationError("IMPORT_NOT_ALLOWED")
        validate_candidate_source(
            source, draft.candidate_name, draft.complete_type, self._allowed_imports
        )
        artifact = self._artifacts.store(draft.run_id, "contributions", source, "text/x-lean")
        payload: dict[str, JsonValue] = {
            "contribution_id": draft.contribution_id,
            "worker_id": draft.worker_id,
            "lease_id": draft.lease_id,
            "document_id": draft.document_id,
            "payload_kind": "patch" if type(draft.payload) is PatchPayload else "snapshot",
            "payload_hash": artifact.content_hash,
            "expected_file_version": document.file_version,
            "environment_fingerprint": draft.environment_fingerprint,
            "base_epoch": draft.epoch_pair.base_epoch,
            "knowledge_epoch": draft.epoch_pair.knowledge_epoch,
            "candidate_name": draft.candidate_name,
            "complete_type": draft.complete_type,
            "imports": _json_strings(draft.imports),
            "dependencies": _json_strings(draft.dependencies),
            "assumptions": _json_strings(draft.assumptions),
            "evidence_links": _json_strings(draft.evidence_links),
        }
        if isinstance(draft.payload, PatchPayload):
            payload["expected_content_hash"] = draft.payload.expected_content_hash
            payload["edits"] = _edits(draft.payload)
        try:
            entry = self._state.enqueue_contribution(
                AppendEventCommand(
                    "ContributionSubmitted", "contribution_service", draft.run_id, None, payload
                )
            )
        except ValueError as error:
            if str(error) == "DUPLICATE_CONTRIBUTION_MISMATCH":
                raise ContributionValidationError("DUPLICATE_CONTRIBUTION_MISMATCH") from None
            raise
        return SubmittedContribution(draft.contribution_id, artifact.content_hash, artifact, entry)

    def _document(self, draft: ContributionDraft):
        try:
            return self._state.document_for(
                draft.run_id, draft.worker_id, draft.lease_id, draft.document_id
            )
        except DocumentStateError:
            raise ContributionValidationError("LEASE_ACCESS_DENIED") from None

    @staticmethod
    def _source(
        draft: ContributionDraft, version: int, content_hash: str, current_source: bytes | None
    ) -> bytes:
        payload = draft.payload
        if isinstance(payload, PatchPayload):
            if payload.expected_file_version != version:
                raise ContributionValidationError("PATCH_FILE_VERSION_MISMATCH")
            if payload.expected_content_hash != content_hash or type(current_source) is not bytes:
                raise ContributionValidationError("CONTENT_HASH_MISMATCH")
            return payload.apply(current_source)
        snapshot = payload
        if current_source is not None or sha256_bytes(snapshot.source) != content_hash:
            raise ContributionValidationError("CONTENT_HASH_MISMATCH")
        return snapshot.source


def _edits(payload: PatchPayload) -> list[JsonValue]:
    return [
        {
            "start_byte": edit.start_byte,
            "end_byte": edit.end_byte,
            "replacement_hex": edit.replacement_utf8.hex(),
        }
        for edit in payload.edits
    ]
