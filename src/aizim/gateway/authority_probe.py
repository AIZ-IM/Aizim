from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from aizim.domain import AgentRole
from aizim.state.service import StateService

from .capabilities import (
    AuthorizedCall,
    CapabilityGrant,
    CapabilityIssuer,
    CapabilitySession,
    GatewayFailure,
    GatewayTool,
)
from .service import CapabilityDependencies, CapabilityGateway, GatewayLimits

AUTHORITY_DENIAL_REASONS = (
    "MISSING_TOKEN",
    "UNKNOWN_TOKEN",
    "ROLE_MISMATCH",
    "UNADVERTISED_OPERATION",
)
WORKER_ID = "security-probe"


class AuthorityProbeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AuthorityProbeEvidence:
    gateway: CapabilityGateway = field(repr=False)
    raw_token: str = field(repr=False)
    token_hash: str
    run_id: str
    expires_at: datetime
    denial_reasons: tuple[str, ...]
    target_dispatches: int


async def run_authority_probe(state: StateService, run_id: str) -> AuthorityProbeEvidence:
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=5)
    raw_token = CapabilityIssuer(state).mint(
        CapabilityGrant(
            run_id,
            WORKER_ID,
            AgentRole.RESEARCH_CONDUCTOR,
            None,
            (GatewayTool.STATE_QUERY,),
            expires_at,
        )
    )
    token_hash = sha256(raw_token.encode()).hexdigest()
    try:
        dispatched: list[AuthorizedCall] = []
        request_numbers = itertools.count(1)
        gateway = CapabilityGateway(
            state,
            {GatewayTool.STATE_QUERY: dispatched.append},
            GatewayLimits(16, 60.0, 16),
            CapabilityDependencies(
                lambda: now,
                time.monotonic,
                lambda: f"gate-{run_id[-8:]}-{next(request_numbers)}",
            ),
        )
        session = CapabilitySession(
            raw_token, run_id, WORKER_ID, AgentRole.RESEARCH_CONDUCTOR.value, None
        )
        forged = ("A" if raw_token[0] != "A" else "B") + raw_token[1:]
        attacks = (
            (replace(session, token=None), GatewayTool.STATE_QUERY),
            (replace(session, token=forged), GatewayTool.STATE_QUERY),
            (replace(session, role=AgentRole.LITERATURE_SCOUT.value), GatewayTool.STATE_QUERY),
            (session, GatewayTool.STATE_APPEND),
        )
        results = tuple(
            [await gateway.call(presented, operation, {}) for presented, operation in attacks]
        )
        if not all(isinstance(result, GatewayFailure) for result in results):
            raise AuthorityProbeError("authorization attack reached a target")
        denials = authority_denial_reasons(state, run_id)
        if denials != AUTHORITY_DENIAL_REASONS or dispatched:
            raise AuthorityProbeError("authorization denials were incomplete")
        return AuthorityProbeEvidence(
            gateway, raw_token, token_hash, run_id, expires_at, denials, len(dispatched)
        )
    except BaseException:
        state.revoke_capability(token_hash)
        raise


def authority_denial_reasons(state: StateService, run_id: str) -> tuple[str, ...]:
    values: list[str] = []
    for record in state.query_events(run_id):
        event = record.envelope
        if event.event_type != "CapabilityDenied":
            continue
        value = event.payload.get("reason_code")
        if type(value) is not str:
            raise AuthorityProbeError("capability denial event is incomplete")
        values.append(value)
    return tuple(values)
