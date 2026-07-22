from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from aizim.agents.backend import AgentRequest, BackendIdentity
from aizim.agents.fake_backend import FakeAgentBackend, FakeToolAction
from aizim.domain import AgentRole
from aizim.domain.serialization import JsonValue
from aizim.gateway import GatewayError, GatewayFailure, GatewaySuccess, GatewayTool


class MemoryTransport:
    def __init__(self, results: tuple[GatewaySuccess | GatewayFailure, ...]) -> None:
        self.role = AgentRole.PROOF_EXPLORER
        self.results = iter(results)
        self.calls: list[tuple[GatewayTool | str, dict[str, JsonValue]]] = []
        self.closed = False

    async def call(
        self, operation: GatewayTool | str, payload: dict[str, JsonValue]
    ) -> GatewaySuccess | GatewayFailure:
        self.calls.append((operation, payload))
        return next(self.results)

    async def aclose(self) -> None:
        self.closed = True


class TransportFactory:
    def __init__(self, build: Callable[[], MemoryTransport]) -> None:
        self._build = build
        self.connections: list[tuple[Path, str, MemoryTransport]] = []

    async def __call__(self, socket_path: Path, session_id: str) -> MemoryTransport:
        transport = self._build()
        self.connections.append((socket_path, session_id, transport))
        return transport


def request(tmp_path: Path) -> AgentRequest:
    view = tmp_path / "view"
    scratch = tmp_path / "scratch"
    view.mkdir(exist_ok=True)
    scratch.mkdir(exist_ok=True)
    return AgentRequest(
        run_id="run-7",
        worker_id="worker-7",
        role=AgentRole.PROOF_EXPLORER,
        prompt="prove the fixture",
        model=None,
        view_root=view,
        scratch_root=scratch,
        gateway_session_id="session-7",
        gateway_broker_socket=tmp_path / "gateway.sock",
        timeout_seconds=20.0,
    )


async def test_fake_backend_uses_gateway_transport_and_is_deterministic(
    tmp_path: Path,
) -> None:
    actions = (
        FakeToolAction(GatewayTool.PROJECT_READ, {"path": "Main.lean"}),
        FakeToolAction(GatewayTool.LEAN_GOAL, {"document_id": "doc-7"}),
    )
    results = (
        GatewaySuccess({"source": "theorem demo : True := by trivial"}),
        GatewaySuccess({"goal": "True"}),
    )
    factory = TransportFactory(lambda: MemoryTransport(results))
    backend = FakeAgentBackend(actions, connect=factory)
    agent_request = request(tmp_path)

    first = await backend.run(agent_request)
    second = await backend.run(agent_request)

    assert backend.identity == BackendIdentity("fake", "deterministic-v1", None)
    assert first == second
    assert first.worker_id == "worker-7"
    assert first.status == "submitted"
    assert len(first.transport_event_hash) == 64
    assert len(first.final_message_hash) == 64
    assert [connection[:2] for connection in factory.connections] == [
        (agent_request.gateway_broker_socket, "session-7"),
        (agent_request.gateway_broker_socket, "session-7"),
    ]
    for _socket, _session, transport in factory.connections:
        assert transport.calls == [
            (GatewayTool.PROJECT_READ, {"path": "Main.lean"}),
            (GatewayTool.LEAN_GOAL, {"document_id": "doc-7"}),
        ]
        assert transport.closed


async def test_fake_backend_fails_closed_on_gateway_denial(tmp_path: Path) -> None:
    denial = GatewayFailure(
        GatewayError("CAPABILITY_DENIED", "operation is not allowed for this worker", "event-7")
    )
    factory = TransportFactory(lambda: MemoryTransport((denial,)))
    backend = FakeAgentBackend(
        (FakeToolAction(GatewayTool.DOCUMENT_APPLY, {"document_id": "doc-7"}),),
        connect=factory,
    )

    result = await backend.run(request(tmp_path))

    assert result.status == "failed"
    assert "event-7" not in result.summary
    assert factory.connections[0][2].closed


async def test_fake_fixture_resolves_typed_delta_reference(tmp_path: Path) -> None:
    fixture = tmp_path / "worker.json"
    fixture.write_text(
        json.dumps(
            {
                "actions": [
                    {"operation": "knowledge.read", "payload": {"after_knowledge_epoch": 0}},
                    {
                        "operation": "document.apply",
                        "payload": {
                            "document_id": {"$cursor": "document_id"},
                            "delta_reference": {"template": "exact {fully_qualified_name} n"},
                        },
                    },
                ]
            }
        )
    )
    delta: dict[str, JsonValue] = {
        "fully_qualified_name": "AizimSmoke.Research.example",
        "module": "AizimSmoke.Research.K00000001_S00000001_example",
    }
    factory = TransportFactory(
        lambda: MemoryTransport((GatewaySuccess({"deltas": [delta]}), GatewaySuccess({})))
    )
    backend = FakeAgentBackend.from_fixture(
        fixture, connect=factory, cursor={"document_id": "document-7"}
    )

    result = await backend.run(request(tmp_path))

    assert result.status == "submitted"
    calls = factory.connections[0][2].calls
    operation, payload = calls[1]
    assert operation is GatewayTool.DOCUMENT_APPLY
    assert payload == {
        "document_id": "document-7",
        "accepted": "exact AizimSmoke.Research.example n",
        "import_module": delta["module"],
    }
    assert backend.resolved_actions[-1] == FakeToolAction(GatewayTool.DOCUMENT_APPLY, payload)
