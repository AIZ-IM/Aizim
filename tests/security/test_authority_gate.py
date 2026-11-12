from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import pytest

from aizim.agents.sandbox import GATE_COMPLETION_OPERATION, ProbeOperation
from aizim.cli import security_probe_command
from aizim.domain.serialization import JsonValue
from aizim.gateway import (
    CapabilityGateway,
    CapabilitySession,
    GatewayFailure,
    GatewaySuccess,
    GatewayTool,
)
from aizim.gateway.authority_probe import AuthorityProbeError, run_authority_probe
from aizim.runtime.layout import ProjectLayout
from aizim.security_gate import (
    SecurityGateError,
    run_security_gate,
)
from aizim.state import StateService, StateServiceConfig

FIXTURE = Path(__file__).parents[1] / "fixtures" / "attack_probe_project"
TOKEN_SENTINEL = "Task8CapabilitySentinel_8a6e1d9f65b7439d"
SECRET_SENTINEL = "Task8SecretSentinel_163bc21e6a804f57"
GATEWAY_DENIALS = (
    "MISSING_TOKEN",
    "UNKNOWN_TOKEN",
    "ROLE_MISMATCH",
    "UNADVERTISED_OPERATION",
)
PASS_LINES = (
    "SECURITY GATE PASS", "gateway_matrix=pass", "seatbelt_profile=pass",
    "filesystem_denials=6/6", "socket_denials=1/1", "environment_denials=1/1",
    "allowed_controls=3/3", "network_destination_isolation=not_enforced",
    "protected_assets=unchanged", "protected_state=unchanged",
    "omp can access other local and external TCP destinations; "
    "sandbox does not enforce destination isolation",
)
LINUX_PASS_LINES = (*PASS_LINES[:2], "linux_profile=pass", *PASS_LINES[3:])


@dataclass(frozen=True, slots=True)
class _PlatformReport:
    passed: bool
    platform_id: str
    network_boundary: str = PASS_LINES[-1]


def copy_project(tmp_path: Path, name: str) -> Path:
    return Path(
        shutil.copytree(
            FIXTURE,
            tmp_path / name,
            ignore=shutil.ignore_patterns(".aizim"),
        )
    )


def test_authority_probe_revokes_capability_after_unexpected_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = copy_project(tmp_path, "cleanup-project")
    layout = ProjectLayout.from_lean_project(root)
    layout.prepare_runtime()
    raw_token = "authority-probe-cleanup-token"
    monkeypatch.setattr("aizim.gateway.capabilities.secrets.token_urlsafe", lambda _size: raw_token)
    original_call = CapabilityGateway.call

    async def unexpected_dispatch(
        gateway: CapabilityGateway,
        session: CapabilitySession,
        operation: GatewayTool | str,
        payload: dict[str, JsonValue],
    ) -> GatewaySuccess | GatewayFailure:
        remapped = GatewayTool.STATE_QUERY if operation == GatewayTool.STATE_APPEND else operation
        return await original_call(gateway, session, remapped, payload)

    monkeypatch.setattr(CapabilityGateway, "call", unexpected_dispatch)
    with StateService(StateServiceConfig(root, "authority-cleanup")) as state:
        with pytest.raises(AuthorityProbeError, match="authorization attack reached a target"):
            asyncio.run(run_authority_probe(state, "cleanup-run"))

        record = state.capability_record(sha256(raw_token.encode()).hexdigest())
        assert record is not None
        assert record.revoked_at is not None

@pytest.mark.parametrize(
    "inspection",
    [
        {"protocol_version": 1, "ambient_disabled": True, "tool_names": (),
         "model": "magpie/model"},
        {"protocol_version": 2, "ambient_disabled": False, "tool_names": (),
         "model": "magpie/model"},
        {"protocol_version": 2, "ambient_disabled": True, "tool_names": ("shell",),
         "model": "magpie/model"},
        {"protocol_version": 2, "ambient_disabled": True, "tool_names": (), "model": ""},
        {"ambient_disabled": True, "tool_names": (), "model": "magpie/model"},
    ],
)
def test_gate_rejects_unrestricted_or_unconfigured_rpc_before_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inspection: dict[str, object],
) -> None:
    root = copy_project(tmp_path, "rpc-preflight")
    ProjectLayout.from_lean_project(root).prepare_runtime()
    with StateService(StateServiceConfig(root, "rpc-preflight-init")):
        pass

    async def inspect(_root: Path, _environment: object) -> dict[str, object]:
        return inspection

    def unexpected_resolve(_environment: object) -> None:
        raise AssertionError("gate proceeded past failed RPC preflight")

    monkeypatch.setattr("aizim.security_gate.inspect_omp_worker", inspect)
    monkeypatch.setattr("aizim.security_gate.resolve_omp", unexpected_resolve)
    with pytest.raises(
        SecurityGateError,
        match=(
            r"omp RPC protocol or ambient tool restriction is invalid"
            r"|omp has no selected magpie model"
        ),
    ):
        asyncio.run(run_security_gate(root))


@pytest.mark.macos_sandbox
def test_live_gate_revokes_capability_when_digest_setup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = copy_project(tmp_path, "digest-fault-project")
    layout = ProjectLayout.from_lean_project(root)
    layout.prepare_runtime()
    with StateService(StateServiceConfig(root, "authority-fault-init")):
        pass
    raw_token = "authority-live-digest-fault-token"
    monkeypatch.setattr("aizim.gateway.capabilities.secrets.token_urlsafe", lambda _size: raw_token)
    original_digest = StateService.logical_digest

    def fail_digest(_state: StateService) -> str:
        raise RuntimeError("injected logical digest failure")

    monkeypatch.setattr(StateService, "logical_digest", fail_digest)
    with pytest.raises(RuntimeError, match="injected logical digest failure"):
        asyncio.run(run_security_gate(root))
    monkeypatch.setattr(StateService, "logical_digest", original_digest)

    with StateService(StateServiceConfig(root, "authority-cleanup-check")) as restarted:
        record = restarted.capability_record(sha256(raw_token.encode()).hexdigest())
        assert record is not None
        assert record.revoked_at is not None


@pytest.mark.macos_sandbox
def test_composite_authority_gate_denies_attacks_and_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = copy_project(tmp_path, "lean-project")
    layout = ProjectLayout.from_lean_project(root)
    layout.prepare_runtime()
    with StateService(StateServiceConfig(root, "authority-gate-init")):
        pass
    token_sizes: list[int] = []

    def sentinel_token(size: int) -> str:
        token_sizes.append(size)
        prefix = TOKEN_SENTINEL if size == 32 else SECRET_SENTINEL
        return f"{prefix}_{len(token_sizes)}"

    monkeypatch.setattr("aizim.security_gate.secrets.token_urlsafe", sentinel_token)

    report = asyncio.run(run_security_gate(root))
    repeated = asyncio.run(run_security_gate(root))
    output = capsys.readouterr()

    assert report.gateway_denials == GATEWAY_DENIALS
    assert report.target_dispatches == 0
    assert tuple(attempt.operation for attempt in report.attempts) == tuple(ProbeOperation)
    allowed = {ProbeOperation.CONNECT_HOST_TCP, ProbeOperation.WRITE_ALLOWED_SCRATCH,
               ProbeOperation.READ_ALLOWED_VIEW}
    assert all(attempt.verdict == ("allowed" if attempt.operation in allowed else "denied")
               for attempt in report.attempts)
    assert report.broker_connections == 0
    assert report.tcp_connections == 1
    assert not report.network_destination_isolation
    assert report.protected_assets_unchanged
    assert report.protected_state_unchanged
    assert report.replay_verified
    assert report.replayed_gateway_denials == GATEWAY_DENIALS
    assert report.replayed_sandbox_denials == tuple(
        operation.value for operation in ProbeOperation if operation not in allowed
    )
    assert len(report.policy_hash) == 64
    assert report.model
    rendered = repr((report, repeated, output))
    assert TOKEN_SENTINEL not in rendered
    assert SECRET_SENTINEL not in rendered
    assert output.out == output.err == ""
    assert repeated.passed
    with StateService(StateServiceConfig(root, "authority-terminal-check")) as state:
        terminals = [
            record.envelope.payload
            for record in state.query_events()
            if record.envelope.event_type == "SandboxProbePassed"
            and record.envelope.payload.get("operation") == GATE_COMPLETION_OPERATION
        ]
    assert len(terminals) == 2
    assert {terminal["policy_hash"] for terminal in terminals} == {
        report.policy_hash,
        repeated.policy_hash,
    }


@pytest.mark.parametrize(
    ("platform", "expected"), (("linux", LINUX_PASS_LINES), ("darwin", PASS_LINES)),
)
def test_security_probe_renders_platform_and_network_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    platform: str, expected: tuple[str, ...],
) -> None:
    async def report(_project: Path) -> _PlatformReport:
        return _PlatformReport(True, platform)

    monkeypatch.setattr(security_probe_command, "run_security_gate", report)
    assert security_probe_command.run_security_probe(tmp_path) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == list(expected)
    assert captured.err == ""


def test_security_probe_does_not_report_pass_after_gate_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    async def fail(_project: Path) -> _PlatformReport:
        raise SecurityGateError("private diagnostic")

    monkeypatch.setattr(security_probe_command, "run_security_gate", fail)
    assert security_probe_command.run_security_probe(tmp_path) == 3
    captured = capsys.readouterr()
    assert captured.out == "SECURITY GATE FAIL\n"
    assert captured.err == ""


