from __future__ import annotations

import asyncio
import secrets
import socket
import stat
import sys
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile, mkdtemp

import aizim.gateway as gateway_api
from aizim.agents.macos_sandbox import MacOSSandboxAdapter
from aizim.agents.sandbox import ProbeAttempt, ProbeOperation, ProbeRequest
from aizim.agents.workspace_view import ViewSource, WorkspaceView, WorkspaceViewBuilder
from aizim.cli.doctor_command import scrubbed_command_environment
from aizim.domain import AgentRole, sha256_file
from aizim.gateway.authority_probe import (
    AUTHORITY_DENIAL_REASONS,
    WORKER_ID,
    authority_denial_reasons,
    run_authority_probe,
)
from aizim.runtime.layout import ProjectLayout
from aizim.state import StateService, StateServiceConfig


class SecurityGateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SecurityGateReport:
    passed: bool
    gateway_denials: tuple[str, ...]
    target_dispatches: int
    attempts: tuple[ProbeAttempt, ...]
    broker_connections: int
    tcp_connections: int
    protected_assets_unchanged: bool
    protected_state_unchanged: bool
    replay_verified: bool
    replayed_gateway_denials: tuple[str, ...]
    replayed_sandbox_denials: tuple[str, ...]
    policy_hash: str


@dataclass(frozen=True, slots=True)
class _GateResources:
    run_id: str
    layout: ProjectLayout
    view: WorkspaceView
    unleased_file: Path
    shared_artifact: Path
    tcp_listener: socket.socket
    broker_socket: Path


class _ObservedBroker(gateway_api.GatewaySessionBroker):
    def __init__(self, socket_path: Path, gateway: gateway_api.CapabilityGateway) -> None:
        super().__init__(socket_path, gateway=gateway)
        self.connection_count = 0

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connection_count += 1
        super()._accept(reader, writer)


async def run_security_gate(project_root: Path) -> SecurityGateReport:
    layout = ProjectLayout.from_lean_project(project_root)
    layout.validate_runtime()
    leased, unleased = _regular_lean_sources(layout.root)
    source = ViewSource(
        PurePosixPath(leased.relative_to(layout.root).as_posix()), sha256_file(leased)
    )
    run_id = f"security-gate-{secrets.token_hex(8)}"
    with ExitStack() as cleanup:
        view = WorkspaceViewBuilder().materialize(layout.root, (source,))
        cleanup.callback(view.close)
        alias_root, broker_socket = _broker_alias(layout.root)
        cleanup.callback(_remove_alias, alias_root)
        shared = _create_shared_artifact(layout.artifact_root)
        cleanup.callback(shared.unlink, missing_ok=True)
        tcp = _tcp_listener()
        cleanup.callback(tcp.close)
        resources = _GateResources(run_id, layout, view, unleased, shared, tcp, broker_socket)
        with StateService(StateServiceConfig(layout.root, "security-gate-live")) as state:
            live_report, protected_digest = await _run_live_gate(state, resources)
    with StateService(StateServiceConfig(layout.root, "security-gate-replay")) as restarted:
        replay = restarted.replay_verify()
        replayed_gateway = authority_denial_reasons(restarted, run_id)
        replayed_sandbox = _sandbox_denials(restarted, run_id)
        replay_verified = (
            replay.matched
            and replay.logical_digest == protected_digest
            and restarted.logical_digest() == protected_digest
        )
    expected_sandbox = tuple(operation.value for operation in tuple(ProbeOperation)[:9])
    if (
        not replay_verified
        or replayed_gateway != AUTHORITY_DENIAL_REASONS
        or replayed_sandbox != expected_sandbox
    ):
        raise SecurityGateError("security gate replay evidence did not close")
    return replace(
        live_report,
        passed=True,
        replay_verified=True,
        replayed_gateway_denials=replayed_gateway,
        replayed_sandbox_denials=replayed_sandbox,
    )


async def _run_live_gate(
    state: StateService, resources: _GateResources
) -> tuple[SecurityGateReport, str]:
    setup = await run_authority_probe(state, resources.run_id)
    broker: _ObservedBroker | None = None
    revoked = False
    try:
        before_digest = state.logical_digest()
        before_assets = (
            sha256_file(resources.unleased_file),
            sha256_file(resources.shared_artifact),
        )
        broker = _ObservedBroker(resources.broker_socket, setup.gateway)
        broker.register(
            gateway_api.BrokerRegistration(
                setup.run_id,
                WORKER_ID,
                AgentRole.RESEARCH_CONDUCTOR,
                sha256_file(Path(sys.executable).resolve(strict=True)),
                setup.expires_at,
                setup.raw_token,
            )
        )
        await broker.start()
        canonical_socket = resources.layout.run_root / "gateway.sock"
        if not stat.S_ISSOCK(canonical_socket.lstat().st_mode):
            raise SecurityGateError("broker did not bind the canonical socket entry")
        probe = await MacOSSandboxAdapter().launch_probe(
            _probe_request(state, resources, broker.socket_path)
        )
    finally:
        try:
            if broker is not None:
                await broker.aclose()
        finally:
            revoked = state.revoke_capability(setup.token_hash)
    if broker is None:
        raise SecurityGateError("security probe broker was not created")
    capability = state.capability_record(setup.token_hash)
    if not revoked or capability is None or capability.revoked_at is None:
        raise SecurityGateError("security probe capability was not revoked")
    after_assets = (
        sha256_file(resources.unleased_file),
        sha256_file(resources.shared_artifact),
    )
    tcp_connections = _accepted_connections(resources.tcp_listener)
    assets_unchanged = before_assets == after_assets
    state_unchanged = before_digest == state.logical_digest() == probe.logical_digest_after
    if not (
        probe.passed
        and broker.connection_count == tcp_connections == 0
        and assets_unchanged
        and state_unchanged
    ):
        raise SecurityGateError("security gate live evidence did not close")
    return (
        SecurityGateReport(
            False,
            setup.denial_reasons,
            setup.target_dispatches,
            probe.attempts,
            broker.connection_count,
            tcp_connections,
            assets_unchanged,
            state_unchanged,
            False,
            (),
            (),
            probe.policy_hash,
        ),
        before_digest,
    )


def _probe_request(
    state: StateService, resources: _GateResources, gateway_socket: Path
) -> ProbeRequest:
    allowed = resources.view.view_root / resources.view.entries[0].relative_path
    host, port = resources.tcp_listener.getsockname()
    environment = scrubbed_command_environment()
    environment["AIZIM_SECURITY_PROBE_SENTINEL"] = secrets.token_urlsafe(24)
    return ProbeRequest(
        "authority-probe",
        resources.run_id,
        resources.layout.root,
        resources.view.view_root,
        resources.view.scratch_root,
        resources.layout.database_path,
        resources.unleased_file,
        resources.shared_artifact,
        gateway_socket,
        str(host),
        int(port),
        allowed,
        sha256_file(allowed),
        "AIZIM_SECURITY_PROBE_SENTINEL",
        environment,
        state,
    )


def _regular_lean_sources(root: Path) -> tuple[Path, Path]:
    sources: list[Path] = []
    for source in sorted(root.rglob("*.lean")):
        relative = source.relative_to(root)
        if any(part in {".aizim", ".git"} for part in relative.parts):
            continue
        metadata = source.lstat()
        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
            sources.append(source)
    if len(sources) < 2:
        raise SecurityGateError("security probe requires two regular Lean sources")
    return sources[0], sources[1]


def _create_shared_artifact(root: Path) -> Path:
    with NamedTemporaryFile(prefix="security-probe-", dir=root, delete=False) as artifact:
        artifact.write(b"protected shared artifact\n")
        return Path(artifact.name)


def _broker_alias(project_root: Path) -> tuple[Path, Path]:
    root = Path(mkdtemp(prefix="aizim-gate-", dir="/private/tmp"))
    link = root / "project"
    try:
        link.symlink_to(project_root, target_is_directory=True)
    except BaseException:
        root.rmdir()
        raise
    return root, link / ".aizim" / "run" / "gateway.sock"


def _remove_alias(root: Path) -> None:
    (root / "project").unlink(missing_ok=True)
    root.rmdir()


def _tcp_listener() -> socket.socket:
    listener = socket.socket(socket.AF_INET)
    listener.setblocking(False)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    return listener


def _accepted_connections(listener: socket.socket) -> int:
    accepted = 0
    while True:
        try:
            connection, _address = listener.accept()
        except BlockingIOError:
            return accepted
        connection.close()
        accepted += 1


def _sandbox_denials(state: StateService, run_id: str) -> tuple[str, ...]:
    values: list[str] = []
    for record in state.query_events(run_id):
        event = record.envelope
        if event.event_type != "SandboxProbeDenied":
            continue
        value = event.payload.get("operation")
        if type(value) is not str:
            raise SecurityGateError("security event is incomplete")
        values.append(value)
    return tuple(values)
