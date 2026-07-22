from __future__ import annotations

import asyncio
import math
import secrets
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from aizim.agents import AgentRequest
from aizim.domain import AgentRole, FileLease
from aizim.domain.serialization import JsonValue
from aizim.gateway import GatewayTool, advertised_tools
from aizim.lean import DocumentBroker
from aizim.lean.broker_knowledge import current_epoch
from aizim.state import AppendEventCommand, StateService
from aizim.state.event_payload import thaw_payload

from .resources import ResourceGovernor
from .worker_authority import GatewaySession, WorkerAuthority, WorkerBackend
from .worker_cursor import WorkerCursor, cursor_from_payload, last_ack
from .worker_events import record_crashed, record_schedule, record_started, record_stopped
from .worker_lifecycle import record_agent_result, release_worker_lease

_PROOF_WORKER_PROMPT = Path(__file__).parent.parent / "agents/prompts/proof_worker.md"


class WorkerExecutionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WorkerDirective:
    directive_id: str
    worker_id: str
    role: AgentRole
    initial_source: bytes
    budget: int
    timeout_seconds: float
    operations: tuple[GatewayTool, ...] | None = None

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
        operations = advertised_tools(self.role) if self.operations is None else self.operations
        if (
            not operations
            or len(operations) != len(set(operations))
            or any(operation not in advertised_tools(self.role) for operation in operations)
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
        record_schedule(
            self._state,
            self._run_id,
            directive.directive_id,
            directive.worker_id,
            execution_id,
            directive.role,
        )
        lease = await self._broker.create_document(
            self._run_id,
            directive.worker_id,
            PurePosixPath(f"AizimSmoke/Workers/{self._run_id}/{directive.worker_id}.lean"),
            directive.initial_source,
            current_epoch(self._state),
        )
        record_started(self._state, self._run_id, directive.worker_id, directive.role)
        self._save(directive, execution_id, lease, directive.budget, False)
        primary_failure: BaseException | None = None
        try:
            async with self._governor.proof_slot():
                session = self._authority.issue(self._run_id, directive, lease)
                result = await asyncio.wait_for(
                    backend.run(self._request(directive, lease, session)), directive.timeout_seconds
                )
        except TimeoutError as error:
            self._state.append_event(
                AppendEventCommand(
                    "WorkerTimedOut",
                    "worker_runner",
                    self._run_id,
                    None,
                    {"worker_id": directive.worker_id, "execution_id": execution_id},
                )
            )
            record_stopped(self._state, self._run_id, directive.worker_id, "WORKER_TIMEOUT")
            self._save(directive, execution_id, lease, 0, True)
            primary_failure = WorkerExecutionError("WORKER_TIMEOUT")
            raise primary_failure from error
        except asyncio.CancelledError as error:
            primary_failure = error
            record_crashed(self._state, self._run_id, directive.worker_id, "WORKER_CANCELLED")
            self._save(directive, execution_id, lease, 0, True)
            raise
        except Exception as error:
            primary_failure = error
            record_crashed(self._state, self._run_id, directive.worker_id, "WORKER_CRASHED")
            self._save(directive, execution_id, lease, 0, True)
            raise
        else:
            record_agent_result(self._state, self._run_id, execution_id, result)
            reason = "COMPLETED" if result.status == "submitted" else "BACKEND_FAILED"
            record_stopped(self._state, self._run_id, directive.worker_id, reason)
            cursor = self._save(directive, execution_id, lease, 0, True)
            if result.status != "submitted":
                primary_failure = WorkerExecutionError(reason)
                raise primary_failure
            return cursor
        finally:
            await release_worker_lease(self._broker, self._run_id, lease, primary_failure)

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
        f"{_PROOF_WORKER_PROMPT.read_text()}\n\ndirective={directive.directive_id} "
        f"worker={directive.worker_id} document={lease.document_id} "
        f"epoch={lease.epoch_pair.knowledge_epoch}\n"
    )
