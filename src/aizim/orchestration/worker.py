from __future__ import annotations

import asyncio
import math
import secrets
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from aizim.agents import AgentRequest
from aizim.domain import AgentRole, FileLease, sha256_json
from aizim.domain.serialization import JsonValue
from aizim.lean import DocumentBroker
from aizim.lean.broker_knowledge import current_epoch
from aizim.state import AppendEventCommand, StateService
from aizim.state.event_payload import thaw_payload

from .resources import ResourceGovernor
from .worker_authority import GatewaySession, WorkerAuthority, WorkerBackend
from .worker_cursor import WorkerCursor, cursor_from_payload, last_ack


@dataclass(frozen=True, slots=True)
class WorkerDirective:
    directive_id: str
    worker_id: str
    role: AgentRole
    initial_source: bytes
    budget: int
    timeout_seconds: float

    def __post_init__(self) -> None:
        if (
            not all(type(value) is str and value for value in (self.directive_id, self.worker_id))
            or type(self.role) is not AgentRole
            or type(self.initial_source) is not bytes
            or not self.initial_source
            or type(self.budget) is not int
            or self.budget < 1
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("INVALID_WORKER_DIRECTIVE")


class WorkerRunner:
    def __init__(
        self,
        state: StateService,
        broker: DocumentBroker,
        governor: ResourceGovernor,
        authority: WorkerAuthority,
        project_root: Path,
        run_id: str,
    ) -> None:
        if (
            type(state) is not StateService
            or type(broker) is not DocumentBroker
            or type(governor) is not ResourceGovernor
            or not isinstance(project_root, Path)
            or type(run_id) is not str
            or not run_id
        ):
            raise ValueError("INVALID_WORKER_RUNNER")
        self._state, self._broker, self._governor = state, broker, governor
        self._authority, self._project_root, self._run_id = authority, project_root, run_id

    async def run(self, directive: WorkerDirective, backend: WorkerBackend) -> WorkerCursor:
        execution_id = secrets.token_hex(16)
        self._schedule(directive, execution_id)
        lease = await self._broker.create_document(
            self._run_id,
            directive.worker_id,
            PurePosixPath(f"AizimSmoke/Workers/{self._run_id}/{directive.worker_id}.lean"),
            directive.initial_source,
            current_epoch(self._state),
        )
        self._started(directive)
        self._save(directive, execution_id, lease, directive.budget, False)
        try:
            async with self._governor.proof_slot():
                session = self._authority.issue(self._run_id, directive, lease)
                result = await asyncio.wait_for(
                    backend.run(self._request(directive, lease, session)), directive.timeout_seconds
                )
        except TimeoutError:
            self._state.append_event(
                AppendEventCommand(
                    "WorkerTimedOut",
                    "worker_runner",
                    self._run_id,
                    None,
                    {"worker_id": directive.worker_id, "execution_id": execution_id},
                )
            )
            await self._broker.release_lease(self._run_id, lease.worker_id, lease.lease_id)
            self._stopped(directive, "WORKER_TIMEOUT")
            return self._save(directive, execution_id, lease, 0, True)
        except asyncio.CancelledError:
            self._crashed(directive, "WORKER_CANCELLED")
            self._save(directive, execution_id, lease, 0, True)
            raise
        except Exception:
            self._crashed(directive, "WORKER_CRASHED")
            self._save(directive, execution_id, lease, 0, True)
            raise
        await self._broker.release_lease(self._run_id, lease.worker_id, lease.lease_id)
        self._stopped(directive, "COMPLETED" if result.status == "submitted" else "BACKEND_FAILED")
        return self._save(directive, execution_id, lease, 0, True)

    def cursor(self, worker_id: str) -> WorkerCursor | None:
        if type(worker_id) is not str or not worker_id:
            raise ValueError("INVALID_WORKER_ID")
        event = next(
            (
                record.envelope
                for record in reversed(self._state.query_events(self._run_id))
                if record.envelope.event_type == "WorkerCursorSaved"
                and record.envelope.payload.get("worker_id") == worker_id
            ),
            None,
        )
        return None if event is None else cursor_from_payload(thaw_payload(event.payload))

    def heartbeat(self, worker_id: str) -> bool:
        cursor = self.cursor(worker_id)
        if cursor is None or cursor.terminal:
            return False
        directive = WorkerDirective(
            cursor.directive_id,
            cursor.worker_id,
            AgentRole.PROOF_EXPLORER,
            b"resume",
            cursor.remaining_budget or 1,
            1.0,
        )
        self._save(directive, cursor.execution_id, None, cursor.remaining_budget, False)
        return True

    def _request(
        self, directive: WorkerDirective, lease: FileLease, session: GatewaySession
    ) -> AgentRequest:
        root = self._project_root / ".aizim" / "run" / self._run_id / "workers" / lease.worker_id
        view, scratch = root / "view", root / "scratch"
        view.mkdir(parents=True, exist_ok=True)
        scratch.mkdir(parents=True, exist_ok=True)
        return AgentRequest(
            self._run_id,
            directive.worker_id,
            directive.role,
            _prompt(directive, lease),
            None,
            view,
            scratch,
            session.session_id,
            session.broker_socket,
            directive.timeout_seconds,
            {
                "document_id": lease.document_id,
                "lease_id": lease.lease_id,
                "document_version": lease.file_version,
                "base_epoch": lease.epoch_pair.base_epoch,
                "knowledge_epoch": lease.epoch_pair.knowledge_epoch,
            },
        )

    def _schedule(self, directive: WorkerDirective, execution_id: str) -> None:
        self._state.append_event(
            AppendEventCommand(
                "ScheduleProposed",
                "research_conductor",
                self._run_id,
                None,
                {
                    "directive_id": directive.directive_id,
                    "worker_id": directive.worker_id,
                    "execution_id": execution_id,
                    "role": directive.role.value,
                },
            )
        )
        self._state.append_event(
            AppendEventCommand(
                "WorkerRegistered",
                "research_conductor",
                self._run_id,
                None,
                {"worker_id": directive.worker_id, "role": directive.role.value},
            )
        )

    def _started(self, directive: WorkerDirective) -> None:
        self._state.append_event(
            AppendEventCommand(
                "WorkerStarted",
                "worker_runner",
                self._run_id,
                None,
                {"worker_id": directive.worker_id, "role": directive.role.value},
            )
        )

    def _stopped(self, directive: WorkerDirective, reason: str) -> None:
        self._state.append_event(
            AppendEventCommand(
                "WorkerStopped",
                "worker_runner",
                self._run_id,
                None,
                {"worker_id": directive.worker_id, "reason_code": reason},
            )
        )

    def _crashed(self, directive: WorkerDirective, reason: str) -> None:
        self._state.append_event(
            AppendEventCommand(
                "WorkerCrashed",
                "worker_runner",
                self._run_id,
                None,
                {
                    "worker_id": directive.worker_id,
                    "reason_code": reason,
                    "artifact_hash": sha256_json({"reason": reason}),
                },
            )
        )

    def _save(
        self,
        directive: WorkerDirective,
        execution_id: str,
        lease: FileLease | None,
        remaining_budget: int,
        terminal: bool,
    ) -> WorkerCursor:
        epoch = current_epoch(self._state)
        events = self._state.query_events(self._run_id)
        payload: dict[str, JsonValue] = {
            "directive_id": directive.directive_id,
            "worker_id": directive.worker_id,
            "execution_id": execution_id,
            "last_event_sequence": events[-1].sequence if events else 0,
            "remaining_budget": remaining_budget,
            "terminal": terminal,
            "base_epoch": epoch.base_epoch,
            "knowledge_epoch": epoch.knowledge_epoch,
        }
        if lease is not None:
            payload.update(
                lease_id=lease.lease_id,
                document_id=lease.document_id,
                document_version=lease.file_version,
            )
        acknowledged = last_ack(events, directive.worker_id)
        if acknowledged is not None:
            payload["last_acknowledged_delta"] = acknowledged
        self._state.append_event(
            AppendEventCommand("WorkerCursorSaved", "worker_runner", self._run_id, None, payload)
        )
        return cursor_from_payload(payload)


def _prompt(directive: WorkerDirective, lease: FileLease) -> str:
    return (
        f"directive={directive.directive_id}\nworker={directive.worker_id}\n"
        f"document={lease.document_id}\nepoch={lease.epoch_pair.knowledge_epoch}\n"
        "Use only the granted gateway tools and the durable cursor context."
    )
