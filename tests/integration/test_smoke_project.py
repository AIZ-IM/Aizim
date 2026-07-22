from __future__ import annotations

import json
import shutil
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest

from aizim.lean.document_io import DocumentIoError
from aizim.lean.project import materialize_smoke_project, smoke_base_epoch

SMOKE_ROOT = Path(__file__).parents[2] / "examples" / "smoke_lean"


def test_smoke_project_is_exact_and_dependency_free() -> None:
    assert (SMOKE_ROOT / "lean-toolchain").read_bytes() == (b"leanprover/lean4:v4.32.0\n")
    assert (SMOKE_ROOT / "lakefile.toml").read_bytes() == (
        b'name = "aizimSmoke"\n'
        b'version = "0.1.0"\n'
        b'defaultTargets = ["AizimSmoke"]\n\n'
        b"[[lean_lib]]\n"
        b'name = "AizimSmoke"\n'
    )
    assert (SMOKE_ROOT / "AizimSmoke.lean").read_bytes() == (b"import AizimSmoke.Base\n")
    assert (SMOKE_ROOT / "AizimSmoke" / "Base.lean").read_bytes() == (
        b"import Std\n\n"
        b"namespace AizimSmoke\n\n"
        b"theorem base_add_zero (n : Nat) : n + 0 = n := Nat.add_zero n\n\n"
        b"end AizimSmoke\n"
    )

    lakefile = tomllib.loads((SMOKE_ROOT / "lakefile.toml").read_text())
    assert lakefile == {
        "name": "aizimSmoke",
        "version": "0.1.0",
        "defaultTargets": ["AizimSmoke"],
        "lean_lib": [{"name": "AizimSmoke"}],
    }
    assert "require" not in lakefile
    manifest = SMOKE_ROOT / "lake-manifest.json"
    assert not manifest.exists() or json.loads(manifest.read_text())["packages"] == []

    imports = {
        line.removeprefix("import ").strip()
        for source in SMOKE_ROOT.rglob("*.lean")
        for line in source.read_text().splitlines()
        if line.startswith("import ")
    }
    assert imports
    assert all(name == "Std" or name.startswith("AizimSmoke.") for name in imports)


def test_smoke_base_epoch_excludes_lake_runtime_state(tmp_path: Path) -> None:
    copied = tmp_path / "smoke"
    shutil.copytree(SMOKE_ROOT, copied)
    before = smoke_base_epoch(copied)
    (copied / ".lake").mkdir()
    (copied / ".lake" / "runtime-state").write_text("ignored")
    assert smoke_base_epoch(copied) == before


def test_run_project_copy_is_fixed_private_and_excludes_lake(tmp_path: Path) -> None:
    source = tmp_path / "source"
    shutil.copytree(SMOKE_ROOT, source)
    (source / ".lake").mkdir()
    (source / ".lake" / "cache").write_text("must not copy")
    project = tmp_path / "project"
    project.mkdir()

    run_project = materialize_smoke_project(project, "run-1", source)

    assert not (run_project / ".lake").exists()
    expected = {
        "lean-toolchain",
        "lakefile.toml",
        "AizimSmoke.lean",
        "AizimSmoke/Base.lean",
    }
    actual = {
        path.relative_to(run_project).as_posix()
        for path in run_project.rglob("*")
        if path.is_file()
    }
    assert actual == expected
    assert all(
        stat.S_IMODE((run_project / relative).stat().st_mode) == 0o444 for relative in expected
    )


def test_smoke_source_symlink_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    shutil.copytree(SMOKE_ROOT, source)
    outside = tmp_path / "outside.lean"
    outside.write_text("import Std\n")
    base = source / "AizimSmoke/Base.lean"
    base.unlink()
    base.symlink_to(outside)

    with pytest.raises(DocumentIoError, match="DOCUMENT_PATH_DENIED"):
        smoke_base_epoch(source)


@pytest.mark.lean_integration
def test_smoke_project_builds_without_external_packages(tmp_path: Path) -> None:
    copied = tmp_path / "smoke"
    shutil.copytree(SMOKE_ROOT, copied)
    completed = subprocess.run(
        ["lake", "build"], cwd=copied, text=True, capture_output=True, check=False
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    combined = (completed.stdout + completed.stderr).lower()
    assert all(
        forbidden not in combined
        for forbidden in ("mathlib", "batteries", "loogle", "repl", "download")
    )
    manifest = copied / "lake-manifest.json"
    assert not manifest.exists() or json.loads(manifest.read_text())["packages"] == []
