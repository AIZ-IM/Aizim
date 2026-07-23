from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import pytest

from aizim.agents.sandbox import ProbeOperation
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
from aizim.security_gate import run_security_gate
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
    "SECURITY GATE PASS",
    "gateway_matrix=pass",
    "seatbelt_profile=pass",
    "filesystem_denials=6/6",
    "socket_denials=2/2",
    "environment_denials=1/1",
    "allowed_controls=2/2",
    "protected_assets=unchanged",
    "protected_state=unchanged",
)
LINUX_PASS_LINES = (
    *PASS_LINES[:2],
    "linux_profile=pass",
    *PASS_LINES[3:],
)


@dataclass(frozen=True, slots=True)
class _PlatformReport:
    passed: bool
    platform_id: str


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "aizim", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


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
        sequence = len(token_sizes)
        if size == 32:
            return f"{TOKEN_SENTINEL}_{sequence}"
        if size == 24:
            return f"{SECRET_SENTINEL}_{sequence}"
        raise AssertionError(f"unexpected token size: {size}")

    monkeypatch.setattr("aizim.security_gate.secrets.token_urlsafe", sentinel_token)

    report = asyncio.run(run_security_gate(root))
    repeated = asyncio.run(run_security_gate(root))
    output = capsys.readouterr()

    assert report.gateway_denials == GATEWAY_DENIALS
    assert report.target_dispatches == 0
    assert tuple(attempt.operation for attempt in report.attempts) == tuple(ProbeOperation)
    assert all(attempt.verdict == "denied" for attempt in report.attempts[:9])
    assert all(attempt.verdict == "allowed" for attempt in report.attempts[9:])
    assert report.broker_connections == report.tcp_connections == 0
    assert report.protected_assets_unchanged
    assert report.protected_state_unchanged
    assert report.replay_verified
    assert report.replayed_gateway_denials == GATEWAY_DENIALS
    assert report.replayed_sandbox_denials == tuple(
        operation.value for operation in tuple(ProbeOperation)[:9]
    )
    assert len(report.policy_hash) == 64
    assert token_sizes == [32, 32, 24, 32, 32, 24]
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
            and record.envelope.payload.get("operation") == "gate_b_complete"
        ]
    assert len(terminals) == 2
    assert {terminal["policy_hash"] for terminal in terminals} == {
        report.policy_hash,
        repeated.policy_hash,
    }


def test_security_probe_requires_the_fixed_credential_free_shape(tmp_path: Path) -> None:
    missing = run_cli("security-probe")
    wrong_backend = run_cli(
        "security-probe",
        "--project",
        str(tmp_path),
        "--backend",
        "fake",
        "--no-model",
    )
    failed = run_cli(
        "security-probe",
        "--project",
        str(tmp_path),
        "--backend",
        "codex",
        "--no-model",
    )

    assert missing.returncode == wrong_backend.returncode == 2
    assert failed.returncode == 3
    assert failed.stdout == "SECURITY GATE FAIL\n"
    assert failed.stderr == ""


def test_security_probe_renders_the_linux_profile_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def linux_report(_project: Path) -> _PlatformReport:
        return _PlatformReport(True, "linux")

    monkeypatch.setattr(security_probe_command, "run_security_gate", linux_report)

    assert security_probe_command.run_security_probe(tmp_path) == 0
    assert capsys.readouterr().out.splitlines() == list(LINUX_PASS_LINES)


@pytest.mark.macos_sandbox
def test_security_probe_cli_prints_exact_nine_line_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = copy_project(tmp_path, "cli-project")
    layout = ProjectLayout.from_lean_project(root)
    layout.prepare_runtime()
    with StateService(StateServiceConfig(root, "authority-cli-init")):
        pass

    monkeypatch.setenv("PYTHONASYNCIODEBUG", "1")
    result = run_cli(
        "security-probe",
        "--project",
        str(root),
        "--backend",
        "codex",
        "--no-model",
    )

    assert result.returncode == 0
    assert result.stdout.splitlines() == list(PASS_LINES)
    assert result.stderr == ""
