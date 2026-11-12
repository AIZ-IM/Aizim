from __future__ import annotations

import asyncio
import base64
import json
import signal
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from aizim.agents.usage import checked_usage
from aizim.omp import rpc
from aizim.omp.rpc import RpcClient, RpcError

READY = {
    "type": "ready", "protocolVersion": 1, "supportedProtocolVersions": [1, 2],
    "maxFrameBytes": rpc.MAX_FRAME_BYTES,
    "maxReassembledFrameBytes": rpc.MAX_REASSEMBLED_FRAME_BYTES,
}


def _line(frame: dict[str, Any]) -> bytes:
    return (json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


class FakeStdin:
    def __init__(self, child: FakeChild) -> None:
        self.child = child
        self.closed = False
        self.fail_write = False

    def write(self, data: bytes) -> None:
        if self.fail_write:
            raise BrokenPipeError("private transport failure")
        if self.closed:
            raise BrokenPipeError("private closed stream")
        command = json.loads(data)
        self.child.sent.put_nowait(command)
        if command["type"] == "negotiate_protocol" and self.child.negotiate:
            self.child.respond(command, {"protocolVersion": 2})

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        if self.child.exit_on_close:
            self.child.exit(0)


class FakeChild:
    def __init__(self, ready: dict[str, Any] | None = READY) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = FakeStdin(self)
        self.sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.returncode: int | None = None
        self.pid: int | None = None
        self.exited = asyncio.Event()
        self.negotiate = True
        self.exit_on_close = True
        self.exit_on_terminate = True
        self.signals: list[signal.Signals] = []
        if ready is not None:
            self.emit(ready)

    def emit(self, frame: dict[str, Any]) -> None:
        self.stdout.feed_data(_line(frame))

    def respond(self, command: dict[str, Any], data: Any = None, *, success: bool = True) -> None:
        self.emit({"type": "response", "id": command["id"], "command": command["type"],
                   "success": success, "data": data, "error": "private server error"})

    async def command(self, kind: str) -> dict[str, Any]:
        while True:
            command = await self.sent.get()
            if command["type"] == kind:
                return command

    def exit(self, code: int) -> None:
        if self.returncode is None:
            self.returncode = code
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.exited.set()

    def terminate(self) -> None:
        self.signals.append(signal.SIGTERM)
        if self.exit_on_terminate:
            self.exit(-signal.SIGTERM)

    def kill(self) -> None:
        self.signals.append(signal.SIGKILL)
        self.exit(-signal.SIGKILL)

    async def wait(self) -> int:
        await self.exited.wait()
        assert self.returncode is not None
        return self.returncode


@asynccontextmanager
async def _client(
    host_tool=None, child: FakeChild | None = None,
) -> AsyncGenerator[tuple[RpcClient, FakeChild]]:
    child = child or FakeChild()
    client = RpcClient(child, host_tool)
    try:
        await client.start()
        yield client, child
    finally:
        await client.close()


async def _barrier(client: RpcClient, child: FakeChild) -> None:
    pending = asyncio.create_task(client.request("get_state"))
    command = await child.command("get_state")
    child.respond(command, {"isSettled": True})
    await pending


def _chunks(frame: dict[str, Any], *, pieces: int = 3) -> list[dict[str, Any]]:
    raw = _line(frame)[:-1]
    size = (len(raw) + pieces - 1) // pieces
    blocks = [raw[index:index + size] for index in range(0, len(raw), size)]
    return [{"type": "rpc_chunk", "chunkId": "rpc-1", "index": index,
             "count": len(blocks), "byteLength": len(raw),
             "data": base64.b64encode(block).decode("ascii")}
            for index, block in enumerate(blocks)]


async def test_start_negotiates_and_requests_correlate_out_of_order() -> None:
    async with _client() as (client, child):
        await client.start()
        negotiation = await child.command("negotiate_protocol")
        assert negotiation["protocolVersion"] == 2
        first = asyncio.create_task(client.request("get_state"))
        second = asyncio.create_task(client.request("set_host_tools", tools=[]))
        first_command = await child.command("get_state")
        second_command = await child.command("set_host_tools")
        assert first_command["id"] != second_command["id"]
        assert second_command["tools"] == []
        child.respond(second_command, {"installed": True})
        child.respond(first_command, {"model": {"provider": "magpie", "id": "selected"}})
        assert await second == {"installed": True}
        assert await first == {"model": {"provider": "magpie", "id": "selected"}}


@pytest.mark.parametrize("ready", [
    {**READY, "supportedProtocolVersions": [1]},
    {**READY, "maxFrameBytes": True},
    {key: value for key, value in READY.items() if key != "maxFrameBytes"},
    {**READY, "maxFrameBytes": None},
    {key: value for key, value in READY.items() if key != "maxReassembledFrameBytes"},
    {**READY, "maxReassembledFrameBytes": None},
    {**READY, "maxReassembledFrameBytes": True},
    {**READY, "maxReassembledFrameBytes": rpc.MAX_REASSEMBLED_FRAME_BYTES + 1},
])
async def test_start_rejects_unsupported_ready(ready: dict[str, Any]) -> None:
    child = FakeChild(ready)
    client = RpcClient(child)
    try:
        with pytest.raises(RpcError, match=r"^OMP_RPC_PROTOCOL_UNSUPPORTED$"):
            await client.start()
    finally:
        await client.close()


async def test_negotiation_failure_has_only_a_stable_error() -> None:
    child = FakeChild()
    child.negotiate = False
    client = RpcClient(child)
    start = asyncio.create_task(client.start())
    try:
        command = await child.command("negotiate_protocol")
        child.respond(command, success=False)
        with pytest.raises(RpcError, match=r"^OMP_RPC_COMMAND_FAILED$") as raised:
            await start
        assert raised.value.code == "OMP_RPC_COMMAND_FAILED"
        assert "private" not in repr(raised.value)
    finally:
        await client.close()


async def test_prompt_waits_for_its_result_and_session_settlement() -> None:
    async with _client() as (client, child):
        prompt = asyncio.create_task(client.prompt("research"))
        command = await child.command("prompt")
        child.respond(command)
        child.emit({"type": "agent_end", "messages": [], "yielded": True})
        await _barrier(client, child)
        assert not prompt.done()
        child.emit({"type": "prompt_result", "id": command["id"], "agentInvoked": True,
                    "status": "completed", "sessionSettled": False})
        await _barrier(client, child)
        assert not prompt.done()
        child.emit({"type": "session_settled"})
        await prompt


async def test_local_prompt_completes_without_agent_events() -> None:
    async with _client() as (client, child):
        prompt = asyncio.create_task(client.prompt("/local"))
        command = await child.command("prompt")
        child.respond(command, {"agentInvoked": False})
        await prompt


@pytest.mark.parametrize("status,code", [
    ("error", "OMP_RPC_PROMPT_FAILED"), ("aborted", "OMP_RPC_PROMPT_ABORTED"),
])
async def test_prompt_errors_never_echo_provider_diagnostics(status: str, code: str) -> None:
    async with _client() as (client, child):
        prompt = asyncio.create_task(client.prompt("private prompt"))
        command = await child.command("prompt")
        child.respond(command)
        child.emit({"type": "prompt_result", "id": command["id"], "agentInvoked": True,
                    "status": status, "sessionSettled": True,
                    "error": {"message": "private provider response"}})
        with pytest.raises(RpcError, match=f"^{code}$"):
            await prompt


async def test_predispatch_prompt_error_and_result_leave_client_usable() -> None:
    async with _client() as (client, child):
        pending = asyncio.create_task(client.prompt("work"))
        command = await child.command("prompt")
        child.respond(command, success=False)
        child.emit({"type": "prompt_result", "id": command["id"], "agentInvoked": False,
                    "status": "error", "sessionSettled": True,
                    "error": {"message": "private preparation error"}})
        with pytest.raises(RpcError, match=r"^OMP_RPC_COMMAND_FAILED$"):
            await pending
        await _barrier(client, child)


async def test_abort_can_be_sent_while_prompt_is_running() -> None:
    async with _client() as (client, child):
        prompt = asyncio.create_task(client.prompt("work"))
        command = await child.command("prompt")
        child.respond(command)
        abort = asyncio.create_task(client.abort())
        abort_command = await child.command("abort")
        child.emit({"type": "prompt_result", "id": command["id"], "agentInvoked": True,
                    "status": "aborted", "sessionSettled": True})
        child.respond(abort_command)
        await abort
        with pytest.raises(RpcError, match=r"^OMP_RPC_PROMPT_ABORTED$"):
            await prompt


async def test_late_prompt_error_after_acknowledgement_is_not_lost() -> None:
    async with _client() as (client, child):
        prompt = asyncio.create_task(client.prompt("work"))
        command = await child.command("prompt")
        child.respond(command)
        await _barrier(client, child)
        child.respond(command, success=False)
        with pytest.raises(RpcError, match=r"^OMP_RPC_PROMPT_FAILED$"):
            await prompt


async def test_chunked_response_reassembles_utf8_and_preserves_data() -> None:
    async with _client() as (client, child):
        pending = asyncio.create_task(client.request("get_state"))
        command = await child.command("get_state")
        data = {"model": {"provider": "magpie", "id": "模型"}}
        frame = {"type": "response", "id": command["id"], "command": "get_state",
                 "success": True, "data": data}
        for chunk in _chunks(frame):
            child.emit(chunk)
        assert await pending == data


@pytest.mark.parametrize("mutation", [
    "index", "id", "count", "length", "oversized", "base64", "empty", "interrupted",
    "invalid_utf8", "nested", "length_mismatch", "bool_index", "bool_count", "bool_byteLength",
    "missing_index", "missing_count", "missing_byteLength",
    "none_index", "none_count", "none_byteLength",
])
async def test_chunk_validation_is_strict(mutation: str) -> None:
    async with _client() as (client, child):
        pending = asyncio.create_task(client.request("get_state"))
        command = await child.command("get_state")
        frame = {"type": "response", "id": command["id"], "command": "get_state",
                 "success": True, "data": {"secret": "private payload"}}
        chunks = _chunks(frame)
        if mutation == "index":
            chunks[0]["index"] = 1
        elif mutation == "id":
            chunks[1]["chunkId"] = "other"
        elif mutation == "count":
            chunks[1]["count"] += 1
        elif mutation == "length":
            chunks[1]["byteLength"] += 1
        elif mutation == "oversized":
            chunks[0]["byteLength"] = rpc.MAX_REASSEMBLED_FRAME_BYTES + 1
        elif mutation == "base64":
            chunks[0]["data"] = "not base64!"
        elif mutation == "empty":
            chunks[0]["data"] = ""
        elif mutation == "interrupted":
            chunks[1] = {"type": "agent_start"}
        elif mutation == "invalid_utf8":
            chunks = [{"type": "rpc_chunk", "chunkId": "rpc-1", "index": 0, "count": 1,
                       "byteLength": 1, "data": base64.b64encode(b"\xff").decode()}]
        elif mutation == "nested":
            chunks = _chunks({"type": "rpc_chunk"})
        elif mutation == "length_mismatch":
            for chunk in chunks:
                chunk["byteLength"] += 1
        elif mutation.startswith("bool_"):
            chunks[0][mutation.removeprefix("bool_")] = False
        elif mutation.startswith("missing_"):
            del chunks[0][mutation.removeprefix("missing_")]
        elif mutation.startswith("none_"):
            chunks[0][mutation.removeprefix("none_")] = None
        for chunk in chunks:
            child.emit(chunk)
        with pytest.raises(RpcError) as raised:
            await pending
        assert raised.value.code in {"OMP_RPC_CHUNK_INVALID", "OMP_RPC_FRAME_INVALID"}
        assert "private" not in str(raised.value)


@pytest.mark.parametrize("raw,code", [
    (b'{"type":"private secret"\n', "OMP_RPC_FRAME_INVALID"),
    (b'{"type":"notice","type":"private secret"}\n', "OMP_RPC_FRAME_INVALID"),
    (b'{"type":"notice","value":NaN}\n', "OMP_RPC_FRAME_INVALID"),
    (b'\xff\n', "OMP_RPC_FRAME_INVALID"),
    (b"x" * rpc.MAX_FRAME_BYTES, "OMP_RPC_FRAME_TOO_LARGE"),
])
async def test_malformed_frames_and_frame_bounds_do_not_expose_bytes(raw: bytes, code: str) -> None:
    async with _client() as (client, child):
        pending = asyncio.create_task(client.request("get_state"))
        await child.command("get_state")
        child.stdout.feed_data(raw)
        with pytest.raises(RpcError, match=f"^{code}$") as raised:
            await pending
        assert raised.value.__cause__ is None
        assert "private" not in repr(raised.value)
        assert len(client.transport_digest) == 64


async def test_mismatched_response_command_fails_correlation() -> None:
    async with _client() as (client, child):
        pending = asyncio.create_task(client.request("get_state"))
        command = await child.command("get_state")
        child.emit({"type": "response", "id": command["id"], "command": "abort", "success": True})
        with pytest.raises(RpcError, match=r"^OMP_RPC_RESPONSE_INVALID$"):
            await pending


async def test_host_callback_is_concurrent_with_requests_and_returns_serialized_text() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def host(name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        assert name == "publish"
        assert arguments == {"candidate": "one"}
        entered.set()
        await release.wait()
        return '{"accepted":true}', False

    async with _client(host) as (client, child):
        child.emit({"type": "host_tool_call", "id": "host-1", "toolName": "publish",
                    "toolCallId": "tool-1", "arguments": {"candidate": "one"}})
        await entered.wait()
        await _barrier(client, child)
        release.set()
        result = await child.command("host_tool_result")
        assert result == {"type": "host_tool_result", "id": "host-1", "isError": False,
                          "result": {"content": [{"type": "text", "text": '{"accepted":true}'}]}}


@pytest.mark.parametrize("mode,expected", [
    ("none", "OMP_HOST_TOOL_UNAVAILABLE"), ("error", "OMP_HOST_TOOL_FAILED"),
    ("too_large", "OMP_HOST_TOOL_RESULT_TOO_LARGE"),
])
async def test_host_tool_failure_is_bounded_and_generic(mode: str, expected: str) -> None:
    async def host(_name: str, _arguments: dict[str, Any]) -> tuple[str, bool] | None:
        if mode == "none":
            return None
        if mode == "error":
            raise RuntimeError("private gateway detail")
        return "private payload" * rpc.MAX_FRAME_BYTES, False

    async with _client(host) as (_transport, child):
        child.emit({"type": "host_tool_call", "id": "host-1", "toolName": "publish",
                    "arguments": {}})
        result = await child.command("host_tool_result")
        assert result["isError"] is True
        assert result["result"]["content"] == [{"type": "text", "text": expected}]


async def test_host_cancellation_reaches_callback_and_suppresses_result() -> None:
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def host(_name: str, _arguments: dict[str, Any]) -> tuple[str, bool]:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            # Even a handler suppressing cancellation must not reply to a revoked call.
            return "late result", False
        raise AssertionError

    async with _client(host) as (client, child):
        child.emit({"type": "host_tool_call", "id": "host-1", "toolName": "publish",
                    "arguments": {}})
        await entered.wait()
        child.emit({"type": "host_tool_cancel", "id": "cancel-1", "targetId": "host-1"})
        await cancelled.wait()
        await _barrier(client, child)
        assert child.sent.empty()


async def test_messages_are_bounded_and_usage_counts_evicted_messages(monkeypatch) -> None:
    monkeypatch.setattr(rpc, "MAX_MESSAGES", 2)
    async with _client() as (client, child):
        for index in range(4):
            child.emit({"type": "message_end", "message": {
                "role": "assistant", "content": [{"type": "text", "text": str(index)}],
                "usage": {"input": 100, "output": 7, "cacheRead": 30, "cacheWrite": 900,
                          "totalTokens": 1037, "cost": {"total": 99}},
            }})
        child.emit({"type": "message_end", "message": {"role": "user", "content": "ignored"}})
        await _barrier(client, child)
        assert [message["content"][0]["text"] for message in client.messages] == ["2", "3"]
        assert client.usage == {
            "input_tokens": 4120, "cached_input_tokens": 120, "output_tokens": 28,
        }


@pytest.mark.parametrize("usage", [
    {"input": 2085, "cacheRead": 3968, "output": 10, "totalTokens": 6063},
    {"input": 25, "cacheRead": 400, "cacheWrite": 175, "output": 12, "totalTokens": 612},
])
async def test_cached_prompt_usage_matches_attempt_accounting(usage: dict[str, int]) -> None:
    async with _client() as (client, child):
        child.emit({"type": "message_end", "message": {"role": "assistant", "usage": usage}})
        await _barrier(client, child)
        normalized = client.usage
        assert normalized is not None
        assert checked_usage(normalized) == normalized
        assert normalized["input_tokens"] + normalized["output_tokens"] == usage["totalTokens"]
        assert normalized["cached_input_tokens"] == usage["cacheRead"]


@pytest.mark.parametrize("bad_usage", [None, {"input": 1, "output": 2},
                                       {"input": True, "cacheRead": 0, "output": 1},
                                       {"input": -1, "cacheRead": 0, "output": 1},
                                       {"input": 1, "cacheRead": 0, "cacheWrite": True,
                                        "output": 1},
                                       {"input": 1, "cacheRead": 0, "cacheWrite": -1, "output": 1}])
async def test_incomplete_usage_stays_unknown(bad_usage: Any) -> None:
    async with _client() as (client, child):
        assert client.usage is None
        good = {"input": 10, "cacheRead": 3, "output": 2}
        for usage in (good, bad_usage, good):
            child.emit({"type": "message_end", "message": {"role": "assistant", "usage": usage}})
        await _barrier(client, child)
        assert client.usage is None


async def test_message_byte_budget_drops_oversized_messages_but_counts_usage(monkeypatch) -> None:
    monkeypatch.setattr(rpc, "MAX_MESSAGE_BYTES", 200)
    async with _client() as (client, child):
        child.emit({"type": "message_end", "message": {
            "role": "assistant", "content": "x" * 300,
            "usage": {"input": 1, "cacheRead": 0, "output": 2},
        }})
        await _barrier(client, child)
        assert client.messages == []
        assert client.usage == {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 2}


async def test_stderr_is_drained_into_digest_without_retained_text() -> None:
    child = FakeChild()
    client = RpcClient(child)
    await client.start()
    before = client.transport_digest
    child.stderr.feed_data(b"private stderr secret" * 100_000)
    await client.close()
    assert child.stderr.at_eof()
    assert client.transport_digest != before
    assert "private" not in repr(client.__dict__)
    assert client.exit_code == 0
    await client.close()


async def test_eof_and_write_failures_settle_pending_requests() -> None:
    async with _client() as (client, child):
        pending = asyncio.create_task(client.request("get_state"))
        await child.command("get_state")
        child.exit(17)
        with pytest.raises(RpcError, match=r"^OMP_RPC_CHILD_EXITED$"):
            await pending
        assert client.exit_code == 17
    async with _client() as (client, child):
        child.stdin.fail_write = True
        with pytest.raises(RpcError, match=r"^OMP_RPC_WRITE_FAILED$"):
            await client.request("get_state")


async def test_cancelled_request_does_not_poison_later_correlation() -> None:
    async with _client() as (client, child):
        pending = asyncio.create_task(client.request("get_state"))
        command = await child.command("get_state")
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        child.respond(command, {"late": True})
        await _barrier(client, child)


async def test_close_terminates_then_kills_uncooperative_child(monkeypatch) -> None:
    monkeypatch.setattr(rpc, "CLOSE_GRACE_SECONDS", 0.001)
    child = FakeChild()
    child.exit_on_close = False
    child.exit_on_terminate = False
    client = RpcClient(child)
    await client.start()
    await client.close()
    assert child.signals == [signal.SIGTERM, signal.SIGKILL]
    assert client.exit_code == -signal.SIGKILL
    assert child.stdout.at_eof()
    assert child.stderr.at_eof()


async def test_close_signals_isolated_group_even_after_leader_exit(monkeypatch) -> None:
    child = FakeChild()
    child.pid = 123_456
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(rpc.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(rpc.os, "killpg", lambda pid, value: signals.append((pid, value)))
    client = RpcClient(child)
    await client.start()
    await client.close()
    assert signals == [(child.pid, signal.SIGTERM), (child.pid, signal.SIGKILL)]


async def test_close_cancels_prompt_and_host_work() -> None:
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def host(_name: str, _arguments: dict[str, Any]) -> tuple[str, bool]:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError

    async with _client(host) as (client, child):
        pending = asyncio.create_task(client.prompt("work"))
        command = await child.command("prompt")
        child.respond(command)
        child.emit({"type": "host_tool_call", "id": "host-1", "toolName": "publish",
                    "arguments": {}})
        await entered.wait()
        await client.close()
        await cancelled.wait()
        with pytest.raises(RpcError, match=r"^OMP_RPC_CLOSED$"):
            await pending
