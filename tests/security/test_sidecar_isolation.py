from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from aizim.agents import launcher
from aizim.agents.launcher import AgentLaunchError, CodexLaunchSpec, launch_codex
from aizim.domain import AgentRole
from aizim.domain.serialization import JsonValue
from aizim.gateway import (
    BrokerDependencies,
    BrokerRegistration,
    CapabilitySession,
    GatewaySessionBroker,
    GatewaySuccess,
    GatewayTool,
    PeerIdentity,
    SessionDeniedError,
    sidecar,
)
from aizim.gateway.mcp_tools import create_gateway_server
from aizim.gateway.transport import GatewayTransportError, connect_gateway

NOW = datetime(2026, 7, 21, 12, tzinfo=UTC)
IMAGE_HASH = "2" * 64
TOKEN_SHAPED = "sensitive_" + "Z" * 40


class StaticChannel:
    role = AgentRole.FORMALIZER

    def __init__(self) -> None:
        self.calls = 0

    async def call(self, operation: str, payload: dict[str, JsonValue]) -> GatewaySuccess:
        del operation, payload
        self.calls += 1
        return GatewaySuccess({"accepted": True})


class StaticGateway:
    async def call(
        self,
        session: CapabilitySession,
        operation: GatewayTool | str,
        payload: dict[str, JsonValue],
    ) -> GatewaySuccess:
        del session, operation, payload
        return GatewaySuccess({"accepted": True})


def dependencies(*, now: datetime = NOW, image_hash: str = IMAGE_HASH) -> BrokerDependencies:
    return BrokerDependencies(
        clock=lambda: now,
        session_ids=lambda: "session-7",
        peer_identity=lambda _socket: PeerIdentity(os.geteuid(), image_hash),
    )


def registration(*, expires_at: datetime | None = None) -> BrokerRegistration:
    return BrokerRegistration(
        run_id="run-7",
        worker_id="worker-7",
        role=AgentRole.FORMALIZER,
        sidecar_executable_sha256=IMAGE_HASH,
        expires_at=expires_at or NOW + timedelta(minutes=5),
        raw_token=TOKEN_SHAPED,
    )


async def test_invalid_mcp_envelope_is_fixed_and_redacts_token_shapes() -> None:
    channel = StaticChannel()
    server = create_gateway_server(channel)
    unexpected_key = "unexpected_" + "K" * 40

    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool(
            "lean.goal",
            {
                "payload": {},
                unexpected_key: TOKEN_SHAPED,
            },
        )

    rendered = json.dumps(result.structuredContent)
    assert result.isError is True
    assert result.structuredContent == {
        "ok": False,
        "error": {
            "code": "INVALID_REQUEST",
            "message": "tool arguments are invalid",
            "event_id": None,
        },
    }
    assert unexpected_key not in rendered
    assert TOKEN_SHAPED not in rendered
    assert channel.calls == 0


@pytest.mark.parametrize("failure", ("unknown", "second", "wrong_peer", "expired"))
async def test_gateway_channel_redemption_fails_closed(tmp_path: Path, failure: str) -> None:
    socket_path = Path("/tmp") / f"aizim-{os.getpid()}-{failure}.sock"
    broker_dependencies = dependencies(
        image_hash="3" * 64 if failure == "wrong_peer" else IMAGE_HASH
    )
    broker = GatewaySessionBroker(socket_path, broker_dependencies, gateway=StaticGateway())
    session_id = broker.register(registration(expires_at=NOW if failure == "expired" else None))
    await broker.start()
    try:
        selected = "missing-session" if failure == "unknown" else session_id
        if failure == "second":
            redeemed = await connect_gateway(socket_path, selected)
            await redeemed.aclose()
        with pytest.raises(SessionDeniedError):
            await connect_gateway(socket_path, selected)
    finally:
        await broker.aclose()


async def test_transport_zeroes_token_and_disconnects_without_fallback(
    tmp_path: Path,
) -> None:
    socket_path = Path("/tmp") / f"aizim-{os.getpid()}-disconnect.sock"
    broker = GatewaySessionBroker(socket_path, dependencies(), gateway=StaticGateway())
    session_id = broker.register(registration())
    await broker.start()
    transport = await connect_gateway(socket_path, session_id)
    retained = transport._token

    await broker.aclose()
    with pytest.raises(GatewayTransportError):
        await transport.call("lean.goal", {})
    await transport.aclose()

    assert retained
    assert set(retained) == {0}
    assert TOKEN_SHAPED not in repr(transport)


def test_sidecar_session_is_never_an_argument_or_environment_capability(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / ".aizim" / "run" / "gateway.sock"
    argv = (
        "/opt/aizim/bin/aizim-gateway-sidecar",
        "--broker-socket",
        str(socket_path),
        "--session-id",
        "session-7",
    )
    environment = {"PATH": "/usr/bin"}

    assert TOKEN_SHAPED not in repr(argv)
    assert TOKEN_SHAPED not in repr(environment)
    assert set(environment) == {"PATH"}


def test_sidecar_main_scrubs_consumed_session_from_python_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: dict[str, object] = {}

    def fake_run(socket_path: Path, session_id: str) -> int:
        observed.update(socket_path=socket_path, session_id=session_id, argv=tuple(sys.argv))
        return 0

    socket_path = tmp_path / "gateway.sock"
    monkeypatch.setattr(sidecar, "run_gateway_sidecar", fake_run)
    command = ("aizim-gateway-sidecar", "--broker-socket", str(socket_path))
    monkeypatch.setattr(sys, "argv", [*command, "--session-id", "session-7"])

    assert sidecar.main() == 0
    assert (observed["socket_path"], observed["session_id"]) == (socket_path, "session-7")
    assert observed["argv"] == tuple([*sys.argv[:-1], ""])


def executable(path: Path, body: str) -> Path:
    path.write_text(f"#!/usr/bin/python3\n{body}\n")
    path.chmod(0o700)
    return path


def launch_spec(tmp_path: Path, executable_path: Path) -> CodexLaunchSpec:
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    return CodexLaunchSpec(
        argv=(str(executable_path),),
        cwd=tmp_path,
        parent_env={"PATH": "/usr/bin:/bin"},
        shell_env={"PATH": "/usr/bin"},
        stdin=b"fixture",
        timeout_seconds=2.0,
        final_message_path=tmp_path / "final.json",
        output_schema_path=schema,
    )


@pytest.mark.parametrize(
    ("stream", "reason"),
    (("stdout", "CODEX_STDOUT_LIMIT"), ("stderr", "CODEX_STDERR_LIMIT")),
)
async def test_launcher_kills_and_reaps_at_stream_limits(
    tmp_path: Path, stream: str, reason: str
) -> None:
    pid_file = tmp_path / "pid"
    target = "sys.stdout.buffer" if stream == "stdout" else "sys.stderr.buffer"
    script = executable(
        tmp_path / "codex",
        "import os, pathlib, sys, time\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        f"{target}.write(b'x' * ({launcher._JSONL_LINE_LIMIT} + 1))\n"
        f"{target}.flush()\ntime.sleep(30)",
    )

    with pytest.raises(AgentLaunchError, match=reason):
        await launch_codex(launch_spec(tmp_path, script))

    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


async def test_launcher_times_out_with_term_then_kill_and_reaps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pid_file = tmp_path / "pid"
    script = executable(
        tmp_path / "codex",
        "import os, pathlib, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)",
    )
    spec = replace(launch_spec(tmp_path, script), timeout_seconds=2.0)
    monkeypatch.setattr(launcher, "_TERMINATE_GRACE_SECONDS", 0.05)
    run = asyncio.create_task(launch_codex(spec))
    while not pid_file.exists() and not run.done():
        await asyncio.sleep(0.01)
    assert pid_file.exists()
    with pytest.raises(AgentLaunchError, match="CODEX_TIMEOUT"):
        await run

    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)
    assert signal.SIGTERM != signal.SIGKILL


@pytest.mark.parametrize("mode", ("jsonl", "schema", "status"))
async def test_launcher_reports_fixed_parse_failures_without_token_values(
    tmp_path: Path, mode: str
) -> None:
    final = json.dumps(
        {"status": [] if mode == "status" else "submitted", "summary": "ok"}
        | ({"extra": TOKEN_SHAPED} if mode == "schema" else {})
    )
    stdout = "print('{bad json')" if mode == "jsonl" else "print('{}')"
    script = executable(
        tmp_path / "codex",
        "import pathlib\n"
        f"{stdout}\n"
        f"pathlib.Path({str(tmp_path / 'final.json')!r}).write_text({final!r})",
    )

    with pytest.raises(AgentLaunchError) as captured:
        await launch_codex(launch_spec(tmp_path, script))

    assert str(captured.value) in {"CODEX_JSONL_INVALID", "CODEX_RESULT_INVALID"}
    assert TOKEN_SHAPED not in str(captured.value)


async def test_transport_hash_redacts_token_shaped_event_values(tmp_path: Path) -> None:
    hashes: list[str] = []
    for index, token in enumerate(("A" * 40, "B" * 40)):
        root = tmp_path / str(index)
        root.mkdir()
        event = json.dumps({"type": "fixture", "value": token})
        final = json.dumps({"status": "submitted", "summary": "ok"})
        script = executable(
            root / "codex",
            "import pathlib\n"
            f"print({event!r})\n"
            f"pathlib.Path({str(root / 'final.json')!r}).write_text({final!r})",
        )
        outcome = await launch_codex(launch_spec(root, script))
        hashes.append(outcome.transport_event_hash)

    assert hashes[0] == hashes[1]
