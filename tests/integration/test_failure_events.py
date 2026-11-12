from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import cast

import pytest

from aizim.config.model import ResourcePolicy
from aizim.domain import EpochPair, FileLease, sha256_bytes
from aizim.gateway.socket_alias import ProjectSocketAlias
from aizim.knowledge import (
    ArtifactStore,
    ContributionDraft,
    ContributionService,
    PromotionError,
    PromotionEvidence,
    PromotionMaterialization,
    PromotionService,
    RuntimePromotionVerifier,
    SnapshotPayload,
)
from aizim.lean.project import smoke_base_epoch
from aizim.orchestration.knowledge_stream import KnowledgeStream
from aizim.orchestration.promotion_consumer import PromotionConsumer, PromotionConsumerError
from aizim.orchestration.resources import ResourceGovernor
from aizim.orchestration.worker_host import WorkerExecutionHost, _HostFactories
from aizim.state import AppendEventCommand, StateService, StateServiceConfig

_BASE = "a" * 64
_ENVIRONMENT = "b" * 64
_SMOKE_ROOT = Path(__file__).parents[2] / "examples" / "smoke_lean"


class RejectingVerifier:
    async def verify(self, source: bytes, theorem_name: str) -> PromotionEvidence:
        assert source and theorem_name.startswith("AizimSmoke.Research.")
        return PromotionEvidence(("REJECTED",), (), "True", ("Std",), ())

    async def materialize(
        self, source: bytes, epoch_pair: EpochPair, publication_sequence: int
    ) -> PromotionMaterialization:
        del source, epoch_pair, publication_sequence
        raise AssertionError("rejected promotions are never materialized")

    async def activate(self, materialization: PromotionMaterialization) -> None:
        del materialization


class EpochFailingVerifier(RejectingVerifier):
    async def verify(self, source: bytes, theorem_name: str) -> PromotionEvidence:
        del source, theorem_name
        return PromotionEvidence((), (), "True", ("Std",), ())

    async def materialize(
        self, source: bytes, epoch_pair: EpochPair, publication_sequence: int
    ) -> PromotionMaterialization:
        del source, epoch_pair, publication_sequence
        raise PromotionError("EPOCH_PROJECT_MISMATCH")


class RuntimeFailingVerifier(RejectingVerifier):
    async def verify(self, source: bytes, theorem_name: str) -> PromotionEvidence:
        del source, theorem_name
        raise OSError("runtime failure")


class FailingConsumer:
    def __init__(self, *arguments: object) -> None:
        del arguments
        self._failed = asyncio.Event()

    def notify_submission(self) -> None:
        return None

    async def run(self, stop: asyncio.Event) -> None:
        del stop
        self._failed.set()

    async def wait_for_failure(self) -> None:
        await self._failed.wait()
        raise PromotionConsumerError("PROMOTION_FAILED")

    async def wait_for_epoch(self, epoch: int) -> None:
        del epoch
        await self.wait_for_failure()


def _state(tmp_path: Path) -> StateService:
    state = StateService(StateServiceConfig(tmp_path, "failed-promotion"))
    state.append_event(
        AppendEventCommand(
            "ProjectInitialized",
            "supervisor",
            None,
            None,
            {"project_id": "project", "base_epoch": _BASE, "knowledge_epoch": 0},
        )
    )
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verifier", "reason_code"),
    (
        (RejectingVerifier(), "LEAN_VERIFICATION_FAILED"),
        (EpochFailingVerifier(), "EPOCH_PROJECT_MISMATCH"),
        (RuntimeFailingVerifier(), "PROMOTION_FAILED"),
    ),
)
async def test_failed_promotion_wakes_the_terminal_epoch_waiter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verifier: RejectingVerifier,
    reason_code: str,
) -> None:
    state = _state(tmp_path)
    source = b"import Std\ntheorem candidate : True := True.intro\n"
    state.grant_document_lease(
        FileLease(
            "lease-1",
            "worker-1",
            "run-1",
            "document-1",
            "AizimSmoke.Workers.W_worker",
            EpochPair(_BASE, 0),
            0,
            sha256_bytes(source),
            datetime(2030, 1, 1, tzinfo=UTC),
        ),
        PurePosixPath("AizimSmoke/Workers/run-1/worker-1.lean"),
    )
    artifacts = ArtifactStore(tmp_path)
    ContributionService(state, artifacts, _ENVIRONMENT, ("Std",)).submit(
        ContributionDraft(
            "contribution-1",
            "worker-1",
            "run-1",
            "lease-1",
            "document-1",
            EpochPair(_BASE, 0),
            _ENVIRONMENT,
            SnapshotPayload(source, sha256_bytes(source)),
            "candidate",
            "True",
            ("Std",),
            (),
            (),
            (),
        )
    )
    stop = asyncio.Event()
    service = PromotionService(state, artifacts, verifier, "failure-consumer", verifier)
    consumer = PromotionConsumer(
        state,
        artifacts,
        cast(RuntimePromotionVerifier, object()),
        KnowledgeStream(state),
    )
    monkeypatch.setattr(consumer, "_service", lambda: service)
    worker = asyncio.create_task(consumer.run(stop))
    waiter = asyncio.create_task(consumer.wait_for_epoch(1))
    try:
        await asyncio.sleep(0)
        consumer.notify_submission()

        with pytest.raises(PromotionConsumerError) as raised:
            await asyncio.wait_for(waiter, timeout=0.5)
        assert raised.value.reason_code == reason_code
        with pytest.raises(PromotionConsumerError) as failure:
            await asyncio.wait_for(consumer.wait_for_failure(), timeout=0.5)
        assert failure.value.reason_code == reason_code
        failed = next(
            record.envelope.payload
            for record in state.query_events("run-1")
            if record.envelope.event_type == "PromotionFailed"
        )
        assert failed["contribution_id"] == "contribution-1"
        assert failed["reason_code"] == reason_code
        artifact_hash = failed["artifact_hash"]
        assert type(artifact_hash) is str
        diagnostics = json.loads(artifacts.load("run-1", "diagnostics", artifact_hash))
        assert diagnostics["reason"] == reason_code
    finally:
        if not waiter.done():
            waiter.cancel()
            with suppress(asyncio.CancelledError):
                await waiter
        stop.set()
        consumer.notify_submission()
        await worker
        state.close()


@pytest.mark.asyncio
async def test_worker_host_cancels_waiting_knowledge_read_after_failed_promotion(
    tmp_path: Path,
) -> None:
    state = StateService(StateServiceConfig(tmp_path, "host-promotion-failure"))
    epoch = smoke_base_epoch(_SMOKE_ROOT)
    state.append_event(
        AppendEventCommand(
            "ProjectInitialized",
            "supervisor",
            None,
            None,
            {"project_id": "project", "base_epoch": epoch, "knowledge_epoch": 0},
        )
    )
    host = await WorkerExecutionHost._open_with_factories(
        state,
        tmp_path,
        _SMOKE_ROOT,
        ResourceGovernor(ResourcePolicy(), disk_free=lambda _path: 3_000_000_000),
        "run-1",
        _HostFactories(ProjectSocketAlias, FailingConsumer),
    )
    waiting = asyncio.create_task(KnowledgeStream(state).read("run-1", "worker-1", 0, wait=True))
    try:
        with pytest.raises(PromotionConsumerError, match="PROMOTION_FAILED"):
            await asyncio.wait_for(host._race_failure(waiting), timeout=1)
        assert waiting.cancelled()
        assert state.active_document_leases() == ()
    finally:
        try:
            await host.aclose()
            assert not (tmp_path / ".aizim/run/gateway.sock").exists()
        finally:
            state.close()
