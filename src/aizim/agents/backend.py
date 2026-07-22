from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from aizim.domain import AgentRole
from aizim.domain.serialization import JsonValue


@dataclass(frozen=True, slots=True)
class BackendIdentity:
    name: Literal["fake", "codex"]
    version: str
    executable_sha256: str | None


@dataclass(frozen=True, slots=True)
class AgentRequest:
    run_id: str
    worker_id: str
    role: AgentRole
    prompt: str
    model: str | None
    view_root: Path
    scratch_root: Path
    gateway_session_id: str
    gateway_broker_socket: Path
    timeout_seconds: float
    context: dict[str, JsonValue] = field(default_factory=dict)
    result_schema: Literal["default", "alignment"] = "default"


@dataclass(frozen=True, slots=True)
class AgentResult:
    worker_id: str
    status: Literal["submitted", "abstained", "failed"]
    summary: str
    transport_event_hash: str
    final_message_hash: str
    exit_code: int
    policy_hash: str | None = None


class AgentBackend(Protocol):
    @property
    def identity(self) -> BackendIdentity: ...

    async def run(self, request: AgentRequest) -> AgentResult: ...
