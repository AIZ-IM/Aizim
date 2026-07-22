from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from aizim.domain import EpochPair
from aizim.state import PublicationQueueState

from .errors import PromotionError

_MODULE = re.compile(r"AizimSmoke\.Research\.[A-Za-z_][A-Za-z0-9_]*")
_HASH = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class PromotionEvidence:
    diagnostics: tuple[str, ...]
    axioms: tuple[str, ...]
    complete_type: str
    dependencies: tuple[str, ...]
    assumptions: tuple[str, ...]
    module_source: bytes | None = None

    def __post_init__(self) -> None:
        if type(self.complete_type) is not str or not self.complete_type:
            raise PromotionError("INVALID_PROMOTION_EVIDENCE")
        for value in (self.diagnostics, self.axioms, self.dependencies, self.assumptions):
            if type(value) is not tuple or any(type(item) is not str or not item for item in value):
                raise PromotionError("INVALID_PROMOTION_EVIDENCE")
        if self.module_source is not None and type(self.module_source) is not bytes:
            raise PromotionError("INVALID_PROMOTION_EVIDENCE")


@dataclass(frozen=True, slots=True)
class PromotionMaterialization:
    module: str
    content_hash: str
    base_epoch: str

    def __post_init__(self) -> None:
        if (
            _MODULE.fullmatch(self.module) is None
            or _HASH.fullmatch(self.content_hash) is None
            or _HASH.fullmatch(self.base_epoch) is None
        ):
            raise PromotionError("INVALID_MATERIALIZATION")


class PromotionVerifier(Protocol):
    async def verify(self, source: bytes, theorem_name: str) -> PromotionEvidence: ...


class PromotionMaterializer(Protocol):
    async def materialize(
        self, source: bytes, epoch_pair: EpochPair, publication_sequence: int
    ) -> PromotionMaterialization: ...

    async def activate(self, materialization: PromotionMaterialization) -> None: ...


@dataclass(frozen=True, slots=True)
class PromotionOutcome:
    contribution_id: str
    state: PublicationQueueState
    new_epoch: EpochPair | None
    rebased_contribution_id: str | None = None


def accepted(evidence: PromotionEvidence) -> bool:
    allowed = {"propext", "Classical.choice", "Quot.sound"}
    return (
        not evidence.diagnostics
        and "sorryAx" not in evidence.axioms
        and set(evidence.axioms) <= allowed
    )
