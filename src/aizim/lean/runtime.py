from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from aizim.domain import sha256_json
from aizim.domain.serialization import JsonValue
from aizim.gateway.capabilities import AuthorizedCall, GatewayTool
from aizim.state import AppendEventCommand, StateService
from aizim.state.documents import DocumentState
from aizim.state.events import utc_now

from .documents import DocumentBroker
from .mcp_client import LeanMcpClient
from .models import (
    DiagnosticsResult,
    GoalResult,
    LeanProcess,
    LeanRuntimeError,
    MultiAttemptResult,
    WorkerSession,
)
from .promotion_runtime import PromotionRuntimeMethods
from .runtime_gateway import targets as gateway_targets
from .verification import BuildResult, VerificationResult

type LeanResult = GoalResult | MultiAttemptResult | DiagnosticsResult
type ResultCall[T: LeanResult] = Callable[[LeanMcpClient, Path], Awaitable[T]]


class SharedLeanRuntime(PromotionRuntimeMethods):
    def __init__(self, state: StateService, broker: DocumentBroker, run_id: str) -> None:
        if type(run_id) is not str or not run_id:
            raise LeanRuntimeError("INVALID_RUNTIME_REQUEST")
        self._state = state
        self._broker = broker
        self._run_id = run_id
        self._client: LeanMcpClient | None = None
        self._project_root: Path | None = None
        self._runtime_id: str | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._opened: dict[tuple[str, str, str, str], WorkerSession] = {}

    @property
    def mcp_process_id(self) -> int:
        return self._client_or_raise().mcp_process_id

    async def goal(
        self, session: WorkerSession, document_id: str, line: int, column: int | None = None
    ) -> GoalResult:
        return await self._execute(
            session,
            document_id,
            {"operation": "lean_goal", "document_id": document_id, "line": line, "column": column},
            lambda client, path: client.goal(path, line, column),
        )

    async def multi_attempt(
        self,
        session: WorkerSession,
        document_id: str,
        line: int,
        snippets: tuple[str, ...],
        column: int | None = None,
    ) -> MultiAttemptResult:
        return await self._execute(
            session,
            document_id,
            {
                "operation": "lean_multi_attempt",
                "document_id": document_id,
                "line": line,
                "column": column,
                "snippets": list(snippets),
            },
            lambda client, path: client.multi_attempt(path, line, snippets, column),
        )

    async def diagnostics(
        self,
        session: WorkerSession,
        document_id: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> DiagnosticsResult:
        return await self._execute(
            session,
            document_id,
            {
                "operation": "lean_diagnostic_messages",
                "document_id": document_id,
                "start_line": start_line,
                "end_line": end_line,
            },
            lambda client, path: client.diagnostics(path, start_line, end_line),
        )

    def processes(self) -> tuple[LeanProcess, ...]:
        return self._client_or_raise().processes()

    async def _build(self) -> BuildResult:
        async with self._lifecycle_lock:
            return await self._client_or_raise().build(clean=False, fetch_cache=False)

    async def _verify(self, document_id: str, theorem_name: str) -> VerificationResult:
        session = next((item for key, item in self._opened.items() if key[-1] == document_id), None)
        if session is None:
            raise LeanRuntimeError("DOCUMENT_NOT_OPEN")
        _, _, path = await self._resolve(session, document_id)
        async with self._lifecycle_lock:
            return await self._client_or_raise().verify(path, theorem_name, scan_source=True)

    async def _terminate_for_test(self) -> None:
        await self._client_or_raise()._terminate_for_test()

    async def aclose(self) -> None:
        client, self._client = self._client, None
        self._project_root = None
        self._runtime_id = None
        if client is not None:
            await client.aclose()

    def gateway_targets(
        self,
    ) -> Mapping[GatewayTool, Callable[[AuthorizedCall], Awaitable[JsonValue]]]:
        return gateway_targets(self)

    async def _execute[T: LeanResult](
        self,
        session: WorkerSession,
        document_id: str,
        input_payload: dict[str, JsonValue],
        call: ResultCall[T],
    ) -> T:
        document, project_root, path = await self._resolve(session, document_id)
        self._opened[(session.run_id, session.worker_id, session.lease_id, document_id)] = session
        started_at = utc_now()
        try:
            client = await self._ensure_started(project_root)
            try:
                result = await call(client, path)
            except LeanRuntimeError as error:
                if error.code != "MCP_TRANSPORT_FAILED":
                    raise
                result = await call(await self._restart(client, project_root), path)
        except LeanRuntimeError as error:
            self._record_action(
                document, session, input_payload, _failure_hash(error), "failure", started_at
            )
            raise
        self._record_action(
            document, session, input_payload, result.response_hash, "success", started_at
        )
        return result

    async def _resolve(
        self, session: WorkerSession, document_id: str
    ) -> tuple[DocumentState, Path, Path]:
        if session.run_id != self._run_id:
            raise LeanRuntimeError("RUN_MISMATCH")
        try:
            return await self._broker._trusted_runtime_document(
                session.run_id, session.worker_id, session.lease_id, document_id
            )
        except Exception:
            raise LeanRuntimeError("DOCUMENT_ACCESS_DENIED") from None

    async def _ensure_started(self, project_root: Path) -> LeanMcpClient:
        async with self._lifecycle_lock:
            if self._client is not None:
                if self._project_root != project_root:
                    raise LeanRuntimeError("RUNTIME_PROJECT_MISMATCH")
                return self._client
            client = LeanMcpClient(project_root)
            await client.start()
            self._client, self._project_root, self._runtime_id = client, project_root, _opaque_id()
            _ = self._state.append_event(
                AppendEventCommand(
                    "LeanRuntimeStarted",
                    "lean_runtime",
                    self._run_id,
                    None,
                    {"runtime_id": self._runtime_id, "mode": "shared", "started_at": _timestamp()},
                )
            )
            return client

    async def _restart(self, failing: LeanMcpClient, project_root: Path) -> LeanMcpClient:
        async with self._lifecycle_lock:
            if self._client is not failing:
                return self._client_or_raise()
            previous = self._runtime_id
            await failing.aclose()
            self._client, self._project_root = None, None
            _ = self._state.append_event(
                AppendEventCommand(
                    "LeanRuntimeCrashed",
                    "lean_runtime",
                    self._run_id,
                    None,
                    {"runtime_id": previous or _opaque_id(), "reason_code": "MCP_TRANSPORT_FAILED"},
                )
            )
            replacement = LeanMcpClient(project_root)
            await replacement.start()
            self._client, self._runtime_id = replacement, _opaque_id()
            await self._reopen_documents(replacement)
            _ = self._state.append_event(
                AppendEventCommand(
                    "LeanRuntimeRestarted",
                    "lean_runtime",
                    self._run_id,
                    None,
                    {"runtime_id": self._runtime_id, "previous_runtime_id": previous or "unknown"},
                )
            )
            return replacement

    async def _reopen_documents(self, client: LeanMcpClient) -> None:
        for (*_, document_id), session in tuple(self._opened.items()):
            _, _, path = await self._resolve(session, document_id)
            _ = await client.diagnostics(path, None, None)

    def _record_action(
        self,
        document: DocumentState,
        session: WorkerSession,
        input_payload: dict[str, JsonValue],
        output_hash: str,
        verdict: str,
        started_at: datetime,
    ) -> None:
        _ = self._state.append_event(
            AppendEventCommand(
                "FormalActionRecorded",
                "lean_runtime",
                session.run_id,
                None,
                {
                    "action_id": _opaque_id(),
                    "worker_id": session.worker_id,
                    "document_id": document.document_id,
                    "input_hash": sha256_json(input_payload),
                    "output_hash": output_hash,
                    "verdict": verdict,
                    "document_version": document.file_version,
                    "base_epoch": document.epoch_pair.base_epoch,
                    "knowledge_epoch": document.epoch_pair.knowledge_epoch,
                    "started_at": _timestamp(started_at),
                    "completed_at": _timestamp(),
                },
            )
        )

    def _client_or_raise(self) -> LeanMcpClient:
        if self._client is None:
            raise LeanRuntimeError("RUNTIME_NOT_STARTED")
        return self._client


def _opaque_id() -> str:
    return secrets.token_hex(16)


def _timestamp(value: datetime | None = None) -> str:
    current = utc_now() if value is None else value
    return current.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _failure_hash(error: LeanRuntimeError) -> str:
    return sha256_json({"code": error.code})
