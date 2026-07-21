from __future__ import annotations

import ast
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[2]
STORE_PATH = REPOSITORY_ROOT / "src" / "aizim" / "state" / "store.py"
SERVICE_PATH = REPOSITORY_ROOT / "src" / "aizim" / "state" / "service.py"
AGENT_ROOT = REPOSITORY_ROOT / "src" / "aizim" / "agents"
MACOS_SANDBOX_PATH = AGENT_ROOT / "macos_sandbox.py"


def _python_sources() -> tuple[Path, ...]:
    roots = tuple(
        path
        for name in ("src", "tests", "scripts")
        if (path := REPOSITORY_ROOT / name).is_dir()
    )
    return tuple(sorted(source for root in roots for source in root.rglob("*.py")))


def _imports_sqlite(node: ast.AST) -> bool:
    direct_import = isinstance(node, ast.Import) and any(
        alias.name == "sqlite3" for alias in node.names
    )
    from_import = isinstance(node, ast.ImportFrom) and node.module == "sqlite3"
    return direct_import or from_import


def _event_store_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    direct_call = isinstance(node.func, ast.Name) and node.func.id == "_EventStore"
    attribute_call = (
        isinstance(node.func, ast.Attribute) and node.func.attr == "_EventStore"
    )
    return direct_call or attribute_call


def _launches_process(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and (node.func.value.id, node.func.attr)
        in {
            ("asyncio", "create_subprocess_exec"),
            ("asyncio", "create_subprocess_shell"),
            ("os", "popen"),
            ("os", "system"),
            ("subprocess", "Popen"),
            ("subprocess", "call"),
            ("subprocess", "run"),
        }
    )


def test_only_store_module_imports_sqlite3() -> None:
    violations: list[str] = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(), filename=str(path))
        if path != STORE_PATH and any(_imports_sqlite(node) for node in ast.walk(tree)):
            violations.append(str(path.relative_to(REPOSITORY_ROOT)))

    assert violations == []


def test_only_state_service_instantiates_private_event_store() -> None:
    violations: list[str] = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(), filename=str(path))
        calls = tuple(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _event_store_call(node)
        )
        if path != SERVICE_PATH:
            violations.extend(
                f"{path.relative_to(REPOSITORY_ROOT)}:{call.lineno}" for call in calls
            )
            continue
        service_classes = (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "StateService"
        )
        allowed = {
            id(node)
            for service_class in service_classes
            for node in ast.walk(service_class)
        }
        violations.extend(
            f"{path.relative_to(REPOSITORY_ROOT)}:{call.lineno}"
            for call in calls
            if id(call) not in allowed
        )

    assert violations == []


def test_task_three_authored_files_stay_within_pure_loc_limit() -> None:
    paths = (
        *(REPOSITORY_ROOT / "src" / "aizim" / "state").glob("*.py"),
        REPOSITORY_ROOT / "tests" / "unit" / "test_event_store.py",
        REPOSITORY_ROOT / "tests" / "unit" / "test_event_replay.py",
        REPOSITORY_ROOT / "tests" / "unit" / "test_event_contract.py",
        REPOSITORY_ROOT / "tests" / "integration" / "test_state_service_rpc.py",
        REPOSITORY_ROOT / "tests" / "integration" / "test_state_service_lifecycle.py",
        Path(__file__),
    )
    oversized: list[str] = []
    for path in paths:
        pure_lines = sum(
            bool(line.strip()) and not line.lstrip().startswith("#")
            for line in path.read_text().splitlines()
        )
        if pure_lines > 250:
            oversized.append(f"{path.relative_to(REPOSITORY_ROOT)}:{pure_lines}")

    assert oversized == []


def test_task_four_authored_files_stay_within_pure_loc_limit() -> None:
    paths = (
        *(REPOSITORY_ROOT / "src" / "aizim" / "gateway").glob("*.py"),
        REPOSITORY_ROOT / "src" / "aizim" / "domain" / "model.py",
        REPOSITORY_ROOT / "tests" / "unit" / "test_capability_matrix.py",
        REPOSITORY_ROOT / "tests" / "integration" / "test_gateway_authorization.py",
        REPOSITORY_ROOT / "tests" / "integration" / "test_gateway_session_broker.py",
        REPOSITORY_ROOT / "tests" / "security" / "test_gateway_attacks.py",
        REPOSITORY_ROOT / "tests" / "security" / "test_gateway_session_attacks.py",
    )
    oversized = []
    for path in paths:
        pure_lines = sum(
            bool(line.strip()) and not line.lstrip().startswith("#")
            for line in path.read_text().splitlines()
        )
        if pure_lines > 250:
            oversized.append(f"{path.relative_to(REPOSITORY_ROOT)}:{pure_lines}")
    assert oversized == []


def test_task_five_authored_files_stay_within_pure_loc_limit() -> None:
    paths = (
        *(REPOSITORY_ROOT / "src" / "aizim" / "runtime").glob("*.py"),
        *(REPOSITORY_ROOT / "src" / "aizim" / "cli").glob("*.py"),
        REPOSITORY_ROOT / "src" / "aizim" / "config" / "model.py",
        REPOSITORY_ROOT / "src" / "aizim" / "config" / "loader.py",
        REPOSITORY_ROOT / "tests" / "unit" / "test_project_layout.py",
        REPOSITORY_ROOT / "tests" / "integration" / "test_cli_init.py",
        REPOSITORY_ROOT / "tests" / "integration" / "test_cli_doctor.py",
        REPOSITORY_ROOT / "tests" / "integration" / "test_cli_status.py",
    )
    oversized = []
    for path in paths:
        pure_lines = sum(
            bool(line.strip()) and not line.lstrip().startswith("#")
            for line in path.read_text().splitlines()
        )
        if pure_lines > 250:
            oversized.append(f"{path.relative_to(REPOSITORY_ROOT)}:{pure_lines}")
    assert oversized == []


def test_only_macos_sandbox_launches_task_six_processes() -> None:
    violations: list[str] = []
    for path in AGENT_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        if path != MACOS_SANDBOX_PATH:
            violations.extend(
                f"{path.relative_to(REPOSITORY_ROOT)}:{node.lineno}"
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and _launches_process(node)
            )
    assert violations == []


def test_task_six_authored_files_stay_within_pure_loc_limit() -> None:
    paths = (
        *AGENT_ROOT.glob("*.py"),
        REPOSITORY_ROOT / "tests" / "unit" / "test_workspace_view.py",
        REPOSITORY_ROOT / "tests" / "unit" / "test_sandbox_profile.py",
        REPOSITORY_ROOT / "tests" / "security" / "test_macos_sandbox.py",
        Path(__file__),
    )
    oversized = []
    for path in paths:
        pure_lines = sum(
            bool(line.strip()) and not line.lstrip().startswith("#")
            for line in path.read_text().splitlines()
        )
        if pure_lines > 250:
            oversized.append(f"{path.relative_to(REPOSITORY_ROOT)}:{pure_lines}")
    assert oversized == []
