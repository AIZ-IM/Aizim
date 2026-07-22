from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest

from aizim.domain import EpochPair, FileLease, sha256_bytes, sha256_json
from aizim.knowledge import (
    ArtifactStore,
    ContributionDraft,
    ContributionService,
    PromotionEvidence,
    PromotionMaterialization,
    PromotionService,
    SnapshotPayload,
)
from aizim.state import AppendEventCommand, StateDependencies, StateService, StateServiceConfig

_BASE = "a" * 64
_ENVIRONMENT = "b" * 64
_SOURCE = b"import Std\ntheorem candidate : True := True.intro\n"


class _Verifier:
    async def verify(self, source: bytes, theorem_name: str) -> PromotionEvidence:
        assert source == _SOURCE and theorem_name.startswith("AizimSmoke.Research.candidate_")
        return PromotionEvidence((), (), "True", ("Std",), ())

    async def materialize(
        self, source: bytes, epoch_pair: EpochPair, publication_sequence: int
    ) -> PromotionMaterialization:
        digest = sha256_bytes(source)
        return PromotionMaterialization(
            f"AizimSmoke.Research.M{publication_sequence}_{digest[:16]}",
            digest,
            sha256_json({"previous": epoch_pair.base_epoch, "module": digest}),
        )

    async def activate(self, materialization: PromotionMaterialization) -> None:
        del materialization


class _BlockingVerifier(_Verifier):
    def __init__(self) -> None:
        self.started, self.release = asyncio.Event(), asyncio.Event()

    async def verify(self, source: bytes, theorem_name: str) -> PromotionEvidence:
        self.started.set()
        await self.release.wait()
        return await super().verify(source, theorem_name)


class _InterruptedVerifier(_Verifier):
    async def activate(self, materialization: PromotionMaterialization) -> None:
        del materialization
        raise asyncio.CancelledError


class _CancellationVerifier(_Verifier):
    def __init__(self) -> None:
        self.started, self.cancelled = asyncio.Event(), asyncio.Event()
        self.task: asyncio.Task[PromotionEvidence] | None = None

    async def verify(self, source: bytes, theorem_name: str) -> PromotionEvidence:
        del source, theorem_name
        self.task = asyncio.current_task()
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("unreachable")


class _CancellationService(PromotionService):
    def __init__(
        self,
        state: StateService,
        artifacts: ArtifactStore,
        verifier: _CancellationVerifier,
        owner_id: str,
        materializer: _CancellationVerifier,
    ) -> None:
        super().__init__(state, artifacts, verifier, owner_id, materializer)
        self.heartbeat_cancelled = asyncio.Event()
        self.heartbeat_task: asyncio.Task[None] | None = None

    async def _heartbeats(self, contribution_id: str) -> None:
        del contribution_id
        self.heartbeat_task = asyncio.current_task()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.heartbeat_cancelled.set()
            raise


def _service(tmp_path: Path, clock: Callable[[], datetime]) -> StateService:
    state = StateService(
        StateServiceConfig(tmp_path, "promotion-liveness"), StateDependencies(clock=clock)
    )
    state.append_event(
        AppendEventCommand(
            "ProjectInitialized",
            "supervisor",
            None,
            None,
            {"project_id": "project-1", "base_epoch": _BASE, "knowledge_epoch": 0},
        )
    )
    return state


def _submit(state: StateService, artifacts: ArtifactStore, now: datetime) -> None:
    state.grant_document_lease(
        FileLease(
            "lease-1",
            "worker-1",
            "run-1",
            "document-1",
            "AizimSmoke.Workers.W_worker",
            EpochPair(_BASE, 0),
            0,
            sha256_bytes(_SOURCE),
            now + timedelta(minutes=5),
        ),
        PurePosixPath("AizimSmoke/Workers/run-1/worker-1.lean"),
    )
    ContributionService(state, artifacts, _ENVIRONMENT, ("Std",)).submit(
        ContributionDraft(
            "contribution-1",
            "worker-1",
            "run-1",
            "lease-1",
            "document-1",
            EpochPair(_BASE, 0),
            _ENVIRONMENT,
            SnapshotPayload(_SOURCE, sha256_bytes(_SOURCE)),
            "candidate",
            "True",
            ("Std",),
            (),
            (),
            (),
        )
    )


@pytest.mark.asyncio
async def test_verification_heartbeat_prevents_expired_claim_takeover(tmp_path: Path) -> None:
    def now() -> datetime:
        return datetime.now(UTC)

    state, artifacts = _service(tmp_path, now), ArtifactStore(tmp_path)
    try:
        _submit(state, artifacts, now())
        blocker = _BlockingVerifier()
        active = PromotionService(
            state,
            artifacts,
            blocker,
            "owner-a",
            blocker,
            heartbeat_interval=timedelta(milliseconds=2),
        )
        task = asyncio.create_task(active.promote_next())
        await blocker.started.wait()
        await asyncio.sleep(0.03)

        contender = PromotionService(state, artifacts, _Verifier(), "owner-b", _Verifier())

        assert await contender.recover_next(timedelta(milliseconds=10)) is None
        blocker.release.set()
        assert (await task) is not None
    finally:
        state.close()


@pytest.mark.asyncio
async def test_interrupted_activation_recovers_from_a_durable_preparation(tmp_path: Path) -> None:
    current = [datetime(2030, 1, 1, tzinfo=UTC)]
    state, artifacts = _service(tmp_path, lambda: current[0]), ArtifactStore(tmp_path)
    try:
        _submit(state, artifacts, current[0])
        interrupted = _InterruptedVerifier()

        with pytest.raises(asyncio.CancelledError):
            await PromotionService(
                state, artifacts, interrupted, "owner-a", interrupted
            ).promote_next()

        events = [record.envelope.event_type for record in state.query_events("run-1")]
        assert "PromotionPrepared" in events and "DeclarationPublished" not in events
        current[0] += timedelta(minutes=2)
        recovery = _Verifier()
        outcome = await PromotionService(
            state, artifacts, recovery, "owner-b", recovery
        ).recover_next(timedelta(minutes=1))

        assert outcome is not None and outcome.new_epoch is not None
        assert [record.envelope.event_type for record in state.query_events("run-1")].count(
            "PromotionPrepared"
        ) == 1
    finally:
        state.close()


@pytest.mark.asyncio
async def test_cancelling_promotion_joins_operation_and_heartbeat(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    state, artifacts = _service(tmp_path, lambda: now), ArtifactStore(tmp_path)
    blocker = _CancellationVerifier()
    service = _CancellationService(state, artifacts, blocker, "owner-a", blocker)
    task: asyncio.Task[object] | None = None
    try:
        _submit(state, artifacts, now)
        task = asyncio.create_task(service.promote_next())
        await blocker.started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert blocker.cancelled.is_set()
        assert service.heartbeat_cancelled.is_set()
        assert blocker.task is not None and blocker.task.done()
        assert service.heartbeat_task is not None and service.heartbeat_task.done()
    finally:
        children = tuple(
            child
            for child in (blocker.task, service.heartbeat_task)
            if child is not None and not child.done()
        )
        for child in children:
            child.cancel()
        for child in children:
            await asyncio.gather(child, return_exceptions=True)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        state.close()
