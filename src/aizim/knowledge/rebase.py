from __future__ import annotations

from collections.abc import Mapping

from aizim.domain.serialization import JsonValue

from .artifacts import ArtifactReference, ArtifactStore
from .contributions import ByteEdit, PatchPayload, validate_candidate_source
from .errors import PromotionError


def rebase_patch(
    payload: Mapping[str, JsonValue],
    document: Mapping[str, object],
    run_id: str,
    artifacts: ArtifactStore,
    allowed_imports: tuple[str, ...],
) -> tuple[ArtifactReference, int, str]:
    version, content_hash = _integer(document, "version"), _text(document, "content_hash")
    source = artifacts.load(run_id, "documents", content_hash)
    patch = PatchPayload(version, content_hash, _edits(payload))
    rebased = patch.apply(source)
    validate_candidate_source(
        rebased,
        _text(payload, "candidate_name"),
        _text(payload, "complete_type"),
        allowed_imports,
    )
    return artifacts.store(run_id, "contributions", rebased, "text/x-lean"), version, content_hash


def _edits(payload: Mapping[str, JsonValue]) -> tuple[ByteEdit, ...]:
    value = payload.get("edits")
    if type(value) is not list:
        raise PromotionError("INVALID_PATCH_EDITS")
    edits: list[ByteEdit] = []
    for item in value:
        if type(item) is not dict or set(item) != {"start_byte", "end_byte", "replacement_hex"}:
            raise PromotionError("INVALID_PATCH_EDITS")
        try:
            replacement = bytes.fromhex(_text(item, "replacement_hex"))
        except ValueError:
            raise PromotionError("INVALID_PATCH_EDITS") from None
        edits.append(
            ByteEdit(_integer(item, "start_byte"), _integer(item, "end_byte"), replacement)
        )
    return tuple(edits)


def _text(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if type(value) is not str or not value:
        raise PromotionError("INVALID_PATCH_EDITS")
    return value


def _integer(payload: Mapping[str, object], field: str) -> int:
    value = payload.get(field)
    if type(value) is not int or value < 0:
        raise PromotionError("INVALID_PATCH_EDITS")
    return value
