from __future__ import annotations

from typing import Final

from aizim.domain.serialization import JsonValue, canonical_json

from .event_payload import thaw_payload
from .events import EventEnvelope
from .projections import ProjectionRecord

_EVENT_PROJECTION: Final = {
    "ProjectInitialized": "project",
    "RunCreated": "runs",
    "RunCompleted": "runs",
    "RunAborted": "runs",
    "WorkerRegistered": "workers",
    "WorkerStarted": "workers",
    "WorkerStopped": "workers",
    "WorkerCrashed": "workers",
    "CapabilityMinted": "resources",
    "CapabilityDenied": "denials",
    "SandboxProbeStarted": "resources",
    "SandboxProbeDenied": "denials",
    "SandboxProbePassed": "resources",
    "SandboxProbeFailed": "denials",
    "LeaseGranted": "leases",
    "LeaseReleased": "leases",
    "LeaseRecovered": "leases",
    "DocumentEditPrepared": "documents",
    "DocumentEdited": "documents",
    "DocumentEditRecovered": "documents",
    "FormalActionRecorded": "documents",
    "ContributionSubmitted": "contributions",
    "ContributionRebased": "contributions",
    "ContributionEnqueued": "contributions",
    "PromotionStateChanged": "contributions",
    "PromotionFailed": "contributions",
    "DeclarationPublished": "verified_declarations",
    "KnowledgeDeltaPublished": "knowledge_deltas",
    "LeanRuntimeStarted": "resources",
    "LeanRuntimeCrashed": "resources",
    "LeanRuntimeRestarted": "resources",
    "EnvironmentTransitionProposed": "project",
    "EnvironmentTransitionApproved": "project",
    "EnvironmentTransitionRejected": "project",
    "AlignmentReviewed": "alignment_reviews",
    "InterventionRecorded": "interventions",
}

_ENTITY_FIELD: Final = {
    "ProjectInitialized": "project_id",
    "WorkerRegistered": "worker_id",
    "WorkerStarted": "worker_id",
    "WorkerStopped": "worker_id",
    "WorkerCrashed": "worker_id",
    "CapabilityDenied": "request_id",
    "SandboxProbeStarted": "probe_id",
    "SandboxProbeDenied": "probe_id",
    "SandboxProbePassed": "probe_id",
    "SandboxProbeFailed": "probe_id",
    "LeaseGranted": "lease_id",
    "LeaseReleased": "lease_id",
    "LeaseRecovered": "lease_id",
    "DocumentEditPrepared": "document_id",
    "DocumentEdited": "document_id",
    "DocumentEditRecovered": "document_id",
    "FormalActionRecorded": "action_id",
    "ContributionSubmitted": "contribution_id",
    "ContributionRebased": "contribution_id",
    "ContributionEnqueued": "contribution_id",
    "PromotionStateChanged": "contribution_id",
    "PromotionFailed": "contribution_id",
    "DeclarationPublished": "declaration_id",
    "KnowledgeDeltaPublished": "delta_id",
    "LeanRuntimeStarted": "runtime_id",
    "LeanRuntimeCrashed": "runtime_id",
    "LeanRuntimeRestarted": "runtime_id",
    "EnvironmentTransitionProposed": "transition_id",
    "EnvironmentTransitionApproved": "transition_id",
    "EnvironmentTransitionRejected": "transition_id",
    "AlignmentReviewed": "review_id",
    "InterventionRecorded": "intervention_id",
}


def _entity_id(event: EventEnvelope) -> str:
    field = _ENTITY_FIELD.get(event.event_type)
    value = event.payload.get(field) if field is not None else event.run_id
    return value if type(value) is str and value else event.event_id


def _next_version(
    snapshots: tuple[ProjectionRecord, ...], projection_name: str, entity_id: str
) -> int:
    current = next(
        (
            item
            for item in snapshots
            if item.projection_name == projection_name and item.entity_id == entity_id
        ),
        None,
    )
    return 1 if current is None else current.version + 1


def _record(
    snapshots: tuple[ProjectionRecord, ...],
    projection_name: str,
    entity_id: str,
    state: dict[str, JsonValue],
) -> ProjectionRecord:
    return ProjectionRecord(
        projection_name=projection_name,
        entity_id=entity_id,
        version=_next_version(snapshots, projection_name, entity_id),
        state_json=canonical_json(state),
    )


def apply_event(
    snapshots: tuple[ProjectionRecord, ...], event: EventEnvelope
) -> tuple[ProjectionRecord, ...]:
    payload = thaw_payload(event.payload)
    projection_name = _EVENT_PROJECTION[event.event_type]
    entity_id = _entity_id(event)
    changes = [
        _record(
            snapshots,
            projection_name,
            entity_id,
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "payload": payload,
                "run_id": event.run_id,
            },
        )
    ]
    if event.event_type in {"ProjectInitialized", "KnowledgeDeltaPublished"}:
        changes.append(
            _record(
                snapshots,
                "epochs",
                "global",
                {
                    "base_epoch": payload["base_epoch"],
                    "knowledge_epoch": payload["knowledge_epoch"],
                },
            )
        )
    return tuple(changes)
