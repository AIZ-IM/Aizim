from __future__ import annotations

import shutil
from hashlib import sha256
from pathlib import Path

import pytest

from aizim.agents import AgentRequest, AgentResult, BackendIdentity
from aizim.domain import AgentRole
from aizim.gateway import CapabilityIssuer
from aizim.orchestration import codex_auditor
from aizim.orchestration.codex_auditor import audit_alignment
from aizim.state import AppendEventCommand, StateService, StateServiceConfig

SMOKE_ROOT = Path(__file__).parents[2] / "examples" / "smoke_lean"


class AlignedBackend:
    identity = BackendIdentity("codex", "codex-cli 0.144.6", "a" * 64)

    def __init__(self) -> None:
        self.request: AgentRequest | None = None

    async def run(self, request: AgentRequest) -> AgentResult:
        self.request = request
        return AgentResult(request.worker_id, "submitted", "aligned", "b" * 64, "c" * 64, 0)


def _project(tmp_path: Path) -> Path:
    return Path(
        shutil.copytree(SMOKE_ROOT, tmp_path / "smoke", ignore=shutil.ignore_patterns(".aizim"))
    )


@pytest.mark.asyncio
async def test_codex_alignment_auditor_records_a_machine_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    state = StateService(StateServiceConfig(project, "audit"))
    state.append_event(
        AppendEventCommand(
            "ProjectInitialized",
            "supervisor",
            None,
            None,
            {"project_id": "smoke", "base_epoch": "a" * 64, "knowledge_epoch": 0},
        )
    )
    for declaration_id, name, module, dependencies in (
        (
            "declaration-a",
            "AizimSmoke.Research.a_add_zero",
            "AizimSmoke.Research.K1",
            ("Std",),
        ),
        (
            "declaration-b",
            "AizimSmoke.Research.b_use_a",
            "AizimSmoke.Research.K2",
            ("AizimSmoke.Research.K1", "Std"),
        ),
    ):
        state.append_event(
            AppendEventCommand(
                "DeclarationPublished",
                "promotion_consumer",
                "shared-audit",
                None,
                {
                    "declaration_id": declaration_id,
                    "name": name,
                    "type": "(n : Nat) : n + 0 = n",
                    "module": module,
                    "dependencies": list(dependencies),
                    "assumptions": [],
                    "axioms": [],
                },
            )
        )
    backend = AlignedBackend()
    raw_token = "auditor-token"

    class FixedIssuer(CapabilityIssuer):
        def __init__(self, service: StateService) -> None:
            super().__init__(service, token_factory=lambda: raw_token)

    monkeypatch.setattr(codex_auditor, "CapabilityIssuer", FixedIssuer)
    try:
        verdict = await audit_alignment(state, project, "shared-audit", backend, "model-fixture")

        review = next(
            record.envelope.payload
            for record in state.query_events("shared-audit")
            if record.envelope.event_type == "AlignmentReviewed"
        )
        assert verdict == "aligned"
        assert review == {
            "review_id": "alignment-shared-audit",
            "kind": "machine",
            "reviewer": "codex-alignment-auditor",
            "verdict": "aligned",
        }
        assert backend.request is not None
        assert backend.request.role is AgentRole.FORMALIZER
        assert backend.request.timeout_seconds == 60.0
        assert '"name":"AizimSmoke.Research.a_add_zero"' in backend.request.prompt
        assert '"name":"AizimSmoke.Research.b_use_a"' in backend.request.prompt
        assert backend.request.prompt.count('"type":"(n : Nat) : n + 0 = n"') == 2
        assert '"dependencies":["AizimSmoke.Research.K1","Std"]' in backend.request.prompt
        assert not backend.request.view_root.exists()
        assert not (project / ".aizim" / "run" / "gateway.sock").exists()
        capability = state.capability_record(sha256(raw_token.encode()).hexdigest())
        assert capability is not None and capability.revoked_at is not None
        assert capability.operations == ("alignment.submit",)
    finally:
        state.close()
