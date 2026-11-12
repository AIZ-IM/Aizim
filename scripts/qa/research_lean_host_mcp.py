"""Exercise isolated host/LSP stdio MCP servers in a disposable Lean project.

Run after the isolated installation documented in
  docs/designs/2026-11-08-lean-host-mcp-study.md
Example:
  python scripts/qa/research_lean_host_mcp.py --output /tmp/lean-host-evidence.json
Only the temporary project is edited; server requests and responses are preserved.
This is a research smoke probe, not a production verification adapter.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

BASE_SOURCE = "namespace Probe\ndef base : Nat := 1\nend Probe\n"
PROBE_SOURCE = """import Probe.Base
namespace Probe
theorem good (n : Nat) : n + 0 = n := by
  simp
theorem sorryProof : True := by sorry
axiom assumed : False
theorem axiomProof : False := assumed
theorem usesBase : base = base := by rfl
end Probe
"""
BAD_SOURCE = """import Probe.Base
namespace Probe
theorem bad : False := by exact True.intro
end Probe
"""


def snapshot(root: Path) -> dict[str, str]:
    """Include build/config bytes, excluding Lake's volatile LSP logs."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix not in {".log", ".lock"}
    }


def changed(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))


class StdioMcp:
    def __init__(self, command: list[str], project: Path, env: dict[str, str], log: Path):
        self.command, self.project, self.env, self.log = command, project, env, log
        self.counter = 0
        self.events: list[dict] = []

    async def __aenter__(self):
        self.stderr = self.log.open("wb")
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=self.project,
            env=self.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=self.stderr,
            limit=8 * 1024 * 1024,
        )
        try:
            self.initialization = await self.request(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "aizim-host-research", "version": "1"},
                },
            )
            await self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self.tools = await self.request("tools/list", {})
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def send(self, frame: dict):
        self.process.stdin.write((json.dumps(frame) + "\n").encode())
        await self.process.stdin.drain()

    async def request(self, method: str, params: dict):
        self.counter += 1
        request_id = self.counter
        await self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        async with asyncio.timeout(180):
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    raise RuntimeError(f"MCP exited during {method}; see {self.log}")
                frame = json.loads(line)
                if frame.get("id") == request_id:
                    return frame
                self.events.append(frame)
                if "id" in frame and "method" in frame:
                    await self.send(
                        {
                            "jsonrpc": "2.0",
                            "id": frame["id"],
                            "error": {"code": -32601, "message": "Unsupported client request"},
                        }
                    )

    async def call(self, name: str, arguments: dict):
        return await self.request("tools/call", {"name": name, "arguments": arguments})

    async def __aexit__(self, *_):
        try:
            self.process.stdin.close()
            await asyncio.wait_for(self.process.wait(), timeout=20)
        except (TimeoutError, BrokenPipeError, ConnectionResetError):
            if self.process.returncode is None:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=5)
                except TimeoutError:
                    self.process.kill()
                    await self.process.wait()
        finally:
            self.stderr.close()


def semantic(frame: dict) -> dict:
    result = frame.get("result", {})
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    for item in result.get("content", []):
        if item.get("type") == "text":
            try:
                return json.loads(item["text"])
            except json.JSONDecodeError:
                pass
    return {}


def command_record(command: list[str], project: Path, env: dict[str, str]) -> dict:
    started = time.monotonic()
    result = subprocess.run(
        command, cwd=project, env=env, text=True, capture_output=True, timeout=180
    )
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


async def study(args, project: Path, report: dict):
    env = os.environ.copy()
    env.update(
        CARGO_HOME=str(args.install_root / "cargo"),
        RUSTUP_HOME=str(args.install_root / "rustup"),
        XDG_DATA_HOME=str(args.install_root / "data"),
        XDG_CACHE_HOME=str(args.install_root / "cache"),
        LEAN_HOST_MCP_CONFIG_DIR=str(args.install_root / "config"),
        LEAN_RS_NUM_THREADS="2",
        LEAN_HOST_MCP_MAX_PROJECTS="1",
    )
    env.pop("LEAN_HOST_MCP_WORKERS_DIR", None)
    env["PATH"] = os.pathsep.join(
        [
            str(args.lean_root / "bin"),
            str(args.install_root / "cargo/bin"),
            str(Path.home() / ".elan/bin"),
            env.get("PATH", ""),
        ]
    )
    report["environment"] = {
        key: env[key]
        for key in [
            "CARGO_HOME",
            "RUSTUP_HOME",
            "XDG_DATA_HOME",
            "LEAN_RS_NUM_THREADS",
            "LEAN_HOST_MCP_MAX_PROJECTS",
        ]
    }
    report["versions"] = {}
    for name, command in {
        "lean": [str(args.lean_root / "bin/lean"), "--version"],
        "rust": [str(args.install_root / "cargo/bin/rustc"), "--version"],
        "host": [str(args.host), "--version"],
        "workers": [str(args.host), "install-worker", "--list"],
    }.items():
        report["versions"][name] = command_record(command, project, env)
    (project / "Probe").mkdir()
    (project / "lean-toolchain").write_text("leanprover/lean4:v4.35.0\n")
    (project / "lakefile.toml").write_text(
        'name = "host_probe"\nversion = "0.1.0"\ndefaultTargets = ["Probe"]\n'
        '\n[[lean_lib]]\nname = "Probe"\n'
    )
    (project / "Probe/Base.lean").write_text(BASE_SOURCE)
    (project / "Probe.lean").write_text(PROBE_SOURCE)
    (project / "Bad.lean").write_text(BAD_SOURCE)
    report["fixtures"] = {
        "Probe/Base.lean": BASE_SOURCE,
        "Probe.lean": PROBE_SOURCE,
        "Bad.lean": BAD_SOURCE,
    }
    report["build_before"] = command_record(
        [str(args.lean_root / "bin/lake"), "build"], project, env
    )
    if report["build_before"]["returncode"]:
        raise RuntimeError("Fixture build failed")
    report["calls"] = []

    async def record(client, label, name, arguments, backend="host"):
        before = snapshot(project)
        started = time.monotonic()
        response = await client.call(name, arguments)
        report["calls"].append(
            {
                "backend": backend,
                "case": label,
                "tool": name,
                "arguments": arguments,
                "response": response,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "changed_project_files": changed(before, snapshot(project)),
            }
        )
        return semantic(response)

    def verify(file, names, allow_sorry=False):
        return {
            "targets": [{"kind": "explicit", "file": file, "declarations": names}],
            "allow_sorry": allow_sorry,
            "report_axioms": True,
        }

    async with StdioMcp(
        [str(args.host), "--lake-root", str(project)],
        project,
        env,
        args.output.with_suffix(".host.stderr.log"),
    ) as host:
        report["host_initialize"], report["host_tools"] = host.initialization, host.tools
        await record(
            host,
            "project_status",
            "lean_status",
            {"kind": "project", "include": ["toolchain", "worker", "artifacts"]},
        )
        await record(
            host,
            "valid_sorry_axioms",
            "lean_verify",
            verify(
                "Probe.lean",
                ["Probe.good", "Probe.sorryProof", "Probe.assumed", "Probe.axiomProof"],
            ),
        )
        await record(
            host, "sorry_allowed", "lean_verify", verify("Probe.lean", ["Probe.sorryProof"], True)
        )
        await record(host, "invalid_declaration", "lean_verify", verify("Bad.lean", ["Probe.bad"]))
        await record(
            host, "missing_declaration", "lean_verify", verify("Probe.lean", ["Probe.absent"])
        )
        await record(
            host,
            "proof_candidates",
            "lean_trial",
            {
                "kind": "proof_step",
                "file": "Probe.lean",
                "declaration": "Probe.good",
                "snippets": ["simp", "exact True.intro", "sorry"],
            },
        )
        await record(
            host,
            "valid_command",
            "lean_trial",
            {"kind": "command", "imports": ["Probe.Base"], "commands": "#eval Probe.base"},
        )
        await record(
            host,
            "invalid_command",
            "lean_trial",
            {
                "kind": "command",
                "imports": ["Init"],
                "commands": "theorem invalid : False := by exact True.intro",
            },
        )
        reference_args = {"kind": "references", "name": "Probe.base", "scope": "project"}
        await record(host, "references_before_edit", "lean_lookup", reference_args)
        await record(host, "invalid_request", "lean_status", {"kind": "invalid"})
        await record(
            host,
            "missing_file",
            "lean_status",
            {"kind": "file_diagnostics", "file": "Missing.lean"},
        )
        await record(
            host,
            "unusable_project",
            "lean_status",
            {"kind": "project", "project": str(project / "absent-project")},
        )
        await record(host, "unadvertised_runtime_tool", "lean_runtime", {"action": "status"})
        (project / "Probe/Base.lean").write_text(BASE_SOURCE.replace(":= 1", ":= 2"))
        (project / "Probe.lean").write_text(PROBE_SOURCE + "\ntheorem fresh : 2 = 2 := by rfl\n")
        report["source_edits"] = {
            "Probe/Base.lean": "base changed from 1 to 2",
            "Probe.lean": "appended theorem fresh",
        }
        report["edited_source_sha256"] = {
            name: hashlib.sha256((project / name).read_bytes()).hexdigest()
            for name in ["Probe.lean", "Probe/Base.lean"]
        }
        await record(
            host, "source_fresh_after_edit", "lean_verify", verify("Probe.lean", ["fresh"])
        )
        await record(host, "references_after_edit", "lean_lookup", reference_args)
        await record(
            host,
            "import_after_edit_before_build",
            "lean_trial",
            {"kind": "command", "imports": ["Probe.Base"], "commands": "#eval Probe.base"},
        )
        await record(
            host,
            "source_overlay_after_edit",
            "lean_trial",
            {"kind": "command", "file": "Probe/Base.lean", "commands": "#eval Probe.base"},
        )
        report["build_after"] = command_record(
            [str(args.lean_root / "bin/lake"), "build"], project, env
        )
        await record(
            host,
            "import_after_build",
            "lean_trial",
            {"kind": "command", "imports": ["Probe.Base"], "commands": "#eval Probe.base"},
        )
        await record(host, "references_after_build", "lean_lookup", reference_args)
        report["host_notifications"] = host.events
    missing_worker_env = env.copy()
    missing_worker_env["LEAN_HOST_MCP_WORKERS_DIR"] = str(project / "missing-workers")
    async with StdioMcp(
        [str(args.host), "--lake-root", str(project)],
        project,
        missing_worker_env,
        args.output.with_suffix(".missing-worker.stderr.log"),
    ) as missing_worker_host:
        await record(
            missing_worker_host,
            "missing_worker",
            "lean_trial",
            {"kind": "command", "imports": ["Init"], "commands": "#check True"},
        )

    if args.lsp.is_file():
        async with StdioMcp(
            [str(args.lsp), "--transport", "stdio", "--lean-project-path", str(project)],
            project,
            env,
            args.output.with_suffix(".lsp.stderr.log"),
        ) as lsp:
            report["lsp_initialize"], report["lsp_tools"] = lsp.initialization, lsp.tools
            names = {item["name"] for item in lsp.tools.get("result", {}).get("tools", [])}
            if "lean_diagnostic_messages" in names:
                await record(
                    lsp,
                    "valid_and_sorry_diagnostics",
                    "lean_diagnostic_messages",
                    {"file_path": str(project / "Probe.lean")},
                    "lsp",
                )
                await record(
                    lsp,
                    "invalid_diagnostics",
                    "lean_diagnostic_messages",
                    {"file_path": str(project / "Bad.lean")},
                    "lsp",
                )
            if "lean_verify" in names:
                for theorem in [
                    "Probe.good",
                    "Probe.sorryProof",
                    "Probe.assumed",
                    "Probe.axiomProof",
                ]:
                    await record(
                        lsp,
                        theorem,
                        "lean_verify",
                        {"file_path": str(project / "Probe.lean"), "theorem_name": theorem},
                        "lsp",
                    )
            report["lsp_notifications"] = lsp.events
    else:
        report["lsp_unavailable"] = str(args.lsp)
    report["read_only_observation"] = {
        "method": "SHA-256 of all project files except .log/.lock before and after each tool call",
        "changes": [
            {"backend": row["backend"], "case": row["case"], "files": row["changed_project_files"]}
            for row in report["calls"]
            if row["changed_project_files"]
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    default_root = Path.home() / ".cache/aizim-lean-host-mcp"
    parser.add_argument("--install-root", type=Path, default=default_root)
    parser.add_argument(
        "--lean-root",
        type=Path,
        default=Path.home() / ".elan/toolchains/leanprover--lean4---v4.35.0",
    )
    parser.add_argument("--host", type=Path)
    parser.add_argument("--lsp", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.install_root = args.install_root.resolve()
    args.lean_root = args.lean_root.resolve()
    args.host = (args.host or args.install_root / "host/bin/lean-host-mcp").resolve()
    args.lsp = (args.lsp or args.install_root / "uv-bin/lean-lsp-mcp").resolve()
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "scope": "temporary Init-only Lake project; no production integration",
    }
    try:
        with tempfile.TemporaryDirectory(prefix="aizim-lean-host-study-") as directory:
            report["temporary_project"] = directory
            asyncio.run(study(args, Path(directory), report))
    except BaseException as exc:
        report["aborted"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(args.output)


if __name__ == "__main__":
    main()
