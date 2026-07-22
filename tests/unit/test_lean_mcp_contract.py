from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from mcp.types import TextContent

from aizim.lean import DocumentBroker
from aizim.lean.mcp_client import EXPECTED_TOOL_NAMES, LeanMcpClient
from aizim.lean.models import LeanRuntimeError
from aizim.lean.project import materialize_smoke_project
from aizim.lean.runtime import SharedLeanRuntime
from aizim.state import StateService

SMOKE_ROOT = Path(__file__).parents[2] / "examples" / "smoke_lean"


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
            else '{"axioms":[],"warnings":[]}'
        )
        return SimpleNamespace(isError=False, content=[TextContent(type="text", text=payload)])


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
