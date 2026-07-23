from __future__ import annotations

from pathlib import Path

from aizim.agents.backend import AgentRequest
from aizim.agents.codex_backend import build_codex_launch_spec
from aizim.agents.macos_profile import compile_macos_profile
from aizim.agents.sandbox import SandboxRequest
from aizim.domain import AgentRole


def test_codex_launch_accepts_a_short_alias_to_the_canonical_project(tmp_path: Path) -> None:
    view, scratch, canonical = (
        tmp_path / "aizim-view",
        tmp_path / "aizim-scratch",
        tmp_path / "canonical-project",
    )
    for root in (view, scratch, canonical):
        root.mkdir()
    alias_root = tmp_path / "socket-alias"
    alias_root.mkdir()
    alias = alias_root / "project"
    alias.symlink_to(canonical, target_is_directory=True)
    request = AgentRequest(
        "run-7",
        "worker-7",
        AgentRole.FORMALIZER,
        "fixture",
        None,
        view,
        scratch,
        "session-7",
        alias / ".aizim" / "run" / "gateway.sock",
        30.0,
    )
    developer_root = view.parent / "developer-root"
    sandbox = compile_macos_profile(
        Path("/opt/aizim/bin/codex"),
        SandboxRequest(canonical, view, scratch, ("/usr/bin/true",), {"PATH": "/usr/bin"}),
        developer_root,
    )

    launch = build_codex_launch_spec(
        request,
        sandbox,
        Path("/opt/aizim/bin/aizim-gateway-sidecar"),
    )

    assert str(request.gateway_broker_socket) in "\n".join(launch.argv)
