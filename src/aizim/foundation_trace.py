from __future__ import annotations

from aizim import foundation_contract as contract
from aizim import foundation_evidence as fe
from aizim.domain import sha256_bytes
from aizim.modes.formal_trace import (
    build_formal_trace_sources,
    formal_trace_bytes,
    read_formal_trace,
    replay_formal_trace,
)


def trace_complete(
    events: tuple[fe.EventEvidence, ...], evidence: fe.FoundationEvidence
) -> bool:
    try:
        body = fe.artifact(evidence, "formal-trace.jsonl").body
        expected = fe.artifact(evidence, "formal-trace.sha256").body.decode().strip()
        trace = read_formal_trace(body)
    except (UnicodeDecodeError, ValueError):
        return False
    seals = tuple(event for event in events if event.event_type == "FormalTraceSealed")
    if len(seals) != 1 or seals[0].payload != {"cutoff_kind": "evaluation_artifacts"}:
        return False
    tail = tuple(
        (event.event_type, event.payload.get("artifact_name"))
        for event in events
        if event.sequence > seals[0].sequence
    )
    expected_tail = tuple(
        ("ArtifactRegistered", name)
        for name in ("formal-trace.jsonl", "formal-trace.sha256", "acceptance-report.json")
    )
    if tail != expected_tail:
        return False
    sources = tuple(
        (event.sequence, event.event_id, event.event_type, event.payload)
        for event in events
        if event.sequence <= seals[0].sequence
    )
    regenerated = build_formal_trace_sources(sources)
    return (
        trace == regenerated
        and body == formal_trace_bytes(regenerated)
        and expected == sha256_bytes(body)
        and replay_formal_trace(trace, expected)
        and {item.kind for item in trace} >= contract.TRACE_KINDS
    )
