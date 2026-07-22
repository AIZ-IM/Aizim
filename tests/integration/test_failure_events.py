from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import cast

import pytest

import aizim.orchestration.conductor as conductor_module
from aizim.config.model import ResourcePolicy
from aizim.domain import EpochPair, FileLease, sha256_bytes
from aizim.knowledge import (
    ArtifactStore,
    ContributionDraft,
    ContributionService,
    PromotionEvidence,
    PromotionMaterialization,
    PromotionService,
    RuntimePromotionVerifier,
    SnapshotPayload,
)
from aizim.lean.project import smoke_base_epoch
from aizim.orchestration.knowledge_stream import KnowledgeStream
from aizim.orchestration.promotion_consumer import PromotionConsumer
from aizim.orchestration.resources import ResourceGovernor
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
        raise RuntimeError("PROMOTION_FAILED")


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
@pytest.mark.lean_integration
async def test_failed_promotion_wakes_the_terminal_epoch_waiter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
    verifier = RejectingVerifier()
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

        with pytest.raises(RuntimeError, match="PROMOTION_FAILED"):
            await asyncio.wait_for(waiter, timeout=0.5)
        assert "PromotionFailed" in [
            record.envelope.event_type for record in state.query_events("run-1")
        ]
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
async def test_conductor_aborts_waiting_worker_after_failed_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = StateService(StateServiceConfig(tmp_path, "conductor-failure"))
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
    waiting = tmp_path / "waiting.json"
    waiting.write_text(
        json.dumps(
            {
                "actions": [
                    {
                        "operation": "knowledge.read",
                        "payload": {"after_knowledge_epoch": 0, "wait": True},
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(conductor_module, "PromotionConsumer", FailingConsumer)
    conductor = conductor_module.ResearchConductor(
        state,
        tmp_path,
        _SMOKE_ROOT,
        ResourceGovernor(ResourcePolicy(), disk_free=lambda _path: 3_000_000_000),
    )
    try:
        with pytest.raises(RuntimeError, match="PROMOTION_FAILED"):
            await asyncio.wait_for(conductor.run_fake(waiting, waiting), timeout=1)
        events = [record.envelope.event_type for record in state.query_events()]
        assert "RunAborted" in events
    finally:
        state.close()
