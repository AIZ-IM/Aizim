from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from contextlib import AsyncExitStack, suppress
from datetime import timedelta
from pathlib import Path
from typing import Final

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import TextContent

from aizim.domain import sha256_json

from .mcp_parsing import parse_attempts, parse_diagnostics, parse_goal, parse_hover_info
from .models import (
    DiagnosticsResult,
    GoalResult,
    LeanProcess,
    LeanRuntimeError,
    MultiAttemptResult,
)
from .verification import BuildResult, VerificationResult, parse_build, parse_verification

EXPECTED_TOOL_NAMES: Final = (
    "lean_build",
    "lean_diagnostic_messages",
    "lean_goal",
    "lean_hover_info",
    "lean_multi_attempt",
    "lean_verify",
)
_DISABLED_TOOLS: Final = (
    "lean_code_actions,lean_completions,lean_declaration_file,lean_file_outline,"
    "lean_get_widget_source,lean_get_widgets,lean_hammer_premise,"
    "lean_leanfinder,lean_leansearch,lean_local_search,lean_loogle,"
    "lean_minimal_hypotheses,lean_profile_proof,lean_references,lean_run_code,"
    "lean_state_search,lean_term_goal"
)
_MAX_RESPONSE_BYTES: Final = 4 * 1024 * 1024


class LeanMcpClient:
    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root.resolve(strict=True)
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tool_names: tuple[str, ...] = ()
        self._mcp_pid: int | None = None

    @property
    def arguments(self) -> tuple[str, ...]:
        return (
            "--transport",
            "stdio",
            "--lean-project-path",
            str(self._project_root),
            "--disable-tools",
            _DISABLED_TOOLS,
        )

    @property
    def project_root(self) -> Path:
        return self._project_root

    @property
    def tool_names(self) -> tuple[str, ...]:
        return self._tool_names

    @property
    def mcp_process_id(self) -> int:
        if self._mcp_pid is None:
            candidates = [
                item
                for item in self._all_processes()
                if item.parent_pid == os.getpid() and "lean-lsp-mcp" in item.command
            ]
            if len(candidates) != 1:
                raise LeanRuntimeError("MCP_PROCESS_NOT_FOUND")
            self._mcp_pid = candidates[0].pid
        return self._mcp_pid

    async def start(self) -> None:
        if self._stack is not None:
            raise LeanRuntimeError("MCP_CLIENT_ALREADY_STARTED")
        executable = Path(sys.executable).parent / "lean-lsp-mcp"
        if not executable.is_file():
            raise LeanRuntimeError("MCP_EXECUTABLE_NOT_FOUND")
        stack = AsyncExitStack()
        try:
            stderr = stack.enter_context(
                os.fdopen(os.open(os.devnull, os.O_WRONLY), "w", encoding="utf-8")
            )
            streams = await stack.enter_async_context(
                stdio_client(
                    StdioServerParameters(
                        command=str(executable),
                        args=list(self.arguments),
                        env={"PATH": os.environ.get("PATH", ""), "LEAN_LOG_LEVEL": "CRITICAL"},
                        cwd=str(self._project_root),
                    ),
                    errlog=stderr,
                )
            )
            session = await stack.enter_async_context(ClientSession(*streams))
            await session.initialize()
            tools = await session.list_tools()
            names = tuple(sorted(item.name for item in tools.tools))
            if names != EXPECTED_TOOL_NAMES or tools.nextCursor is not None:
                raise LeanRuntimeError("MCP_TOOL_SURFACE_MISMATCH")
        except LeanRuntimeError:
            await stack.aclose()
            raise
        except Exception:
            await stack.aclose()
            raise LeanRuntimeError("MCP_START_FAILED") from None
        self._stack = stack
        self._session = session
        self._tool_names = names

    async def aclose(self) -> None:
        stack, self._stack = self._stack, None
        self._session = None
        self._mcp_pid = None
        if stack is not None:
            with suppress(Exception):
                await stack.aclose()

    async def goal(self, path: Path, line: int, column: int | None) -> GoalResult:
        payload, response_hash = await self._call(
            "lean_goal", self._file_arguments(path, line, column, {"format": "structured"})
        )
        return parse_goal(payload, response_hash)

    async def multi_attempt(
        self, path: Path, line: int, snippets: tuple[str, ...], column: int | None
    ) -> MultiAttemptResult:
        arguments = self._file_arguments(path, line, column, {"snippets": list(snippets)})
        payload, response_hash = await self._call("lean_multi_attempt", arguments)
        return parse_attempts(payload, response_hash)

    async def diagnostics(
        self, path: Path, start_line: int | None, end_line: int | None
    ) -> DiagnosticsResult:
        arguments: dict[str, object] = {"file_path": str(self._trusted_path(path))}
        if start_line is not None:
            arguments["start_line"] = start_line
        if end_line is not None:
            arguments["end_line"] = end_line
        payload, response_hash = await self._call("lean_diagnostic_messages", arguments)
        return parse_diagnostics(payload, response_hash)

    async def hover(self, path: Path, line: int, column: int) -> str:
        arguments = self._file_arguments(path, line, column, {})
        payload, _ = await self._call("lean_hover_info", arguments)
        return parse_hover_info(payload)

    async def build(self, *, clean: bool, fetch_cache: bool) -> BuildResult:
        if type(clean) is not bool or type(fetch_cache) is not bool:
            raise LeanRuntimeError("INVALID_LEAN_REQUEST")
        payload, response_hash = await self._call(
            "lean_build", {"clean": clean, "fetch_cache": fetch_cache}
        )
        return parse_build(payload, response_hash)

    async def verify(
        self, path: Path, theorem_name: str, *, scan_source: bool
    ) -> VerificationResult:
        if type(scan_source) is not bool:
            raise LeanRuntimeError("INVALID_LEAN_REQUEST")
        payload, response_hash = await self._call(
            "lean_verify",
            {
                "file_path": str(self._trusted_path(path)),
                "theorem_name": theorem_name,
                "scan_source": scan_source,
            },
        )
        return parse_verification(payload, response_hash)

    def processes(self) -> tuple[LeanProcess, ...]:
        root = self.mcp_process_id
        by_parent: dict[int, list[LeanProcess]] = {}
        for process in self._all_processes():
            by_parent.setdefault(process.parent_pid, []).append(process)
        descendants = [root]
        for parent in descendants:
            descendants.extend(item.pid for item in by_parent.get(parent, ()))
        visible = {root, *descendants}
        return tuple(item for item in self._all_processes() if item.pid in visible)

    async def _terminate_for_test(self) -> None:
        with suppress(ProcessLookupError):
            os.killpg(self.mcp_process_id, signal.SIGKILL)

    async def _call(self, name: str, arguments: dict[str, object]) -> tuple[dict[str, object], str]:
        if self._session is None:
            raise LeanRuntimeError("MCP_CLIENT_NOT_STARTED")
        try:
            result = await self._session.call_tool(
                name, arguments, read_timeout_seconds=timedelta(seconds=10)
            )
        except Exception:
            raise LeanRuntimeError("MCP_TRANSPORT_FAILED") from None
        if result.isError:
            raise LeanRuntimeError("MCP_TOOL_FAILED")
        if len(result.content) != 1 or not isinstance(result.content[0], TextContent):
            raise LeanRuntimeError("INVALID_LEAN_RESPONSE")
        text = result.content[0].text
        if len(text.encode()) > _MAX_RESPONSE_BYTES:
            raise LeanRuntimeError("LEAN_RESPONSE_TOO_LARGE")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            raise LeanRuntimeError("INVALID_LEAN_RESPONSE") from None
        if type(payload) is not dict or any(type(key) is not str for key in payload):
            raise LeanRuntimeError("INVALID_LEAN_RESPONSE")
        return payload, sha256_json(_sanitize(payload, self._project_root))

    def _trusted_path(self, path: Path) -> Path:
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(self._project_root)
        except (OSError, ValueError):
            raise LeanRuntimeError("MCP_PATH_DENIED") from None
        return resolved

    def _file_arguments(
        self, path: Path, line: int, column: int | None, extra: dict[str, object]
    ) -> dict[str, object]:
        if type(line) is not int or line < 1 or (column is not None and column < 1):
            raise LeanRuntimeError("INVALID_LEAN_REQUEST")
        arguments = {"file_path": str(self._trusted_path(path)), "line": line, **extra}
        if column is not None:
            arguments["column"] = column
        return arguments

    @staticmethod
    def _all_processes() -> tuple[LeanProcess, ...]:
        completed = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,command="], capture_output=True, check=False, text=True
        )
        records: list[LeanProcess] = []
        for line in completed.stdout.splitlines():
            fields = line.strip().split(None, 2)
            if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
                records.append(LeanProcess(int(fields[0]), int(fields[1]), fields[2]))
        return tuple(records)


def _sanitize(value: object, project_root: Path) -> object:
    if type(value) is str:
        return value.replace(str(project_root), "<project>")
    if type(value) is list:
        return [_sanitize(item, project_root) for item in value]
    if type(value) is dict:
        return {key: _sanitize(item, project_root) for key, item in value.items()}
    return value
