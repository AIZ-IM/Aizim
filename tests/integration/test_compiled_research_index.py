from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from aizim.cli.init_command import run_init
from aizim.research.compiled_index import build_compiler_index
from aizim.research.records import ResearchError
from aizim.research.search import lean_search


@pytest.mark.lean_integration
@pytest.mark.parametrize("src_dir", (".", "src"))
def test_compiler_index_extracts_real_types_and_rejects_stale_builds(
    tmp_path: Path, src_dir: str
) -> None:
    fixture = Path(__file__).parents[1] / "fixtures/research_lean"
    project = Path(shutil.copytree(fixture, tmp_path / "compiler-index"))
    if src_dir != ".":
        (project / src_dir).mkdir()
        (project / "ResearchLab.lean").rename(project / src_dir / "ResearchLab.lean")
        config = project / "lakefile.toml"
        config.write_text(
            config.read_text().replace(
                'name = "ResearchLab"', 'name = "ResearchLab"\nsrcDir = "src"'
            )
        )
    lake_command = shutil.which("lake")
    assert lake_command is not None, "Put the pinned Lean toolchain on PATH"
    lake = Path(lake_command)
    assert run_init(project) == 0
    subprocess.run([str(lake), "build"], cwd=project, check=True, capture_output=True)
    (project / "Extra.lean").write_text(
        "namespace Extra\ntheorem additional : True := trivial\nend Extra\n"
    )
    metadata = build_compiler_index(project, ["ResearchLab"], lake=lake)
    assert metadata["origin"] == "compiler"
    results = lean_search(project, "ResearchLab.addition_identity", mode="name")
    assert len(results) == 1
    entry = results[0]
    assert entry["origin"] == "compiler"
    assert entry["full_name"] == "ResearchLab.addition_identity"
    assert "Nat" in str(entry["signature"])
    assert "natural number" in str(entry["docstring"])
    assert entry["project_local"] is True
    assert lean_search(
        project, "ResearchLab.addition_identity", mode="name", include_dependencies=False
    )
    assert lean_search(project, "Nat.add_zero", mode="name")
    assert not lean_search(project, "Nat.add_zero", mode="name", include_dependencies=False)
    assert lean_search(project, "Extra.additional", mode="name")[0]["origin"] == "source"
    source = project / src_dir / "ResearchLab.lean"
    source.write_text(source.read_text().replace("addition_identity", "renamed_identity"))
    assert not lean_search(project, "ResearchLab.addition_identity", mode="name")
    assert lean_search(project, "renamed_identity", mode="name")[0]["origin"] == "source"
    with pytest.raises(ResearchError, match="DECLARATION_INDEX_FAILED"):
        build_compiler_index(project, ["ResearchLab"], lake=lake)
