from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from mcp.types import TextContent

from aizim.lean import DocumentBroker, promotion_runtime
from aizim.lean.mcp_client import EXPECTED_TOOL_NAMES, LeanMcpClient
from aizim.lean.models import DiagnosticsResult, LeanRuntimeError
from aizim.lean.project import materialize_smoke_project
from aizim.lean.promotion_runtime import _complete_diagnostics
from aizim.lean.runtime import SharedLeanRuntime
from aizim.lean.verification import BuildResult
from aizim.state import StateService

SMOKE_ROOT = Path(__file__).parents[2] / "examples" / "smoke_lean"


@pytest.mark.lean_integration
@pytest.mark.asyncio
async def test_pinned_mcp_server_exposes_only_the_reviewed_tool_surface(tmp_path: Path) -> None:
    project = materialize_smoke_project(tmp_path, "run-1", SMOKE_ROOT)
    client = LeanMcpClient(project)
    try:
        await client.start()

        assert client.tool_names == EXPECTED_TOOL_NAMES
        assert "--repl" not in client.arguments
        assert "--loogle-local" not in client.arguments
        assert client.project_root == project.resolve()
        assert (
            await client.hover(project / "AizimSmoke" / "Base.lean", 5, 9)
            == "AizimSmoke.base_add_zero (n : Nat) : n + 0 = n"
        )
    finally:
        await client.aclose()


class _Session:
    def __init__(self, result: object) -> None:
        self._result = result

    async def call_tool(self, *_arguments: object, **_keyword_arguments: object) -> object:
        return self._result


class _RecordingSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call_tool(self, name: str, arguments: dict[str, object], **_kwargs: object) -> object:
        self.calls.append((name, arguments))
        payload = (
            '{"success":true,"output":"","errors":[]}'
            if name == "lean_build"
            else (
                '{"partial":false,"still_elaborating_lines":null,"success":true,'
                '"timed_out":false,"items":[],"failed_dependencies":[]}'
            )
            if name == "lean_diagnostic_messages"
            else (
                '{"symbol":"AizimSmoke.base_add_zero",'
                '"info":"AizimSmoke.base_add_zero : (n : Nat) → n + 0 = n",'
                '"diagnostics":[]}'
            )
            if name == "lean_hover_info"
            else '{"axioms":[],"warnings":[]}'
        )
        return SimpleNamespace(isError=False, content=[TextContent(type="text", text=payload)])


class _TimeoutRecordingSession:
    def __init__(self) -> None:
        self.read_timeout: timedelta | None = None

    async def call_tool(
        self,
        _name: str,
        _arguments: dict[str, object],
        *,
        read_timeout_seconds: timedelta,
    ) -> object:
        self.read_timeout = read_timeout_seconds
        return SimpleNamespace(
            isError=False,
            content=[
                TextContent(
                    type="text",
                    text='{"success":true,"output":"","errors":[]}',
                )
            ],
        )


@pytest.mark.asyncio
async def test_client_allows_three_minutes_for_a_bounded_mcp_tool_call(tmp_path: Path) -> None:
    project = materialize_smoke_project(tmp_path, "run-1", SMOKE_ROOT)
    client = LeanMcpClient(project)
    session = _TimeoutRecordingSession()
    object.__setattr__(client, "_session", session)

    await client.build(clean=False, fetch_cache=False)

    assert session.read_timeout == timedelta(seconds=180)


@pytest.mark.asyncio
async def test_client_sends_an_explicit_diagnostics_elaboration_timeout(tmp_path: Path) -> None:
    project = materialize_smoke_project(tmp_path, "run-1", SMOKE_ROOT)
    client = LeanMcpClient(project)
    session = _RecordingSession()
    object.__setattr__(client, "_session", session)
    source = project / "AizimSmoke" / "Base.lean"

    await client.diagnostics(source, None, None, timeout_seconds=60)

    assert session.calls == [
        (
            "lean_diagnostic_messages",
            {
                "file_path": str(source.resolve()),
                "timeout_s": 60,
            },
        )
    ]


class _ElaboratingClient:
    def __init__(self) -> None:
        self.calls: list[int | None] = []
        self.results = [
            DiagnosticsResult(True, (1,), True, True, (), (), "a" * 64),
            DiagnosticsResult(False, None, True, False, (), (), "b" * 64),
        ]

    async def diagnostics(
        self,
        _path: Path,
        _start_line: int | None,
        _end_line: int | None,
        *,
        timeout_seconds: int | None = None,
    ) -> DiagnosticsResult:
        self.calls.append(timeout_seconds)
        return self.results.pop(0)


@pytest.mark.asyncio
async def test_promotion_polls_diagnostics_until_elaboration_finishes() -> None:
    client = _ElaboratingClient()

    result = await _complete_diagnostics(
        cast(LeanMcpClient, cast(object, client)), SMOKE_ROOT / "AizimSmoke" / "Base.lean"
    )

    assert not result.partial
    assert not result.timed_out
    assert client.calls == [60, 60]


class _ColdElaboratingClient:
    def __init__(self) -> None:
        self.calls: list[int | None] = []
        self.ready = False

    async def diagnostics(
        self,
        _path: Path,
        _start_line: int | None,
        _end_line: int | None,
        *,
        timeout_seconds: int | None = None,
    ) -> DiagnosticsResult:
        self.calls.append(timeout_seconds)
        return DiagnosticsResult(
            not self.ready,
            (1,) if not self.ready else None,
            True,
            not self.ready,
            (),
            (),
            "a" * 64,
        )


@pytest.mark.asyncio
async def test_promotion_yields_while_waiting_for_cold_elaboration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _ColdElaboratingClient()
    monkeypatch.setattr(
        promotion_runtime, "_DIAGNOSTICS_POLL_SECONDS", 0, raising=False
    )

    async def finish_elaboration() -> None:
        await asyncio.sleep(0)
        client.ready = True

    task = asyncio.create_task(finish_elaboration())
    result = await _complete_diagnostics(
        cast(LeanMcpClient, cast(object, client)),
        SMOKE_ROOT / "AizimSmoke" / "Base.lean",
    )
    await task

    assert not result.partial
    assert not result.timed_out
    assert client.calls == [60, 60, 60]


class _PreparationClient:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def build(self, *, clean: bool, fetch_cache: bool) -> BuildResult:
        self.calls.append(("build", clean, fetch_cache))
        return BuildResult(True, "", (), "a" * 64)

    async def diagnostics(
        self, path: Path, start_line: int | None, end_line: int | None
    ) -> DiagnosticsResult:
        self.calls.append(("diagnostics", path, start_line, end_line))
        return DiagnosticsResult(False, None, True, False, (), (), "b" * 64)


@pytest.mark.asyncio
async def test_runtime_prepares_the_base_project_before_worker_deadlines() -> None:
    runtime = SharedLeanRuntime(
        cast(StateService, cast(object, _RecordingState())), cast(DocumentBroker, object()), "run-1"
    )
    client = _PreparationClient()

    async def ensure_started(project_root: Path) -> LeanMcpClient:
        assert project_root == SMOKE_ROOT
        return cast(LeanMcpClient, cast(object, client))

    object.__setattr__(runtime, "_ensure_started", ensure_started)

    await runtime.prepare(SMOKE_ROOT)
    await runtime.prepare(SMOKE_ROOT)

    assert client.calls == [
        ("build", False, False),
        ("diagnostics", SMOKE_ROOT / "AizimSmoke" / "Base.lean", None, None),
    ]


@pytest.mark.asyncio
async def test_client_rejects_non_text_oversized_and_unreviewed_goal_responses(
    tmp_path: Path,
) -> None:
    project = materialize_smoke_project(tmp_path, "run-1", SMOKE_ROOT)
    client = LeanMcpClient(project)
    object.__setattr__(
        client, "_session", _Session(SimpleNamespace(isError=False, content=[object()]))
    )
    with pytest.raises(LeanRuntimeError, match="INVALID_LEAN_RESPONSE"):
        await client._call("lean_goal", {})

    oversized = TextContent(type="text", text="{" + '"x":"' + "a" * (4 * 1024 * 1024) + '"}')
    object.__setattr__(
        client, "_session", _Session(SimpleNamespace(isError=False, content=[oversized]))
    )
    with pytest.raises(LeanRuntimeError, match="LEAN_RESPONSE_TOO_LARGE"):
        await client._call("lean_goal", {})

    unreviewed = TextContent(
        type="text",
        text=(
            '{"line_context":"x","goals":null,"goals_before":[],"goals_after":[],'
            '"status":null,"unexpected":"field"}'
        ),
    )
    object.__setattr__(
        client, "_session", _Session(SimpleNamespace(isError=False, content=[unreviewed]))
    )
    with pytest.raises(LeanRuntimeError, match="INVALID_LEAN_RESPONSE"):
        await client.goal(project / "AizimSmoke" / "Base.lean", 5, None)


@pytest.mark.asyncio
async def test_client_sends_explicit_clean_cache_and_source_scan_flags(tmp_path: Path) -> None:
    project = materialize_smoke_project(tmp_path, "run-1", SMOKE_ROOT)
    client = LeanMcpClient(project)
    session = _RecordingSession()
    object.__setattr__(client, "_session", session)

    await client.build(clean=False, fetch_cache=False)
    await client.verify(
        project / "AizimSmoke" / "Base.lean", "AizimSmoke.base_add_zero", scan_source=True
    )

    assert session.calls == [
        ("lean_build", {"clean": False, "fetch_cache": False}),
        (
            "lean_verify",
            {
                "file_path": str((project / "AizimSmoke" / "Base.lean").resolve()),
                "theorem_name": "AizimSmoke.base_add_zero",
                "scan_source": True,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_client_uses_reviewed_hover_for_trusted_type_lookup(tmp_path: Path) -> None:
    project = materialize_smoke_project(tmp_path, "run-1", SMOKE_ROOT)
    client = LeanMcpClient(project)
    session = _RecordingSession()
    object.__setattr__(client, "_session", session)

    info = await client.hover(project / "AizimSmoke" / "Base.lean", 5, 9)

    assert info == "AizimSmoke.base_add_zero : (n : Nat) → n + 0 = n"
    assert session.calls == [
        (
            "lean_hover_info",
            {
                "file_path": str((project / "AizimSmoke" / "Base.lean").resolve()),
                "line": 5,
                "column": 9,
            },
        )
    ]


class _RecordingState:
    def append_event(self, _command: object) -> None:
        pass


class _ClosedClient:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _StartFailure:
    def __init__(self, _project_root: Path) -> None:
        pass

    async def start(self) -> None:
        raise LeanRuntimeError("MCP_START_FAILED")


@pytest.mark.asyncio
async def test_failed_restart_does_not_retain_a_closed_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = SharedLeanRuntime(
        cast(StateService, cast(object, _RecordingState())), cast(DocumentBroker, object()), "run-1"
    )
    closed = _ClosedClient()
    failing = cast(LeanMcpClient, cast(object, closed))
    runtime._client = failing
    runtime._runtime_id = "runtime-1"
    monkeypatch.setattr("aizim.lean.runtime.LeanMcpClient", _StartFailure)

    with pytest.raises(LeanRuntimeError, match="MCP_START_FAILED"):
        await runtime._restart(failing, tmp_path)

    assert closed.closed
    assert runtime._client is None
