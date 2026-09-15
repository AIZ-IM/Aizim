from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path

CLAUDE_DOMAINS = ("api.anthropic.com", "claude.ai", "auth.anthropic.com")


def prepare_auth(provider: str, source: Mapping[str, str], scratch: Path) -> tuple[str, Path]:
    home = Path(source.get("HOME", str(scratch)))
    if provider == "codex":
        key = "CODEX_HOME"
        original = Path(source.get(key, str(home / ".codex"))) / "auth.json"
        leaf = "auth.json"
    else:
        key = "CLAUDE_CONFIG_DIR"
        original = Path(source.get(key, str(home / ".claude"))) / ".credentials.json"
        leaf = ".credentials.json"
    target = scratch / f"{provider}-auth"
    target.mkdir(mode=0o700)
    (scratch / "tmp").mkdir(mode=0o700)
    (scratch / "client-tmp").mkdir(mode=0o700)
    try:
        descriptor = os.open(original, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return key, target
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > 1024 * 1024
        ):
            raise ValueError("PROVIDER_AUTH_FILE_INVALID")
        body = os.read(descriptor, 1024 * 1024 + 1)
        if len(body) != metadata.st_size:
            raise ValueError("PROVIDER_AUTH_FILE_CHANGED")
        destination = os.open(target / leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(destination, "wb") as stream:
            stream.write(body)
    finally:
        os.close(descriptor)
    return key, target


def codex_tool_policy(view: Path, scratch: Path, project: Path, runtime: Path) -> tuple[str, ...]:
    filesystem = {
        ":minimal": "read",
        str(view): "read",
        str(scratch): "write",
        str(scratch / "codex-auth"): "deny",
        str(runtime): "read",
        str(project): "deny",
    }
    entries = ",".join(
        f"{json.dumps(path)}={json.dumps(access)}" for path, access in filesystem.items()
    )
    return (
        "-c",
        'approval_policy="never"',
        "-c",
        'web_search="disabled"',
        "-c",
        'default_permissions="aizim-controller-tools"',
        "-c",
        f"permissions.aizim-controller-tools={{filesystem={{{entries}}},network={{enabled=false}}}}",
        "-c",
        "features.network_proxy=false",
    )
