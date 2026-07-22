from __future__ import annotations

from mcp.shared.memory import create_connected_server_and_client_session

from aizim.domain import AgentRole
from aizim.gateway import GatewaySuccess, GatewayTool
from aizim.gateway.mcp_tools import create_gateway_server


class ProofChannel:
    role = AgentRole.PROOF_EXPLORER

    def __init__(self) -> None:
        self.calls = []

    async def call(self, operation, payload):
        self.calls.append((operation, payload))
        return GatewaySuccess({"forwarded": True})


async def test_proof_tools_describe_their_gateway_payload_contracts() -> None:
    async with create_connected_server_and_client_session(
        create_gateway_server(ProofChannel())
    ) as client:
        listed = await client.list_tools()

    schemas = {tool.name: tool.inputSchema["properties"]["payload"] for tool in listed.tools}
    expected = {
        GatewayTool.PROJECT_READ.value: ({"path"}, set()),
        GatewayTool.LEAN_GOAL.value: ({"document_id", "line"}, {"column"}),
        GatewayTool.LEAN_MULTI_ATTEMPT.value: (
            {"document_id", "line", "snippets"},
            {"column"},
        ),
        GatewayTool.LEAN_DIAGNOSTICS.value: (
            {"document_id"},
            {"start_line", "end_line"},
        ),
        GatewayTool.DOCUMENT_APPLY.value: (
            {"document_id", "accepted"},
            {"import_module"},
        ),
        GatewayTool.CONTRIBUTION_SUBMIT.value: (
            {
                "document_id",
                "contribution_id",
                "candidate_name",
                "complete_type",
                "imports",
                "dependencies",
                "assumptions",
                "evidence_links",
            },
            set(),
        ),
        GatewayTool.KNOWLEDGE_READ.value: ({"after_knowledge_epoch"}, {"wait"}),
    }
    assert set(schemas) == {
        tool.value
        for tool in GatewayTool
        if tool
        in {
            GatewayTool.PROJECT_READ,
            GatewayTool.LEAN_GOAL,
            GatewayTool.LEAN_MULTI_ATTEMPT,
            GatewayTool.LEAN_DIAGNOSTICS,
            GatewayTool.DOCUMENT_APPLY,
            GatewayTool.CONTRIBUTION_SUBMIT,
            GatewayTool.KNOWLEDGE_READ,
        }
    }
    for tool, (required, optional) in expected.items():
        schema = schemas[tool]
        assert set(schema["required"]) == required
        assert set(schema["properties"]) == required | optional
        assert schema["additionalProperties"] is False


async def test_every_gateway_tool_has_a_closed_payload_schema() -> None:
    schemas = []
    for role in AgentRole:
        channel = ProofChannel()
        channel.role = role
        async with create_connected_server_and_client_session(
            create_gateway_server(channel)
        ) as client:
            listed = await client.list_tools()
        schemas.extend(tool.inputSchema["properties"]["payload"] for tool in listed.tools)

    assert schemas
    assert all(schema["additionalProperties"] is False for schema in schemas)


async def test_sidecar_rejects_invalid_tool_payload_before_forwarding() -> None:
    channel = ProofChannel()
    async with create_connected_server_and_client_session(create_gateway_server(channel)) as client:
        result = await client.call_tool(
            GatewayTool.LEAN_GOAL.value,
            {"payload": {"document": "doc-7", "line": 4}},
        )

    assert result.isError is True
    assert channel.calls == []
