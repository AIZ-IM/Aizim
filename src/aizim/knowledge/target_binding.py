from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from aizim.lean.document_io import DocumentIoError
from aizim.research.records import ResearchStore, get, records, task_hash
from aizim.state.event_payload import thaw_payload

from .artifacts import ArtifactStore
from .contributions import ContributionValidationError, candidate_imports, validate_candidate_source


@dataclass(frozen=True, slots=True)
class TargetBinding:
    target_hash: str
    statement: str
    imports: tuple[str, ...]


def validate_published_target(
    state: ResearchStore,
    project_root: Path,
    run_id: str,
    worker_id: str,
    payload: Mapping[str, object],
) -> TargetBinding | None:
    """Fail closed when historical publication lacks reproducible target evidence."""
    try:
        if payload.get("worker_id") != worker_id:
            _invalid()
        source = ArtifactStore(project_root).load(
            run_id, "contributions", _text(payload.get("payload_hash"))
        )
        imports = payload.get("imports")
        if not isinstance(imports, (list, tuple)):
            _invalid()
        submitted_imports = tuple(_text(item) for item in imports)
        binding = validate_target_context(
            state, run_id, worker_id, _text(payload.get("candidate_name")),
            _text(payload.get("complete_type")), submitted_imports, source,
        )
        if binding is not None:
            validate_candidate_source(
                source, _text(payload.get("candidate_name")), binding.statement, binding.imports
            )
        return binding
    except (DocumentIoError, ValueError, OSError, KeyError, TypeError):
        return None


def validate_target_context(
    state: ResearchStore,
    run_id: str,
    worker_id: str,
    name: str,
    statement: str,
    imports: tuple[str, ...],
    source: bytes,
) -> TargetBinding | None:
    """Reconstruct the host-owned context, including dependencies, from durable history.

    The first attempt record fixes its dependency publications. Later task bookkeeping
    and publication of other declarations cannot expand that attempt's import context.
    Contributions without a research attempt retain their historical unbound behavior.
    """
    from aizim.orchestration.run_identity import candidate_name

    attempts = records(state, "attempt")
    matches = [
        item for item in attempts
        if item.get("run_id") == run_id and item.get("worker_id") == worker_id
    ]
    if not matches:
        if any(
            item.get("run_id") == run_id or item.get("worker_id") == worker_id
            for item in attempts
        ):
            _invalid()
        for record in state.query_events():
            event = record.envelope
            data = event.payload.get("data")
            if (
                event.event_type == "ResearchRecordUpdated"
                and event.payload.get("kind") == "attempt"
                and isinstance(data, Mapping)
                and (data.get("run_id") == run_id or data.get("worker_id") == worker_id)
            ):
                _invalid()
        binding = None
    else:
        if len(matches) != 1:
            _invalid()
        binding = _binding(state, matches[0])
        if name != candidate_name(run_id, worker_id) or statement != binding.statement:
            raise ContributionValidationError("RESEARCH_TARGET_MISMATCH")
        if imports != binding.imports:
            raise ContributionValidationError("RESEARCH_IMPORT_CONTEXT_MISMATCH")
    if candidate_imports(source) != imports:
        raise ContributionValidationError("IMPORT_METADATA_MISMATCH")
    return binding


def _binding(state: ResearchStore, attempt: Mapping[str, object]) -> TargetBinding:
    task_id = _text(attempt.get("task_id"))
    target = get(state, "task", task_id)
    if target is None or target.get("target_hash") != attempt.get("target_hash"):
        _invalid()
    if task_hash(target) != target.get("target_hash"):
        _invalid()
    tasks: dict[str, Mapping[str, object]] = {}
    declarations: dict[str, Mapping[str, object]] = {}
    for record in state.query_events():
        event = record.envelope
        payload = thaw_payload(event.payload)
        if event.event_type == "DeclarationPublished":
            declaration_id = payload.get("declaration_id")
            if type(declaration_id) is str:
                declarations[declaration_id] = payload
        if event.event_type != "ResearchRecordUpdated":
            continue
        data = payload.get("data")
        if type(data) is not dict:
            continue
        if payload.get("kind") == "task":
            identity = data.get("task_id")
            if type(identity) is str:
                tasks[identity] = data
        if payload.get("kind") != "attempt" or data.get("id") != attempt.get("id"):
            continue
        if any(data.get(key) != attempt.get(key) for key in (
            "id", "task_id", "run_id", "worker_id", "target_hash"
        )) or event.run_id != attempt.get("run_id"):
            _invalid()
        original = tasks.get(task_id)
        if original is None or any(original.get(key) != target.get(key) for key in (
            "statement", "imports", "depends_on", "source_refs", "target_hash"
        )):
            _invalid()
        imports = list(dict.fromkeys(_strings(original.get("imports"))))
        for dependency_id in _strings(original.get("depends_on")):
            dependency = tasks.get(dependency_id)
            if dependency is None or dependency.get("status") != "verified":
                _invalid()
            verified = _strings(dependency.get("verified_declarations"))
            if not verified:
                _invalid()
            for declaration_id in verified:
                published = declarations.get(declaration_id)
                projection = state.query_projection("verified_declarations", declaration_id)
                if published is None or projection is None:
                    _invalid()
                projected = json.loads(projection.state_json).get("payload")
                if (
                    type(projected) is not dict
                    or projected.get("module") != published.get("module")
                ):
                    _invalid()
                module = _text(published.get("module"))
                if module not in imports:
                    imports.append(module)
        return TargetBinding(
            _text(target.get("target_hash")), _text(original.get("statement")), tuple(imports)
        )
    _invalid()


def _text(value: object) -> str:
    if type(value) is not str or not value:
        _invalid()
    return value


def _strings(value: object) -> tuple[str, ...]:
    if type(value) is not list:
        _invalid()
    return tuple(_text(item) for item in value)


def _invalid() -> NoReturn:
    raise ContributionValidationError("RESEARCH_TARGET_BINDING_INVALID")
