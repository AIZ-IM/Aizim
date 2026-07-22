from __future__ import annotations

from aizim.domain import EpochPair, sha256_bytes

from .artifacts import ArtifactReference, ArtifactStore
from .errors import PromotionError
from .promotion_types import PromotionMaterialization, PromotionMaterializer


async def materialize(
    module: ArtifactReference,
    current: EpochPair,
    sequence: int,
    artifacts: ArtifactStore,
    materializer: PromotionMaterializer,
) -> PromotionMaterialization:
    source = artifacts.load(module.run_id, module.category, module.content_hash)
    result = await materializer.materialize(source, current, sequence)
    if result.content_hash != sha256_bytes(source):
        raise PromotionError("MATERIALIZATION_HASH_MISMATCH")
    return result
