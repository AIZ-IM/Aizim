from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

FIXTURE = Path(__file__).parents[1] / "fixtures" / "minimal_lean"


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "aizim", *args], check=False, capture_output=True, text=True
    )


def copy_project(tmp_path: Path) -> Path:
    return Path(shutil.copytree(FIXTURE, tmp_path / "lean-project"))


def outside_snapshot(root: Path) -> dict[str, tuple[str, bytes]]:
    snapshot: dict[str, tuple[str, bytes]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if relative.parts[0] == ".aizim":
            continue
        kind = "directory" if path.is_dir() else "file"
        body = b"" if path.is_dir() else path.read_bytes()
        snapshot[relative.as_posix()] = (kind, hashlib.sha256(body).digest())
    return snapshot


def test_init_creates_only_private_aizim_state_and_is_idempotent(tmp_path: Path) -> None:
    root = copy_project(tmp_path)
    before = outside_snapshot(root)

    first = run_cli("init", str(root))
    second = run_cli("init", str(root))

    assert first.returncode == second.returncode == 0
    assert outside_snapshot(root) == before
    owned = root / ".aizim"
    for relative in ("", "run", "artifacts", "promoted", "documents"):
        assert stat.S_IMODE((owned / relative).stat().st_mode) == 0o700
    for relative in ("config.toml", "state.sqlite3"):
        assert stat.S_IMODE((owned / relative).stat().st_mode) == 0o600


def test_init_conflicting_config_is_exit_two_and_does_not_rewrite_state(
    tmp_path: Path,
) -> None:
    root = copy_project(tmp_path)
    assert run_cli("init", str(root)).returncode == 0
    config = root / ".aizim" / "config.toml"
    database = root / ".aizim" / "state.sqlite3"
    config.write_text("conflicting = true\n")
    config.chmod(0o644)
    before = (
        config.read_bytes(),
        stat.S_IMODE(config.stat().st_mode),
        database.read_bytes(),
    )

    result = run_cli("init", str(root))

    assert result.returncode == 2
    assert (
        config.read_bytes(),
        stat.S_IMODE(config.stat().st_mode),
        database.read_bytes(),
    ) == before


def test_init_rejects_missing_lean_project_markers_without_side_effects(
    tmp_path: Path,
) -> None:
    root = tmp_path / "not-lean"
    root.mkdir()

    result = run_cli("init", str(root))

    assert result.returncode == 2
    assert not (root / ".aizim").exists()


def test_init_reports_corrupt_state_without_a_traceback(tmp_path: Path) -> None:
    root = copy_project(tmp_path)
    assert run_cli("init", str(root)).returncode == 0
    (root / ".aizim" / "state.sqlite3").write_bytes(b"not a database")

    result = run_cli("init", str(root))

    assert result.returncode == 2
    assert result.stderr == "aizim init: initialization failed\n"


def test_init_rejects_hard_linked_database(tmp_path: Path) -> None:
    root = copy_project(tmp_path)
    assert run_cli("init", str(root)).returncode == 0
    database = root / ".aizim" / "state.sqlite3"
    outside = tmp_path / "outside.sqlite3"
    database.replace(outside)
    os.link(outside, database)

    result = run_cli("init", str(root))

    assert result.returncode == 2
    assert database.samefile(outside)


def test_init_rejects_symlinked_state_lock_without_touching_target(
    tmp_path: Path,
) -> None:
    root = copy_project(tmp_path)
    assert run_cli("init", str(root)).returncode == 0
    lock = root / ".aizim" / "run" / "state.lock"
    outside = tmp_path / "outside.lock"
    outside.write_text("outside\n")
    outside.chmod(0o644)
    lock.unlink()
    lock.symlink_to(outside)

    result = run_cli("init", str(root))

    assert result.returncode == 2
    assert outside.read_text() == "outside\n"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644
