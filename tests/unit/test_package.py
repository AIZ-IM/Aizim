from __future__ import annotations

import importlib.metadata
import subprocess
import sys

import aizim


def test_version_matches_distribution() -> None:
    assert aizim.__version__ == "0.1.0"
    assert importlib.metadata.version("aizim") == aizim.__version__


def test_module_entrypoint_reports_version() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "aizim", "--version"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "aizim 0.1.0"


def test_formal_bridge_dependencies_are_direct_pins() -> None:
    requirements = set(importlib.metadata.requires("aizim") or ())
    assert "lean-lsp-mcp==0.28.1" in requirements
    assert "leanclient==0.12.1" in requirements
    assert 'mcp[cli]==1.28.1' in requirements
