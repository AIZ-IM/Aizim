from __future__ import annotations

import ast
from pathlib import Path

from aizim.gateway import ROLE_CAPABILITIES, GatewayTool

SOURCE_ROOT = Path(__file__).parents[2] / "src" / "aizim"
TOKEN_SENTINEL = "Task8CapabilitySentinel_8a6e1d9f65b7439d"
SECRET_SENTINEL = "Task8SecretSentinel_163bc21e6a804f57"
PROCESS_CREATORS = {
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
    "concurrent.futures.ProcessPoolExecutor",
    "multiprocessing.Process",
    "os.fork",
    "os.forkpty",
    "os.popen",
    "os.posix_spawn",
    "os.posix_spawnp",
    "os.system",
    "pty.fork",
    "pty.spawn",
    *(f"os.exec{name}" for name in ["l", "le", "lp", "lpe", "v", "ve", "vp", "vpe"]),
    *(f"os.spawn{name}" for name in ["l", "le", "lp", "lpe", "v", "ve", "vp", "vpe"]),
    *(f"subprocess.{name}" for name in ["Popen", "call", "run"]),
    *(f"subprocess.{name}" for name in ["check_call", "check_output"]),
    *(f"subprocess.{name}" for name in ["getoutput", "getstatusoutput"]),
}
PROCESS_OWNERS = {
    "agents/launcher.py",
    "agents/linux_sandbox.py",
    "agents/macos_sandbox.py",
    "agents/probe_execution.py",
    "lean/mcp_client.py",
}
TRUSTED_OPERATIONS = {
    GatewayTool.STATE_APPEND,
    GatewayTool.DOCUMENT_RESOLVE_PATH,
    GatewayTool.LEAN_BUILD,
    GatewayTool.LEAN_VERIFY,
    GatewayTool.PROMOTION_ENQUEUE,
    GatewayTool.PROMOTION_PUBLISH,
    GatewayTool.ENVIRONMENT_APPROVE,
    GatewayTool.CAPABILITY_MINT,
}
FORBIDDEN_AGENT_MODULES = {
    "aizim.knowledge.promotion",
    "aizim.lean.documents",
    "aizim.state.store",
    "sqlite3",
}
SIDECAR_MODULES = {
    "gateway/mcp_tools.py",
    "gateway/sidecar.py",
    "gateway/transport.py",
    "gateway/transport_frames.py",
}
FORBIDDEN_SIDECAR_MODULES = {
    "aizim.agents.workspace_copy",
    "aizim.agents.workspace_view",
    "aizim.cli.init_command",
    "aizim.gateway.session_broker",
    "aizim.knowledge.promotion",
    "aizim.lean.documents",
    "aizim.runtime.layout",
    "aizim.runtime.state_process",
    "aizim.security_gate",
    "aizim.state.service",
    "aizim.state.service_ownership",
    "aizim.state.store",
}
FILESYSTEM_MUTATION_CALLS = {
    "builtins.open",
    "io.open",
    "open",
    "os.chmod",
    "os.chown",
    "os.fchmod",
    "os.fchown",
    "os.fdopen",
    "os.link",
    "os.makedirs",
    "os.mkdir",
    "os.mknod",
    "os.open",
    "os.remove",
    "os.removedirs",
    "os.rename",
    "os.renames",
    "os.replace",
    "os.rmdir",
    "os.symlink",
    "os.truncate",
    "os.unlink",
    "os.write",
    "shutil.copy",
    "shutil.copy2",
    "shutil.copyfile",
    "shutil.copyfileobj",
    "shutil.copymode",
    "shutil.copystat",
    "shutil.copytree",
    "shutil.move",
    "shutil.rmtree",
    "tempfile.NamedTemporaryFile",
    "tempfile.TemporaryFile",
    "tempfile.mkdtemp",
    "tempfile.mkstemp",
}
FILESYSTEM_MUTATION_METHODS = {
    "chmod",
    "hardlink_to",
    "link_to",
    "mkdir",
    "open",
    "rename",
    "replace",
    "rmdir",
    "symlink_to",
    "touch",
    "unlink",
    "write_bytes",
    "write_text",
}


def _sources() -> tuple[Path, ...]:
    return tuple(sorted(SOURCE_ROOT.rglob("*.py")))


def _relative(source: Path) -> str:
    return source.relative_to(SOURCE_ROOT).as_posix()


def _tree(source: Path) -> ast.Module:
    return ast.parse(source.read_text(), filename=str(source))


def _aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                binding = name.asname or name.name.split(".")[0]
                aliases[binding] = name.name if name.asname else binding
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            for name in node.names:
                aliases[name.asname or name.name] = f"{node.module}.{name.name}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        else:
            continue
        if isinstance(target, ast.Name) and value is not None:
            resolved = _qualified(value, aliases)
            if resolved:
                aliases[target.id] = resolved
    return aliases


def _qualified(node: ast.expr, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        owner = _qualified(node.value, aliases)
        return f"{owner}.{node.attr}"
    return ""


def _imports(tree: ast.Module, source: Path) -> tuple[str, ...]:
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(name.name for name in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0:
                resolved = module
            else:
                package = ("aizim", *source.relative_to(SOURCE_ROOT).parent.parts)
                parent = package[: len(package) - node.level + 1]
                resolved = ".".join((*parent, module)).rstrip(".")
            if resolved:
                imported.append(resolved)
            imported.extend(f"{resolved}.{name.name}".lstrip(".") for name in node.names)
    return tuple(imported)


def _is_module(module: str, expected: str) -> bool:
    return module == expected or module.startswith(f"{expected}.")


def test_process_creator_aliases_cover_simple_and_annotated_assignments() -> None:
    tree = ast.parse(
        "import subprocess\nimport concurrent.futures\nimport concurrent.futures as cf\n"
        "spawn = subprocess.run\nchecked: object = subprocess.run\n"
        "pooled = concurrent.futures.ProcessPoolExecutor\naliased = cf.ProcessPoolExecutor\n"
    )

    aliases = _aliases(tree)

    assert aliases["spawn"] == aliases["checked"] == "subprocess.run"
    assert aliases["pooled"] == "concurrent.futures.ProcessPoolExecutor"
    assert aliases["aliased"] == "concurrent.futures.ProcessPoolExecutor"


def test_sqlite_store_and_subprocess_creation_have_single_trusted_owners() -> None:
    sqlite_importers: list[str] = []
    store_constructors: list[str] = []
    process_creators: list[str] = []
    for source in _sources():
        relative = _relative(source)
        tree = _tree(source)
        aliases = _aliases(tree)
        if any(_is_module(module, "sqlite3") for module in _imports(tree, source)):
            sqlite_importers.append(relative)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = _qualified(node.func, aliases)
            if called == "_EventStore" or called.endswith("._EventStore"):
                store_constructors.append(relative)
            if called in PROCESS_CREATORS:
                process_creators.append(relative)

    assert sqlite_importers == ["state/store.py"]
    assert store_constructors == ["state/service.py"]
    assert set(process_creators) <= PROCESS_OWNERS


def test_untrusted_modules_cannot_import_or_mutate_trusted_surfaces() -> None:
    violations: list[str] = []
    for source in _sources():
        relative = _relative(source)
        tree = _tree(source)
        imports = _imports(tree, source)
        if relative.startswith("agents/") and any(
            _is_module(module, forbidden)
            for module in imports
            for forbidden in FORBIDDEN_AGENT_MODULES
        ):
            violations.append(relative)
        if relative in SIDECAR_MODULES:
            if any(
                _is_module(module, forbidden)
                for module in imports
                for forbidden in FORBIDDEN_SIDECAR_MODULES
            ):
                violations.append(relative)
            aliases = _aliases(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                called = _qualified(node.func, aliases)
                if (
                    called in FILESYSTEM_MUTATION_CALLS
                    or called.rsplit(".", 1)[-1] in FILESYSTEM_MUTATION_METHODS
                ):
                    violations.append(relative)
                    break

    assert violations == []


def test_capability_matrix_and_source_literals_exclude_trusted_authority() -> None:
    assert all(
        TRUSTED_OPERATIONS.isdisjoint(operations) for operations in ROLE_CAPABILITIES.values()
    )
    forbidden_literals = (
        "--dangerously-bypass-approvals-and-sandbox",
        "--full-auto",
        "danger-full-access",
        TOKEN_SENTINEL,
        SECRET_SENTINEL,
    )
    violations = {
        _relative(source): literal
        for source in _sources()
        for literal in forbidden_literals
        if literal in source.read_text()
    }
    assert violations == {}
