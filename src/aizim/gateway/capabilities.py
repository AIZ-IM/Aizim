from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from types import MappingProxyType
from typing import Final, Literal

from aizim.domain import AgentRole
from aizim.domain.serialization import JsonValue
from aizim.state.capabilities import CapabilityRecord
from aizim.state.service import StateService


class GatewayTool(StrEnum):
    STATE_QUERY = "state.query"
    SCHEDULE_PROPOSE = "schedule.propose"
    EVIDENCE_QUERY = "evidence.query"
    SEARCH_BROKERED = "search.brokered"
    EVIDENCE_SUBMIT = "evidence.submit"
    CANDIDATE_SUBMIT = "candidate.submit"
    AUDIT_SUBMIT = "audit.submit"
    PROJECT_READ = "project.read"
    LEAN_GOAL = "lean.goal"
    LEAN_MULTI_ATTEMPT = "lean.multi_attempt"
    LEAN_DIAGNOSTICS = "lean.diagnostics"
    ALIGNMENT_SUBMIT = "alignment.submit"
    FORMAL_GRAPH_READ = "formal_graph.read"
    TASK_EDGE_SUBMIT = "task_edge.submit"
    DOCUMENT_APPLY = "document.apply"
    CONTRIBUTION_SUBMIT = "contribution.submit"
    KNOWLEDGE_READ = "knowledge.read"
    TRACE_READ = "trace.read"
    ANNOTATION_SUBMIT = "annotation.submit"
    STATE_APPEND = "state.append"
    DOCUMENT_RESOLVE_PATH = "document.resolve_path"
    LEAN_BUILD = "lean.build"
    LEAN_VERIFY = "lean.verify"
    PROMOTION_ENQUEUE = "promotion.enqueue"
    PROMOTION_PUBLISH = "promotion.publish"
    ENVIRONMENT_APPROVE = "environment.approve"
    CAPABILITY_MINT = "capability.mint"


ROLE_CAPABILITIES: Final[Mapping[AgentRole, tuple[GatewayTool, ...]]] = MappingProxyType(
    {
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
)


def advertised_tools(role: AgentRole | str | None) -> tuple[GatewayTool, ...]:
    try:
        parsed = role if isinstance(role, AgentRole) else AgentRole(role)
    except (TypeError, ValueError):
        return ()
    return ROLE_CAPABILITIES[parsed]


def _utc(value: datetime, field: str) -> None:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be a timezone-aware UTC datetime")


@dataclass(frozen=True, slots=True)
class CapabilityGrant:
    run_id: str
    worker_id: str
    role: AgentRole
    lease_id: str | None
    operations: tuple[GatewayTool, ...]
    expires_at: datetime

    def __post_init__(self) -> None:
        for field, value in (("run_id", self.run_id), ("worker_id", self.worker_id)):
            if type(value) is not str or not value:
                raise ValueError(f"{field} must be a non-empty string")
        if not isinstance(self.role, AgentRole):
            raise ValueError("role must be an AgentRole")
        if self.lease_id is not None and (type(self.lease_id) is not str or not self.lease_id):
            raise ValueError("lease_id must be a non-empty string when present")
        if type(self.operations) is not tuple or not self.operations:
            raise ValueError("operations must be a non-empty tuple")
        if len(self.operations) != len(set(self.operations)):
            raise ValueError("operations must be unique")
        if any(not isinstance(operation, GatewayTool) for operation in self.operations):
            raise ValueError("operations must contain GatewayTool values")
        if any(operation not in ROLE_CAPABILITIES[self.role] for operation in self.operations):
            raise ValueError("operations exceed the role capability matrix")
        _utc(self.expires_at, "expires_at")


@dataclass(frozen=True, slots=True, repr=False)
class CapabilitySession:
    token: str | None
    run_id: str
    worker_id: str
    role: str
    lease_id: str | None

    def __repr__(self) -> str:
        return "CapabilitySession(token=<redacted>, claims=<redacted>)"


@dataclass(frozen=True, slots=True)
class AuthorizedCall:
    request_id: str
    run_id: str
    worker_id: str
    role: AgentRole
    lease_id: str | None
    operation: GatewayTool
    payload: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class GatewayError:
    code: str
    message: str
    event_id: str | None


@dataclass(frozen=True, slots=True)
class GatewaySuccess:
    result: JsonValue
    exit_code: Literal[0] = 0


@dataclass(frozen=True, slots=True)
class GatewayFailure:
    error: GatewayError
    exit_code: Literal[4] = 4


class CapabilityIssuer:
    def __init__(
        self, state: StateService, token_factory: Callable[[], str] | None = None
    ) -> None:
        self._state = state
        self._token_factory = (
            lambda: secrets.token_urlsafe(32)
        ) if token_factory is None else token_factory

    def mint(self, grant: CapabilityGrant) -> str:
        raw_token = self._token_factory()
        if type(raw_token) is not str or not raw_token:
            raise ValueError("token factory must return a non-empty string")
        record = CapabilityRecord(
            token_hash=sha256(raw_token.encode()).hexdigest(),
            run_id=grant.run_id,
            worker_id=grant.worker_id,
            role=grant.role.value,
            lease_id=grant.lease_id,
            operations=tuple(operation.value for operation in grant.operations),
            expires_at=grant.expires_at,
        )
        self._state.persist_capability(record)
        return raw_token
