from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest

from aizim.cli.init_command import run_init
from aizim.domain import FileLease, sha256_bytes, sha256_json
from aizim.domain.serialization import JsonValue
from aizim.knowledge import (
    ArtifactStore,
    ContributionDraft,
    ContributionService,
    ContributionValidationError,
    PromotionEvidence,
    PromotionMaterialization,
    PromotionService,
    SnapshotPayload,
)
from aizim.knowledge.promotion_context import current_epoch
from aizim.knowledge.target_binding import validate_published_target
from aizim.orchestration.run_identity import candidate_name
from aizim.research.engine import ResearchEngine
from aizim.research.records import define_task, require_task, save
from aizim.state import (
    AppendEventCommand,
    PublicationQueueState,
    StateDependencies,
    StateService,
    StateServiceConfig,
)


@pytest.mark.parametrize("mismatch", ["statement", "candidate", "worker", "run"])
async def test_published_other_target_cannot_verify_on_recovery(
    tmp_path: Path, mismatch: str
) -> None:
    project = Path(
        shutil.copytree(Path(__file__).parents[1] / "fixtures/research_lean", tmp_path / "project")
    )
    assert run_init(project) == 0
    with StateService(StateServiceConfig(project, "binding-test")) as state:
        define_task(state, {
            "task_id": "addition", "title": "Addition identity",
            "statement": "(n : Nat) : n + 0 = n", "author": "Ada",
            "imports": ["Std"], "depends_on": [], "source_refs": [],
            "max_rounds": 1, "max_failures": 1, "timeout_seconds": 5,
        })

        async def never(*args):
            raise AssertionError("recovery must settle before dispatch")

        engine = ResearchEngine(state, never, "previous-run", "model")
        attempt = engine._begin(require_task(state, "addition"))
        run, worker = attempt["run_id"], attempt["worker_id"]
        assert isinstance(run, str)
        assert isinstance(worker, str)
        payload: dict[str, JsonValue] = {
            "contribution_id": "other-contribution",
            "worker_id": "other-worker" if mismatch == "worker" else worker,
            "candidate_name": (
                "other_candidate" if mismatch == "candidate" else candidate_name(run, worker)
            ),
            "complete_type": "True" if mismatch == "statement" else "(n : Nat) : n + 0 = n",
        }
        event_run = "other-run" if mismatch == "run" else run
        state.append_event(
            AppendEventCommand("ContributionSubmitted", "fixture", event_run, None, payload)
        )
        state.append_event(AppendEventCommand("DeclarationPublished", "fixture", event_run, None, {
            "contribution_id": "other-contribution", "declaration_id": "other-declaration",
        }))
        engine.recover()
        assert require_task(state, "addition")["status"] != "verified"


_BINDING_NOW = datetime(2030, 1, 1, tzinfo=UTC)
_BINDING_ENV = "b" * 64


def _binding_service(project: Path, now: datetime = _BINDING_NOW) -> StateService:
    state = StateService(
        StateServiceConfig(project, "formal-binding"), StateDependencies(clock=lambda: now)
    )
    if not any(item.envelope.event_type == "ProjectInitialized" for item in state.query_events()):
        state.append_event(AppendEventCommand("ProjectInitialized", "fixture", None, None, {
            "project_id": "project-1", "base_epoch": "a" * 64, "knowledge_epoch": 0,
            "environment_fingerprint": _BINDING_ENV,
        }))
    return state


def _binding_task(state: StateService, identity: str, imports=("Std",), dependencies=()) -> None:
    define_task(state, {
        "task_id": identity, "title": identity, "statement": "True", "author": "Ada",
        "imports": list(imports), "depends_on": list(dependencies), "source_refs": [],
        "max_rounds": 2, "max_failures": 2, "timeout_seconds": 5,
    })


def _binding_attempt(state: StateService, identity: str = "target"):
    async def unused(*args):
        raise AssertionError("submission tests do not dispatch workers")

    return ResearchEngine(state, unused, "research-run", "model")._begin(
        require_task(state, identity)
    )


def _binding_source(name: str, imports: tuple[str, ...]) -> bytes:
    return ("".join(f"import {module}\n" for module in imports)
            + f"theorem {name} : True := True.intro\n").encode()


def _binding_draft(
    state, source, *, run="research-run", worker, imports=("Std",), name=None,
    identity="contribution",
):
    epoch = current_epoch(state)
    lease = FileLease(
        lease_id=f"lease-{identity}", worker_id=worker, run_id=run,
        document_id=f"document-{identity}",
        virtual_document_namespace="AizimSmoke.Workers.W_binding",
        epoch_pair=epoch, file_version=0, content_hash=sha256_bytes(source),
        expires_at=_BINDING_NOW + timedelta(days=1),
    )
    state.grant_document_lease(lease, PurePosixPath(f"AizimSmoke/Workers/{run}/{worker}.lean"))
    return ContributionDraft(
        identity, worker, run, lease.lease_id, lease.document_id, epoch, _BINDING_ENV,
        SnapshotPayload(source, sha256_bytes(source)), name or candidate_name(run, worker),
        "True", imports, (), (), (),
    )


class _BindingFormalHost:
    def __init__(self):
        self.sources = []

    async def verify(self, source, theorem_name):
        self.sources.append(source)
        return PromotionEvidence((), (), "True", (), (), axiom_response_hash="c" * 64)

    async def materialize(self, source, epoch_pair, publication_sequence):
        digest = sha256_bytes(source)
        return PromotionMaterialization(
            f"AizimSmoke.Research.M{publication_sequence}_{digest[:16]}", digest,
            sha256_json({"previous": epoch_pair.base_epoch, "module": digest}),
        )

    async def activate(self, materialization):
        pass


@pytest.mark.parametrize("source_imports,metadata", [
    (("Std", "Mathlib"), ("Std", "Mathlib")),
    (("Mathlib",), ("Mathlib",)),
    (("Std", "Mathlib"), ("Std",)),
    (("Std",), ("Std", "Mathlib")),
])
def test_research_submission_rejects_changed_import_context(tmp_path, source_imports, metadata):
    with _binding_service(tmp_path) as state:
        _binding_task(state, "target")
        attempt = _binding_attempt(state)
        worker = attempt["worker_id"]
        source = _binding_source(candidate_name("research-run", worker), source_imports)
        draft = _binding_draft(state, source, worker=worker, imports=metadata)
        with pytest.raises(
            ContributionValidationError, match=r"IMPORT_(CONTEXT|METADATA)_MISMATCH"
        ):
            ContributionService(
                state, ArtifactStore(tmp_path), _BINDING_ENV, ("Std", "Mathlib")
            ).submit(draft)
        assert not any(
            item.envelope.event_type == "ContributionSubmitted" for item in state.query_events()
        )


@pytest.mark.parametrize("mismatch", ["candidate", "statement", "hash", "missing_target", "worker"])
def test_research_submission_requires_complete_trusted_attempt(tmp_path, mismatch):
    with _binding_service(tmp_path) as state:
        _binding_task(state, "target")
        attempt = _binding_attempt(state)
        worker = "unbound-worker" if mismatch == "worker" else attempt["worker_id"]
        name = "other" if mismatch == "candidate" else candidate_name("research-run", worker)
        source = _binding_source(name, ("Std",))
        draft = _binding_draft(state, source, worker=worker, name=name)
        if mismatch == "statement":
            draft = replace(draft, complete_type="False")
        if mismatch in {"hash", "missing_target"}:
            field, value = (
                ("target_hash", "0" * 64) if mismatch == "hash" else ("task_id", "missing")
            )
            save(
                state, "attempt", attempt["id"], {**attempt, field: value},
                actor="research_supervisor", run_id="research-run",
            )
        with pytest.raises(ContributionValidationError, match="RESEARCH_TARGET"):
            ContributionService(state, ArtifactStore(tmp_path), _BINDING_ENV, ("Std",)).submit(
                draft
            )


def test_research_submission_preserves_generated_import_order(tmp_path):
    with _binding_service(tmp_path) as state:
        _binding_task(state, "target", imports=("Std", "Mathlib"))
        attempt = _binding_attempt(state)
        worker = attempt["worker_id"]
        source = _binding_source(candidate_name("research-run", worker), ("Mathlib", "Std"))
        draft = _binding_draft(state, source, worker=worker, imports=("Mathlib", "Std"))
        with pytest.raises(ContributionValidationError, match="RESEARCH_IMPORT_CONTEXT_MISMATCH"):
            ContributionService(
                state, ArtifactStore(tmp_path), _BINDING_ENV, ("Std", "Mathlib")
            ).submit(draft)


@pytest.mark.parametrize(
    "header", ["import Std Mathlib\n", "import Std\n Mathlib\n", "prelude\nimport Std\n"]
)
def test_research_submission_reads_all_header_imports(tmp_path, header):
    with _binding_service(tmp_path) as state:
        _binding_task(state, "target")
        attempt = _binding_attempt(state)
        worker = attempt["worker_id"]
        source = (
            header + f"theorem {candidate_name('research-run', worker)} : True := True.intro\n"
        ).encode()
        draft = _binding_draft(state, source, worker=worker)
        with pytest.raises(
            ContributionValidationError, match=r"IMPORT_METADATA_MISMATCH|INVALID_IMPORT_CONTEXT"
        ):
            ContributionService(
                state, ArtifactStore(tmp_path), _BINDING_ENV, ("Std", "Mathlib")
            ).submit(draft)


async def test_dependency_context_survives_restart_and_publication_bookkeeping(tmp_path):
    host = _BindingFormalHost()
    with _binding_service(tmp_path) as state:
        _binding_task(state, "parent")
        parent_source = _binding_source("parent_candidate", ("Std",))
        parent_draft = _binding_draft(
            state, parent_source, run="legacy-run", worker="legacy-worker",
            name="parent_candidate", identity="parent-contribution",
        )
        artifacts = ArtifactStore(tmp_path)
        ContributionService(state, artifacts, _BINDING_ENV, ("Std",)).submit(parent_draft)
        outcome = await PromotionService(
            state, artifacts, host, "parent-owner", host
        ).promote_next()
        assert outcome is not None
        assert outcome.state is PublicationQueueState.PUBLISHED
        publication = next(
            item.envelope.payload for item in state.query_events()
            if item.envelope.event_type == "DeclarationPublished"
        )
        declaration_id = publication["declaration_id"]
        assert isinstance(declaration_id, str)
        module = publication["module"]
        assert isinstance(module, str)
        parent = require_task(state, "parent")
        save(
            state, "task", "parent",
            {
                **parent, "status": "verified",
                "verified_declarations": [declaration_id],
            },
            actor="research_supervisor",
        )
        _binding_task(state, "target", imports=("Std", "Std"), dependencies=("parent",))
        attempt = _binding_attempt(state)
        worker = attempt["worker_id"]
        imports = ("Std", module)
        source = _binding_source(candidate_name("research-run", worker), imports)
        draft = _binding_draft(state, source, worker=worker, imports=imports)
        ContributionService(state, artifacts, _BINDING_ENV, ("Std",)).submit(draft)
        # Later bookkeeping must not replace the publications captured at attempt creation.
        save(
            state, "task", "parent", {**parent, "status": "verified", "verified_declarations": []},
            actor="research_supervisor",
        )
        assert state.claim_next_promotion("crashed-owner") is not None
        event = next(
            item.envelope for item in state.query_events()
            if item.envelope.event_type == "ContributionSubmitted"
            and item.envelope.payload["contribution_id"] == "contribution"
        )
        digest = state.logical_digest()
    with _binding_service(tmp_path, _BINDING_NOW + timedelta(minutes=10)) as state:
        assert state.replay_verify().matched
        assert state.logical_digest() == digest
        binding = validate_published_target(state, tmp_path, "research-run", worker, event.payload)
        assert binding is not None and binding.target_hash == attempt["target_hash"]
        outcome = await PromotionService(
            state, ArtifactStore(tmp_path), host, "recovered-owner", host
        ).recover_next(timedelta(minutes=1))
        assert outcome is not None
        assert outcome.state is PublicationQueueState.PUBLISHED
        assert host.sources[-1] == source


@pytest.mark.parametrize("source_imports,metadata", [
    (("Std", "Mathlib"), ["Std"]),
    (("Mathlib",), ["Mathlib"]),
    (("Std",), ["Std", "Mathlib"]),
])
async def test_promotion_revalidates_historical_research_artifact(
    tmp_path, source_imports, metadata
):
    with _binding_service(tmp_path) as state:
        _binding_task(state, "target")
        attempt = _binding_attempt(state)
        worker = attempt["worker_id"]
        name = candidate_name("research-run", worker)
        source = _binding_source(name, source_imports)
        artifact = ArtifactStore(tmp_path).store(
            "research-run", "contributions", source, "text/x-lean"
        )
        state.enqueue_contribution(AppendEventCommand(
            "ContributionSubmitted", "historical_service", "research-run", None, {
            "contribution_id": "historical", "worker_id": worker, "lease_id": "legacy-lease",
            "document_id": "legacy-document", "payload_kind": "snapshot",
            "payload_hash": artifact.content_hash,
            "environment_fingerprint": _BINDING_ENV, "base_epoch": "a" * 64, "knowledge_epoch": 0,
            "candidate_name": name, "complete_type": "True", "imports": metadata,
            "dependencies": [], "assumptions": [], "evidence_links": [],
        }))
    with _binding_service(tmp_path) as state:
        assert state.replay_verify().matched
        payload = next(
            item.envelope.payload for item in state.query_events()
            if item.envelope.event_type == "ContributionSubmitted"
        )
        assert validate_published_target(state, tmp_path, "research-run", worker, payload) is None
        host = _BindingFormalHost()
        outcome = await PromotionService(
            state, ArtifactStore(tmp_path), host, "owner", host, allowed_imports=("Std", "Mathlib")
        ).promote_next()
        assert outcome is not None
        assert outcome.state is PublicationQueueState.QUARANTINED
        assert host.sources == []
        assert not any(
            item.envelope.event_type == "DeclarationPublished" for item in state.query_events()
        )


async def test_nonresearch_historical_contribution_remains_publishable_after_replay(tmp_path):
    with _binding_service(tmp_path) as state:
        source = _binding_source("legacy_candidate", ("Std",))
        draft = _binding_draft(
            state, source, run="legacy-run", worker="legacy-worker", name="legacy_candidate"
        )
        ContributionService(state, ArtifactStore(tmp_path), _BINDING_ENV, ("Std",)).submit(draft)
        event = next(
            item.envelope for item in state.query_events()
            if item.envelope.event_type == "ContributionSubmitted"
        )
        assert "target_hash" not in event.payload
    with _binding_service(tmp_path) as state:
        assert state.replay_verify().matched
        host = _BindingFormalHost()
        outcome = await PromotionService(
            state, ArtifactStore(tmp_path), host, "owner", host
        ).promote_next()
        assert outcome is not None
        assert outcome.state is PublicationQueueState.PUBLISHED


def test_settlement_rejects_missing_artifact_and_unbound_historical_publication(tmp_path):
    with _binding_service(tmp_path) as state:
        _binding_task(state, "target")
        attempt = _binding_attempt(state)
        worker = attempt["worker_id"]
        payload = {
            "worker_id": worker, "payload_hash": "0" * 64,
            "candidate_name": candidate_name("research-run", worker),
            "complete_type": "True", "imports": ["Std"],
        }
        assert validate_published_target(state, tmp_path, "research-run", worker, payload) is None
        source = _binding_source("legacy_candidate", ("Std",))
        artifact = ArtifactStore(tmp_path).store(
            "legacy-run", "contributions", source, "text/x-lean"
        )
        legacy_payload = {
            **payload, "worker_id": "legacy-worker", "payload_hash": artifact.content_hash,
            "candidate_name": "legacy_candidate",
        }
        assert validate_published_target(
            state, tmp_path, "legacy-run", "legacy-worker", legacy_payload
        ) is None
