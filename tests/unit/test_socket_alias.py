from __future__ import annotations

from pathlib import Path

import pytest

from aizim.gateway import socket_alias
from aizim.gateway.socket_alias import ProjectSocketAlias


def test_linux_socket_alias_uses_the_conventional_short_temporary_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(socket_alias.sys, "platform", "linux")

    alias = ProjectSocketAlias(project)
    try:
        assert alias.socket_path.is_relative_to(Path("/tmp"))
    finally:
        alias.close()


