from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import signal
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, TypeGuard

MAX_FRAME_BYTES = 1_048_576
MAX_REASSEMBLED_FRAME_BYTES = 67_108_864
MAX_MESSAGE_BYTES = 1_048_576
MAX_MESSAGES = 128
CLOSE_GRACE_SECONDS = 1.0

HostTool = Callable[[str, dict[str, Any]], Awaitable[tuple[str, bool] | None]]


class RpcError(RuntimeError):
    """A transport failure whose text and code never contain child-controlled data."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _integer(value: object) -> TypeGuard[int]:
    return type(value) is int


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _invalid_constant(_value: str) -> Any:
    raise ValueError


def _decode(raw: bytes) -> dict[str, Any]:
    try:
        frame = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_object,
            parse_constant=_invalid_constant,
        )
        if not isinstance(frame, dict) or not isinstance(frame.get("type"), str):
            raise ValueError
        return frame
    except (ValueError, UnicodeError, RecursionError):
        raise RpcError("OMP_RPC_FRAME_INVALID") from None


def _future() -> asyncio.Future[Any]:
    future = asyncio.get_running_loop().create_future()
    # A prompt can fail before its acknowledgement is awaited. Retrieve failures
    # even when cancellation means that no coroutine will subsequently await it.
    future.add_done_callback(lambda item: None if item.cancelled() else item.exception())
    return future


class RpcClient:
    """One continuously drained omp child; callers own startup/prompt deadlines.

    ``process`` follows asyncio.subprocess.Process: binary stdin.write/drain/close,
    stdout.read, optional stderr.read, wait(), returncode, and terminate()/kill().
    Real children must be launched with start_new_session=True so close() can
    terminate their process group. Process-compatible fakes need no pid.
    Retained assistant messages and usage are untrusted telemetry.
    """

    def __init__(self, process: Any, host_tool: HostTool | None = None) -> None:
        self.process = process
        self.host_tool = host_tool
        self._process_group: int | None = None
        pid = getattr(process, "pid", None)
        if type(pid) is int and pid > 0:
            try:
                if os.getpgid(pid) == pid:
                    self._process_group = pid
            except OSError:
                pass
        self._counter = 0
        self._pending: dict[str, tuple[str, asyncio.Future[Any]]] = {}
        self._prompts: dict[str, dict[str, Any]] = {}
        self._tools: dict[str, asyncio.Task[None]] = {}
        self._start_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._reader: asyncio.Task[None] | None = None
        self._stderr_reader: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[Any] | None = None
        self._failure: str | None = None
        self._closing = False
        self._negotiated = False
        self._write_lock = asyncio.Lock()
        self._frame_limit = MAX_FRAME_BYTES
        self._logical_limit = MAX_REASSEMBLED_FRAME_BYTES
        self._chunks: dict[str, Any] | None = None
        self._messages: deque[tuple[dict[str, Any], int]] = deque()
        self._message_bytes = 0
        self._usage: dict[str, int] | None = None
        self._usage_incomplete = False
        self._hashes = {name: hashlib.sha256() for name in ("stdin", "stdout", "stderr")}

    @property
    def transport_digest(self) -> str:
        digest = hashlib.sha256()
        for name, stream in self._hashes.items():
            digest.update(name.encode("ascii"))
            digest.update(stream.digest())
        return digest.hexdigest()

    @property
    def messages(self) -> list[dict[str, Any]]:
        return [message for message, _size in self._messages]

    @property
    def usage(self) -> dict[str, int] | None:
        return None if self._usage is None else dict(self._usage)

    @property
    def exit_code(self) -> int | None:
        return self.process.returncode

    async def start(self) -> None:
        if self._closing:
            raise RpcError("OMP_RPC_CLOSED")
        if self._start_task is None:
            self._start_task = asyncio.create_task(self._start())
        await asyncio.shield(self._start_task)

    async def _start(self) -> None:
        self._ready = _future()
        self._reader = asyncio.create_task(self._read_stdout())
        if self.process.stderr is not None:
            self._stderr_reader = asyncio.create_task(self._read_stderr())
        await self._ready
        await self._request("negotiate_protocol", protocolVersion=2)

    async def request(self, type: str, **kwargs: Any) -> Any:
        await self.start()
        return await self._request(type, **kwargs)

    async def _request(self, command: str, **kwargs: Any) -> Any:
        identifier, response = self._register(command)
        try:
            await self._send({**kwargs, "id": identifier, "type": command})
            return await response
        finally:
            self._pending.pop(identifier, None)
            if not response.done():
                response.cancel()

    def _register(self, command: str) -> tuple[str, asyncio.Future[Any]]:
        if self._failure:
            raise RpcError(self._failure)
        if self._closing:
            raise RpcError("OMP_RPC_CLOSED")
        self._counter += 1
        identifier = f"aizim-{self._counter}"
        response = _future()
        self._pending[identifier] = (command, response)
        return identifier, response

    async def prompt(self, message: str) -> None:
        await self.start()
        identifier, response = self._register("prompt")
        completion = _future()
        self._prompts[identifier] = {
            "future": completion, "acknowledged": False, "yielded": False,
        }
        try:
            await self._send({"type": "prompt", "id": identifier, "message": message})
            await response
            await completion
        finally:
            self._pending.pop(identifier, None)
            self._prompts.pop(identifier, None)
            for future in (response, completion):
                if not future.done():
                    future.cancel()

    async def abort(self) -> None:
        await self.request("abort")

    def _encode(self, frame: dict[str, Any]) -> bytes:
        try:
            raw = (json.dumps(frame, ensure_ascii=False, separators=(",", ":"),
                              allow_nan=False) + "\n").encode("utf-8")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise RpcError("OMP_RPC_REQUEST_INVALID") from None
        if len(raw) > self._frame_limit:
            raise RpcError("OMP_RPC_FRAME_TOO_LARGE")
        return raw

    async def _send(self, frame: dict[str, Any]) -> None:
        raw = self._encode(frame)
        async with self._write_lock:
            if self._failure:
                raise RpcError(self._failure)
            if self._closing:
                raise RpcError("OMP_RPC_CLOSED")
            try:
                self.process.stdin.write(raw)
                self._hashes["stdin"].update(raw)
                await self.process.stdin.drain()
            except (OSError, RuntimeError):
                self._fail("OMP_RPC_WRITE_FAILED")
                raise RpcError("OMP_RPC_WRITE_FAILED") from None

    async def _read_stderr(self) -> None:
        try:
            while block := await self.process.stderr.read(65_536):
                # Retain only a digest: stderr may contain credentials and dumps.
                self._hashes["stderr"].update(block)
        except (OSError, RuntimeError):
            self._fail("OMP_RPC_READ_FAILED")

    async def _read_stdout(self) -> None:
        buffer = bytearray()
        try:
            while block := await self.process.stdout.read(65_536):
                self._hashes["stdout"].update(block)
                buffer.extend(block)
                while (end := buffer.find(b"\n")) >= 0:
                    if end + 1 > self._frame_limit:
                        raise RpcError("OMP_RPC_FRAME_TOO_LARGE")
                    raw = bytes(buffer[:end])
                    del buffer[:end + 1]
                    self._physical_frame(_decode(raw))
                if len(buffer) >= self._frame_limit:
                    raise RpcError("OMP_RPC_FRAME_TOO_LARGE")
            if buffer or self._chunks is not None:
                raise RpcError("OMP_RPC_FRAME_INVALID")
            if not self._closing:
                self._fail("OMP_RPC_CHILD_EXITED")
        except RpcError as error:
            self._fail(error.code)
        except (OSError, RuntimeError, ValueError, TypeError, RecursionError):
            self._fail("OMP_RPC_READ_FAILED")

    def _physical_frame(self, frame: dict[str, Any]) -> None:
        if frame["type"] != "rpc_chunk":
            if self._chunks is not None:
                raise RpcError("OMP_RPC_CHUNK_INVALID")
            self._dispatch(frame)
            return
        if not self._negotiated:
            raise RpcError("OMP_RPC_CHUNK_INVALID")
        chunk_id, index, count, length, data = (
            frame.get(key) for key in ("chunkId", "index", "count", "byteLength", "data")
        )
        if (not isinstance(chunk_id, str) or not chunk_id
                or not _integer(index) or not _integer(count) or not _integer(length)
                or not 0 <= index < count <= length <= self._logical_limit
                or not isinstance(data, str)):
            raise RpcError("OMP_RPC_CHUNK_INVALID")
        if self._chunks is None:
            if index != 0:
                raise RpcError("OMP_RPC_CHUNK_INVALID")
            self._chunks = {"id": chunk_id, "index": 0, "count": count,
                            "length": length, "data": bytearray()}
        chunks = self._chunks
        if (chunk_id != chunks["id"] or index != chunks["index"]
                or count != chunks["count"] or length != chunks["length"]):
            raise RpcError("OMP_RPC_CHUNK_INVALID")
        try:
            decoded = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error):
            raise RpcError("OMP_RPC_CHUNK_INVALID") from None
        if not decoded or base64.b64encode(decoded).decode("ascii") != data:
            raise RpcError("OMP_RPC_CHUNK_INVALID")
        if len(chunks["data"]) + len(decoded) > length:
            raise RpcError("OMP_RPC_CHUNK_INVALID")
        chunks["data"].extend(decoded)
        chunks["index"] += 1
        if chunks["index"] == count:
            if len(chunks["data"]) != length:
                raise RpcError("OMP_RPC_CHUNK_INVALID")
            logical = _decode(bytes(chunks["data"]))
            self._chunks = None
            if logical["type"] in {"rpc_chunk", "ready"}:
                raise RpcError("OMP_RPC_CHUNK_INVALID")
            self._dispatch(logical)

    def _dispatch(self, frame: dict[str, Any]) -> None:
        kind = frame["type"]
        if kind == "ready":
            if self._ready is None or self._ready.done():
                raise RpcError("OMP_RPC_READY_INVALID")
            versions = frame.get("supportedProtocolVersions")
            physical, logical = frame.get("maxFrameBytes"), frame.get("maxReassembledFrameBytes")
            if (frame.get("protocolVersion") != 1 or not isinstance(versions, list)
                    or not any(_integer(version) and version == 2 for version in versions)
                    or not _integer(physical) or not 0 < physical <= MAX_FRAME_BYTES
                    or not _integer(logical)
                    or not physical <= logical <= MAX_REASSEMBLED_FRAME_BYTES):
                raise RpcError("OMP_RPC_PROTOCOL_UNSUPPORTED")
            self._frame_limit, self._logical_limit = physical, logical
            self._ready.set_result(None)
        elif self._ready is None or not self._ready.done():
            raise RpcError("OMP_RPC_READY_INVALID")
        elif kind == "response":
            self._response(frame)
        elif kind == "prompt_result":
            self._prompt_result(frame)
        elif kind == "session_settled":
            for prompt in self._prompts.values():
                if prompt["yielded"] and not prompt["future"].done():
                    prompt["future"].set_result(None)
        elif kind == "host_tool_call":
            identifier, name, arguments = (
                frame.get("id"), frame.get("toolName"), frame.get("arguments")
            )
            if (not isinstance(identifier, str) or not identifier or identifier in self._tools
                    or not isinstance(name, str) or not name or not isinstance(arguments, dict)):
                raise RpcError("OMP_RPC_HOST_TOOL_INVALID")
            task = asyncio.create_task(self._run_tool(identifier, name, arguments))
            self._tools[identifier] = task
            task.add_done_callback(lambda finished: self._tool_done(identifier, finished))
        elif kind == "host_tool_cancel":
            target = frame.get("targetId")
            if not isinstance(target, str) or not target:
                raise RpcError("OMP_RPC_HOST_TOOL_INVALID")
            if task := self._tools.get(target):
                task.cancel()
        elif kind == "message_end":
            self._retain_message(frame.get("message"))
        elif kind == "rpc_frame_error":
            raise RpcError("OMP_RPC_FRAME_TOO_LARGE")

    def _issued(self, identifier: Any) -> bool:
        if not isinstance(identifier, str) or not identifier.startswith("aizim-"):
            return False
        number = identifier.removeprefix("aizim-")
        return (number.isascii() and number.isdecimal() and len(number) <= 20
                and 0 < int(number) <= self._counter and str(int(number)) == number)

    def _response(self, frame: dict[str, Any]) -> None:
        identifier = frame.get("id")
        if not self._issued(identifier) or type(frame.get("success")) is not bool:
            raise RpcError("OMP_RPC_RESPONSE_INVALID")
        pending = self._pending.get(identifier)
        prompt = self._prompts.get(identifier)
        if pending is None:
            return  # A caller may have cancelled its request.
        command, future = pending
        if frame.get("command") != command:
            raise RpcError("OMP_RPC_RESPONSE_INVALID")
        if frame["success"] is False:
            if not future.done():
                future.set_exception(RpcError("OMP_RPC_COMMAND_FAILED"))
            if prompt and not prompt["future"].done():
                prompt["future"].set_exception(RpcError("OMP_RPC_PROMPT_FAILED"))
            return
        if future.done():
            raise RpcError("OMP_RPC_RESPONSE_INVALID")
        data = frame.get("data")
        if command == "negotiate_protocol":
            if not isinstance(data, dict) or data.get("protocolVersion") != 2:
                raise RpcError("OMP_RPC_PROTOCOL_UNSUPPORTED")
            self._negotiated = True
        if prompt:
            prompt["acknowledged"] = True
            if isinstance(data, dict) and data.get("agentInvoked") is False:
                prompt["future"].set_result(None)
        future.set_result(data)

    def _prompt_result(self, frame: dict[str, Any]) -> None:
        identifier = frame.get("id")
        if not self._issued(identifier):
            raise RpcError("OMP_RPC_RESPONSE_INVALID")
        prompt = self._prompts.get(identifier)
        if prompt is None:
            return
        completion = prompt["future"]
        if completion.cancelled() or (completion.done() and completion.exception() is not None):
            # Pre-admission failures may send a legacy error response followed
            # by prompt_result for the same ticket.
            return
        if (not prompt["acknowledged"] or prompt["yielded"] or prompt["future"].done()
                or frame.get("status") not in {"completed", "aborted", "error"}
                or type(frame.get("agentInvoked")) is not bool
                or type(frame.get("sessionSettled")) is not bool):
            raise RpcError("OMP_RPC_RESPONSE_INVALID")
        prompt["yielded"] = True
        status = frame["status"]
        if status != "completed":
            code = "OMP_RPC_PROMPT_ABORTED" if status == "aborted" else "OMP_RPC_PROMPT_FAILED"
            prompt["future"].set_exception(RpcError(code))
        elif frame["sessionSettled"]:
            prompt["future"].set_result(None)

    def _retain_message(self, message: Any) -> None:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return
        usage = message.get("usage")
        keys = {"input": "input_tokens", "cacheRead": "cached_input_tokens",
                "output": "output_tokens"}
        if (not isinstance(usage, dict)
                or any(not _integer(usage.get(key)) or usage[key] < 0 for key in keys)
                or not _integer(usage.get("cacheWrite", 0)) or usage.get("cacheWrite", 0) < 0):
            self._usage_incomplete = True
            self._usage = None
        elif not self._usage_incomplete:
            if self._usage is None:
                self._usage = dict.fromkeys(keys.values(), 0)
            for source, destination in keys.items():
                self._usage[destination] += usage[source]
            # omp reports disjoint uncached, cache-read and cache-write input buckets.
            # Aizim's input_tokens includes the complete prompt; only read hits are cached_input.
            self._usage["input_tokens"] += usage["cacheRead"] + usage.get("cacheWrite", 0)
        size = len(json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("ascii"))
        if size > MAX_MESSAGE_BYTES:
            return
        while self._messages and (len(self._messages) >= MAX_MESSAGES
                                  or self._message_bytes + size > MAX_MESSAGE_BYTES):
            _old, old_size = self._messages.popleft()
            self._message_bytes -= old_size
        self._messages.append((message, size))
        self._message_bytes += size

    async def _run_tool(self, identifier: str, name: str, arguments: dict[str, Any]) -> None:
        try:
            text, is_error = "OMP_HOST_TOOL_UNAVAILABLE", True
            if self.host_tool is not None:
                try:
                    result = await self.host_tool(name, arguments)
                    if result is not None:
                        text, is_error = result
                        if not isinstance(text, str) or type(is_error) is not bool:
                            raise ValueError
                except Exception:
                    text, is_error = "OMP_HOST_TOOL_FAILED", True
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                return
            frame = {"type": "host_tool_result", "id": identifier, "isError": is_error,
                     "result": {"content": [{"type": "text", "text": text}]}}
            try:
                self._encode(frame)
            except RpcError:
                frame["isError"] = True
                frame["result"] = {
                    "content": [{"type": "text", "text": "OMP_HOST_TOOL_RESULT_TOO_LARGE"}]
                }
            await self._send(frame)
        except RpcError as error:
            self._fail(error.code)

    def _tool_done(self, identifier: str, task: asyncio.Task[None]) -> None:
        if self._tools.get(identifier) is task:
            self._tools.pop(identifier, None)
        if not task.cancelled():
            task.exception()

    def _fail(self, code: str) -> None:
        if self._failure is not None:
            return
        self._failure = code
        if self._ready is not None and not self._ready.done():
            self._ready.set_exception(RpcError(code))
        for _command, future in self._pending.values():
            if not future.done():
                future.set_exception(RpcError(code))
        for prompt in self._prompts.values():
            if not prompt["future"].done():
                prompt["future"].set_exception(RpcError(code))
        for task in self._tools.values():
            task.cancel()
        self._chunks = None

    async def close(self) -> None:
        """Drain through stdin EOF, then terminate and kill an uncooperative group.

        Cleanup continues if its caller is cancelled; a later close() awaits the
        same cleanup task. No child output or exception text is exposed.
        """
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    def _signal(self, value: signal.Signals) -> None:
        if self._process_group is not None:
            try:
                os.killpg(self._process_group, value)
                return
            except ProcessLookupError:
                return
            except OSError:
                pass
        if self.process.returncode is None:
            try:
                if value == signal.SIGKILL:
                    self.process.kill()
                else:
                    self.process.terminate()
            except ProcessLookupError:
                pass

    async def _close(self) -> None:
        self._closing = True
        self._fail("OMP_RPC_CLOSED")
        if self._start_task is not None and not self._start_task.done():
            self._start_task.cancel()
        with suppress(OSError, RuntimeError):
            self.process.stdin.close()
        waiter = asyncio.create_task(self.process.wait())
        try:
            try:
                await asyncio.wait_for(asyncio.shield(waiter), CLOSE_GRACE_SECONDS)
            except TimeoutError:
                self._signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(asyncio.shield(waiter), CLOSE_GRACE_SECONDS)
                except TimeoutError:
                    self._signal(signal.SIGKILL)
                    await waiter
            # A child can exit while descendants retain its pipes. Kill the
            # isolated group as well, then bound the time spent draining EOF.
            self._signal(signal.SIGTERM)
            readers = [task for task in (self._reader, self._stderr_reader) if task is not None]
            if readers:
                _done, pending = await asyncio.wait(readers, timeout=CLOSE_GRACE_SECONDS)
                if pending:
                    self._signal(signal.SIGKILL)
                    for task in pending:
                        task.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
            if self._process_group is not None:
                self._signal(signal.SIGKILL)
        finally:
            for task in self._tools.values():
                task.cancel()
            await asyncio.gather(*list(self._tools.values()), return_exceptions=True)
            if self._start_task is not None:
                await asyncio.gather(self._start_task, return_exceptions=True)
