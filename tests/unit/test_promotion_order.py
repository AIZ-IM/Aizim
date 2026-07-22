from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aizim.state import (
    AppendEventCommand,
    PublicationQueueState,
    StateDependencies,
    StateService,
    StateServiceConfig,
)

_BASE_EPOCH = "a" * 64


class _Ids:
    def __init__(self) -> None:
        self._value = 0

    def __call__(self) -> str:
        value = self._value
        self._value += 1
        return f"01J{value:023d}"


def _submission(contribution_id: str) -> AppendEventCommand:
    return AppendEventCommand(
        "ContributionSubmitted",
        "contribution_service",
        "run-1",
        None,
        {
            "contribution_id": contribution_id,
            "worker_id": "worker-1",
            "lease_id": "lease-1",
            "document_id": "document-1",
            "payload_kind": "snapshot",
            "payload_hash": "b" * 64,
            "environment_fingerprint": "c" * 64,
            "base_epoch": _BASE_EPOCH,
            "knowledge_epoch": 0,
            "candidate_name": "candidate",
            "complete_type": "True",
            "imports": ["Std"],
            "dependencies": [],
            "assumptions": [],
            "evidence_links": [],
        },
    )


def _service(tmp_path: Path) -> StateService:
    now = datetime(2026, 7, 22, 10, tzinfo=UTC)
    service = StateService(
        StateServiceConfig(tmp_path, "promotion-order"),
        StateDependencies(clock=lambda: now, event_ids=_Ids()),
    )
    service.append_event(
        AppendEventCommand(
            "ProjectInitialized",
            "supervisor",
            None,
            None,
            {"project_id": "project-1", "base_epoch": _BASE_EPOCH, "knowledge_epoch": 0},
        )
    )
    return service


def test_publication_queue_claims_database_order_and_forbids_skips(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        service.enqueue_contribution(_submission("contribution-2"))
        service.enqueue_contribution(_submission("contribution-1"))

        claim = service.claim_next_promotion("owner-a")

        assert claim is not None
        assert claim.contribution_id == "contribution-2"
        assert claim.state is PublicationQueueState.STAGED
        assert service.claim_next_promotion("owner-b") is None
        with pytest.raises(ValueError, match="INVALID_PROMOTION_TRANSITION"):
            service.advance_promotion(
                claim.contribution_id,
                "owner-a",
                PublicationQueueState.MATERIALIZED,
            )

        verified = service.advance_promotion(
            claim.contribution_id, "owner-a", PublicationQueueState.VERIFIED
        )
        materialized = service.advance_promotion(
            claim.contribution_id, "owner-a", PublicationQueueState.MATERIALIZED
        )
        with pytest.raises(ValueError, match="PUBLISHED_REQUIRES_PREPARATION"):
            service.advance_promotion(
                claim.contribution_id, "owner-a", PublicationQueueState.PUBLISHED
            )

        assert [verified.state, materialized.state] == [
            PublicationQueueState.VERIFIED,
            PublicationQueueState.MATERIALIZED,
        ]
        assert service.claim_next_promotion("owner-b") is None
    finally:
        service.close()


def test_expired_owner_reclaim_keeps_the_promotion_state(tmp_path: Path) -> None:
    current = [datetime(2026, 7, 22, 10, tzinfo=UTC)]
    service = StateService(
        StateServiceConfig(tmp_path, "promotion-recovery"),
        StateDependencies(clock=lambda: current[0], event_ids=_Ids()),
    )
    try:
        service.append_event(
            AppendEventCommand(
                "ProjectInitialized",
                "supervisor",
                None,
                None,
                {"project_id": "project-1", "base_epoch": _BASE_EPOCH, "knowledge_epoch": 0},
            )
        )
        service.enqueue_contribution(_submission("contribution-1"))
        assert service.claim_next_promotion("crashed-owner") is not None

        current[0] += timedelta(minutes=2)
        recovered = service.recover_expired_promotion("recovery-owner", timedelta(minutes=1))

        assert recovered is not None
        assert recovered.state is PublicationQueueState.STAGED
        assert recovered.claimed_by == "recovery-owner"
        assert recovered.state_version == 2
        recovered_event = service.query_events()[-1].envelope
        assert recovered_event.payload["former_owner"] == "crashed-owner"
    finally:
        service.close()


def test_heartbeat_keeps_an_active_promotion_claim_fresh(tmp_path: Path) -> None:
    current = [datetime(2026, 7, 22, 10, tzinfo=UTC)]
    service = StateService(
        StateServiceConfig(tmp_path, "promotion-heartbeat"),
        StateDependencies(clock=lambda: current[0], event_ids=_Ids()),
    )
    try:
        service.append_event(
            AppendEventCommand(
                "ProjectInitialized",
                "supervisor",
                None,
                None,
                {"project_id": "project-1", "base_epoch": _BASE_EPOCH, "knowledge_epoch": 0},
            )
        )
        service.enqueue_contribution(_submission("contribution-1"))
        assert service.claim_next_promotion("owner-a") is not None
        current[0] += timedelta(minutes=2)
        refreshed = service.heartbeat_promotion("contribution-1", "owner-a")
        current[0] += timedelta(seconds=30)

        assert refreshed.state is PublicationQueueState.STAGED
        assert service.recover_expired_promotion("owner-b", timedelta(minutes=1)) is None
    finally:
        service.close()


def test_failure_event_and_quarantine_share_one_promotion_transition(tmp_path: Path) -> None:
    service = _service(tmp_path)
    try:
        service.enqueue_contribution(_submission("contribution-1"))
        claimed = service.claim_next_promotion("owner-a")
        assert claimed is not None

        quarantined = service.fail_promotion(
            claimed.contribution_id,
            "owner-a",
            AppendEventCommand(
                "PromotionFailed",
                "promotion_service",
                "run-1",
                None,
                {
                    "contribution_id": claimed.contribution_id,
                    "reason_code": "LEAN_VERIFICATION_FAILED",
                    "artifact_hash": "b" * 64,
                },
            ),
        )

        assert quarantined.state is PublicationQueueState.QUARANTINED
        assert [event.envelope.event_type for event in service.query_events()][-2:] == [
            "PromotionFailed",
            "PromotionStateChanged",
        ]
    finally:
        service.close()
