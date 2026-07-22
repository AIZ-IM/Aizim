from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping
from contextlib import suppress
from datetime import timedelta

from aizim.domain import EpochPair, sha256_json
from aizim.domain.serialization import JsonValue
from aizim.state import (
    AppendEventCommand,
    PublicationQueueEntry,
    PublicationQueueState,
    StateService,
)
from aizim.state.event_payload import thaw_payload

from .artifacts import ArtifactError, ArtifactReference, ArtifactStore
from .contributions import ContributionValidationError, validate_candidate_source
from .deltas import publication_delta, research_name
from .errors import PromotionError
from .failures import quarantine_failure
from .promotion_context import (
    contribution_run_id,
    current_epoch,
    document_for,
    find_contribution,
    is_stale,
    strings_field,
    text_field,
)
from .promotion_evidence import record_verification
from .promotion_materialization import materialize
from .promotion_types import (
    PromotionEvidence,
    PromotionMaterialization,
    PromotionMaterializer,
    PromotionOutcome,
    PromotionVerifier,
    accepted,
)
from .rebase import rebase_patch


class PromotionService:
    def __init__(
        self,
        state: StateService,
        artifacts: ArtifactStore,
        verifier: PromotionVerifier,
        owner_id: str,
        materializer: PromotionMaterializer,
        allowed_imports: tuple[str, ...] = ("Std",),
        heartbeat_interval: timedelta = timedelta(seconds=1),
    ) -> None:
        if type(state) is not StateService or type(artifacts) is not ArtifactStore:
            raise PromotionError("INVALID_PROMOTION_SERVICE")
        if type(owner_id) is not str or not owner_id or materializer is None:
            raise PromotionError("INVALID_PROMOTION_SERVICE")
        if type(allowed_imports) is not tuple or any(
            type(item) is not str for item in allowed_imports
        ):
            raise PromotionError("INVALID_PROMOTION_SERVICE")
        if type(heartbeat_interval) is not timedelta or heartbeat_interval <= timedelta(0):
            raise PromotionError("INVALID_PROMOTION_SERVICE")
        self._state, self._artifacts, self._verifier = state, artifacts, verifier
        self._owner_id, self._materializer = owner_id, materializer
        self._allowed_imports, self._heartbeat_interval = allowed_imports, heartbeat_interval

    async def promote_next(self) -> PromotionOutcome | None:
        entry = self._state.claim_next_promotion(self._owner_id)
        return None if entry is None else await self._promote(entry)

    async def recover_next(self, claim_timeout: timedelta) -> PromotionOutcome | None:
        entry = self._state.recover_expired_promotion(self._owner_id, claim_timeout)
        return None if entry is None else await self._promote(entry)

    async def _promote(self, entry: PublicationQueueEntry) -> PromotionOutcome:
        try:
            event, current = (
                find_contribution(self._state, entry.contribution_id),
                current_epoch(self._state),
            )
            if type(event.run_id) is not str:
                raise PromotionError("INVALID_CONTRIBUTION")
            run_id, payload = event.run_id, thaw_payload(event.payload)
            if is_stale(self._state, payload, current):
                return self._rebase(entry, run_id, payload, current)
            source = self._artifacts.load(
                run_id, "contributions", text_field(payload, "payload_hash")
            )
            validate_candidate_source(
                source,
                text_field(payload, "candidate_name"),
                text_field(payload, "complete_type"),
                self._allowed_imports,
            )
            name = research_name(
                text_field(payload, "candidate_name"), text_field(payload, "payload_hash")
            )
            evidence = await self._active(entry, self._verifier.verify(source, name))
            record_verification(self._state, run_id, entry.contribution_id, evidence)
            if not accepted(evidence):
                return self._fail(entry, run_id, "LEAN_VERIFICATION_FAILED", evidence)
            verified = entry
            if entry.state is PublicationQueueState.STAGED:
                verified = self._state.advance_promotion(
                    entry.contribution_id, self._owner_id, PublicationQueueState.VERIFIED
                )
            rendered = source if evidence.module_source is None else evidence.module_source
            module = self._artifacts.store(run_id, "promotions", rendered, "text/x-lean")
            materialized = verified
            if verified.state is PublicationQueueState.VERIFIED:
                materialized = self._state.advance_promotion(
                    entry.contribution_id, self._owner_id, PublicationQueueState.MATERIALIZED
                )
            result = await self._active(
                entry,
                materialize(
                    module,
                    current,
                    materialized.enqueue_sequence,
                    self._artifacts,
                    self._materializer,
                ),
            )
            return await self._publish(
                materialized, run_id, payload, current, module, result, evidence, name
            )
        except (ArtifactError, ContributionValidationError, PromotionError) as error:
            return self._fail(
                entry, contribution_run_id(self._state, entry.contribution_id), str(error), None
            )
        except Exception:
            return self._fail(
                entry,
                contribution_run_id(self._state, entry.contribution_id),
                "PROMOTION_FAILED",
                None,
            )

    async def _publish(
        self,
        entry: PublicationQueueEntry,
        run_id: str,
        payload: Mapping[str, object],
        current: EpochPair,
        module: ArtifactReference,
        materialization: PromotionMaterialization,
        evidence: PromotionEvidence,
        name: str,
    ) -> PromotionOutcome:
        publication = publication_delta(
            entry.contribution_id,
            run_id,
            current,
            materialization.base_epoch,
            module,
            materialization.module,
            name,
            evidence.complete_type,
            evidence.dependencies,
            evidence.assumptions,
            evidence.axioms,
            strings_field(payload, "evidence_links"),
            entry.enqueue_sequence,
        )
        self._state.prepare_promotion(entry.contribution_id, self._owner_id, publication.prepared)
        await self._active(entry, self._materializer.activate(materialization))
        published = self._state.publish_promotion(
            entry.contribution_id, self._owner_id, publication.declaration, publication.delta
        )
        return PromotionOutcome(entry.contribution_id, published.state, publication.new_epoch)

    async def _active[T](self, entry: PublicationQueueEntry, operation: Awaitable[T]) -> T:
        task = asyncio.ensure_future(operation)
        heartbeat = asyncio.create_task(self._heartbeats(entry.contribution_id))
        done, _pending = await asyncio.wait((task, heartbeat), return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            return task.result()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        heartbeat.result()
        raise PromotionError("PROMOTION_CLAIM_DENIED")

    async def _heartbeats(self, contribution_id: str) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval.total_seconds())
            self._state.heartbeat_promotion(contribution_id, self._owner_id)

    def _rebase(
        self,
        entry: PublicationQueueEntry,
        run_id: str,
        payload: dict[str, JsonValue],
        current: EpochPair,
    ) -> PromotionOutcome:
        patch = None
        if text_field(payload, "payload_kind") == "patch":
            document = document_for(self._state, payload)
            if document is None:
                raise PromotionError("DOCUMENT_NOT_FOUND")
            patch = rebase_patch(payload, document, run_id, self._artifacts, self._allowed_imports)
        rebased = sha256_json({"contribution_id": entry.contribution_id, "epoch": current})
        next_payload: dict[str, JsonValue] = {
            **payload,
            "contribution_id": rebased,
            "base_epoch": current.base_epoch,
            "knowledge_epoch": current.knowledge_epoch,
            "rebased_from": entry.contribution_id,
        }
        if patch is not None:
            artifact, version, content_hash = patch
            next_payload.update(
                payload_hash=artifact.content_hash,
                expected_file_version=version,
                expected_content_hash=content_hash,
            )
        self._state.rebase_promotion(
            entry.contribution_id,
            self._owner_id,
            AppendEventCommand(
                "ContributionSubmitted", "promotion_service", run_id, None, next_payload
            ),
            AppendEventCommand(
                "ContributionRebased",
                "promotion_service",
                run_id,
                None,
                {
                    "contribution_id": rebased,
                    "source_contribution_id": entry.contribution_id,
                    "base_epoch": current.base_epoch,
                },
            ),
        )
        return PromotionOutcome(
            entry.contribution_id, PublicationQueueState.QUARANTINED, None, rebased
        )

    def _fail(
        self,
        entry: PublicationQueueEntry,
        run_id: str | None,
        reason: str,
        evidence: PromotionEvidence | None,
    ) -> PromotionOutcome:
        quarantined = quarantine_failure(
            self._state,
            self._artifacts,
            self._owner_id,
            entry,
            run_id,
            reason,
            () if evidence is None else evidence.diagnostics,
            () if evidence is None else evidence.axioms,
        )
        return PromotionOutcome(entry.contribution_id, quarantined.state, None)
