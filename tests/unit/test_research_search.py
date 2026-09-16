from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from aizim.cli.init_command import run_init
from aizim.research import declaration_index
from aizim.research.compiled_index import build_compiler_index
from aizim.research.declaration_index import source_index
from aizim.research.records import ResearchError
from aizim.research.search import lean_search, ranked

FIXTURE = Path(__file__).parents[1] / "fixtures/research_lean"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = Path(shutil.copytree(FIXTURE, tmp_path / "project"))
    assert run_init(root) == 0
    return root


def test_source_discovery_preserves_names_scopes_docs_and_multiline_types(project: Path) -> None:
    (project / "ResearchLab.lean").write_text("""import Std
/- theorem fake : False := by sorry
   /- nested comment -/
-/
namespace Outer
section vars
namespace Inner
/-- Cancelling addition on the right. -/
@[simp]
theorem add_cancel
    (n : Nat)
    (m : Nat := 0) : n + m = n + m := by rfl
private theorem hidden : True := trivial
end Inner
end vars
def description := "theorem forged : False := by sorry"
end Outer
theorem outside : True := trivial
""")
    docs = source_index(project).documents
    names = {entry["full_name"] for entry in docs}
    assert names == {"Outer.Inner.add_cancel", "Outer.description", "outside"}
    result = lean_search(project, "Outer.Inner.add_cancel", mode="name")[0]
    assert result["docstring"] == "Cancelling addition on the right."
    assert "(m : Nat := 0) : n + m = n + m" in str(result["signature"])
    assert "by rfl" not in str(result["signature"])
    assert result["line"] == 9
    assert result["origin"] == "source"


def test_index_reuses_files_across_processes_and_invalidates_changed_added_deleted_files(
    project: Path,
) -> None:
    first = source_index(project)
    assert first.parsed_files == 1
    assert source_index(project).parsed_files == 0
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "aizim",
            "research",
            "index",
            "--project",
            str(project),
            "--project-only",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout)["parsed_files"] == 0
    cache = project / ".aizim/search/source-v1-0.json"
    assert stat.S_IMODE(cache.stat().st_mode) == 0o600
    path = project / "ResearchLab.lean"
    before = path.stat()
    path.write_text("theorem changed_name : True := trivial\n")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    second = source_index(project)
    assert second.parsed_files == 1 and second.fingerprint != first.fingerprint
    assert not lean_search(project, "addition_identity", mode="name")
    extra = project / "Extra.lean"
    extra.write_text("theorem extra_target : True := trivial\n")
    assert source_index(project).parsed_files == 1
    assert lean_search(project, "extra_target", mode="name")
    extra.unlink()
    assert not lean_search(project, "extra_target", mode="name")
    unchanged = source_index(project).fingerprint
    (project / "lake-manifest.json").write_text('{"packages": []}')
    assert source_index(project).fingerprint != unchanged


def test_corrupted_disk_cache_recovers_and_symlinked_sources_are_excluded(
    project: Path, tmp_path: Path
) -> None:
    source_index(project)
    cache = project / ".aizim/search/source-v1-0.json"
    cache.write_text('{"version":1,"files":{"broken":null}}')
    declaration_index._CACHES.clear()
    assert source_index(project).parsed_files == 1
    outside = tmp_path / "outside.lean"
    outside.write_text("theorem must_not_be_indexed : True := trivial\n")
    (project / "Link.lean").symlink_to(outside)
    assert not lean_search(project, "must_not_be_indexed", mode="name")


def test_name_tokenization_and_ranking_do_not_match_metadata_noise(project: Path) -> None:
    (project / "ResearchLab.lean").write_text("""namespace Algebra
theorem additiveIdentity : True := trivial
theorem add_zero : True := trivial
theorem contains_underscore : True := trivial
end Algebra
""")
    assert lean_search(project, "additive identity")[0]["full_name"] == "Algebra.additiveIdentity"
    assert lean_search(project, "Algebra.add_zero")[0]["full_name"] == "Algebra.add_zero"
    assert not lean_search(project, "containsXunderscore", mode="type")
    assert not lean_search(project, "/irrelevant/path/to/project")
    assert ranked("", [], 10) == []
    with pytest.raises(ResearchError):
        ranked("test", [], True)


def test_project_filter_excludes_dependency_records(project: Path) -> None:
    dep = project / ".lake/packages/another"
    dep.mkdir(parents=True)
    (dep / "lakefile.toml").write_text('name = "another"\n')
    (dep / "Other.lean").write_text(
        "namespace Other\ntheorem only_dependency : True := trivial\nend Other\n"
    )
    assert lean_search(project, "only_dependency", mode="name")
    assert not lean_search(project, "only_dependency", mode="name", include_dependencies=False)


def test_compiler_index_rejects_invalid_modules_before_starting_a_process(project: Path) -> None:
    for modules in ([], ["Std; echo bad"], ["../Outside"]):
        with pytest.raises(ResearchError, match="INDEX_REQUIRES_VALID_MODULES"):
            build_compiler_index(project, modules)
