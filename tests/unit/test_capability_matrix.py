from __future__ import annotations

from types import MappingProxyType

import pytest

from aizim.domain import AgentRole
from aizim.gateway import ROLE_CAPABILITIES, GatewayTool, advertised_tools

EXPECTED = {
    AgentRole.RESEARCH_CONDUCTOR: (GatewayTool.STATE_QUERY, GatewayTool.SCHEDULE_PROPOSE),
    AgentRole.LITERATURE_SCOUT: (
        GatewayTool.EVIDENCE_QUERY,
        GatewayTool.SEARCH_BROKERED,
        GatewayTool.EVIDENCE_SUBMIT,
    ),
    AgentRole.BOUNDARY_MAPPER: (GatewayTool.EVIDENCE_QUERY, GatewayTool.EVIDENCE_SUBMIT),
    AgentRole.CONJECTURE_GENERATOR: (GatewayTool.CANDIDATE_SUBMIT,),
    AgentRole.NOVELTY_AUDITOR: (
        GatewayTool.EVIDENCE_QUERY,
        GatewayTool.SEARCH_BROKERED,
        GatewayTool.AUDIT_SUBMIT,
    ),
    AgentRole.FORMALIZER: (
        GatewayTool.PROJECT_READ,
        GatewayTool.LEAN_GOAL,
        GatewayTool.LEAN_MULTI_ATTEMPT,
        GatewayTool.LEAN_DIAGNOSTICS,
        GatewayTool.CANDIDATE_SUBMIT,
        GatewayTool.ALIGNMENT_SUBMIT,
    ),
    AgentRole.FORMAL_PLANNER: (
        GatewayTool.FORMAL_GRAPH_READ,
        GatewayTool.TASK_EDGE_SUBMIT,
    ),
    AgentRole.PROOF_EXPLORER: (
        GatewayTool.PROJECT_READ,
        GatewayTool.LEAN_GOAL,
        GatewayTool.LEAN_MULTI_ATTEMPT,
        GatewayTool.LEAN_DIAGNOSTICS,
        GatewayTool.DOCUMENT_APPLY,
        GatewayTool.CONTRIBUTION_SUBMIT,
        GatewayTool.KNOWLEDGE_READ,
    ),
    AgentRole.LEMMA_INVENTOR: (
        GatewayTool.PROJECT_READ,
        GatewayTool.LEAN_GOAL,
        GatewayTool.LEAN_MULTI_ATTEMPT,
        GatewayTool.LEAN_DIAGNOSTICS,
        GatewayTool.DOCUMENT_APPLY,
        GatewayTool.CONTRIBUTION_SUBMIT,
        GatewayTool.KNOWLEDGE_READ,
    ),
    AgentRole.COUNTEREXAMPLE_AGENT: (
        GatewayTool.PROJECT_READ,
        GatewayTool.LEAN_GOAL,
        GatewayTool.LEAN_MULTI_ATTEMPT,
        GatewayTool.LEAN_DIAGNOSTICS,
        GatewayTool.DOCUMENT_APPLY,
        GatewayTool.EVIDENCE_SUBMIT,
        GatewayTool.CONTRIBUTION_SUBMIT,
        GatewayTool.KNOWLEDGE_READ,
    ),
    AgentRole.LEARNING_AGENT: (
        GatewayTool.FORMAL_GRAPH_READ,
        GatewayTool.KNOWLEDGE_READ,
        GatewayTool.TRACE_READ,
        GatewayTool.ANNOTATION_SUBMIT,
    ),
}
TRUSTED_ONLY = {
    GatewayTool.STATE_APPEND,
    GatewayTool.DOCUMENT_RESOLVE_PATH,
    GatewayTool.LEAN_BUILD,
    GatewayTool.LEAN_VERIFY,
    GatewayTool.PROMOTION_ENQUEUE,
    GatewayTool.PROMOTION_PUBLISH,
    GatewayTool.ENVIRONMENT_APPROVE,
    GatewayTool.CAPABILITY_MINT,
}


def test_agent_roles_are_the_exact_untrusted_design_roles() -> None:
    assert tuple(role.value for role in AgentRole) == (
        "research_conductor",
        "literature_scout",
        "boundary_mapper",
        "conjecture_generator",
        "novelty_auditor",
        "formalizer",
        "formal_planner",
        "proof_explorer",
        "lemma_inventor",
        "counterexample_agent",
        "learning_agent",
    )


def test_gateway_tool_enum_contains_all_advertised_and_trusted_only_operations() -> None:
    assert len(GatewayTool) == 27
    assert set(GatewayTool) == set(TRUSTED_ONLY).union(*map(set, EXPECTED.values()))


def test_role_matrix_exhaustively_matches_every_role_tool_pair() -> None:
    assert isinstance(ROLE_CAPABILITIES, MappingProxyType)
    assert set(ROLE_CAPABILITIES) == set(AgentRole)
    for role in AgentRole:
        assert ROLE_CAPABILITIES[role] == EXPECTED[role]
        assert len(ROLE_CAPABILITIES[role]) == len(set(ROLE_CAPABILITIES[role]))
        for tool in GatewayTool:
            assert (tool in ROLE_CAPABILITIES[role]) is (tool in EXPECTED[role])


def test_trusted_operations_are_never_advertised_to_an_agent_role() -> None:
    assert all(tool not in tools for tool in TRUSTED_ONLY for tools in ROLE_CAPABILITIES.values())


@pytest.mark.parametrize("role", [None, "", "proof_worker", "promotion_service", "unknown"])
def test_unknown_missing_and_malformed_roles_discover_nothing(role: str | None) -> None:
    assert advertised_tools(role) == ()


def test_role_matrix_cannot_be_mutated() -> None:
    assert not hasattr(ROLE_CAPABILITIES, "__setitem__")
