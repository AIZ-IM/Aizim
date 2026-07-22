from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Final

from aizim.domain import compute_base_epoch, sha256_bytes, sha256_json

from .document_io import (
    DocumentIoError,
    create_relative,
    ensure_tree,
    mode_relative,
    open_root,
    read_relative,
)

_SMOKE_FILES: Final = (
    PurePosixPath("lean-toolchain"),
    PurePosixPath("lakefile.toml"),
    PurePosixPath("AizimSmoke.lean"),
    PurePosixPath("AizimSmoke/Base.lean"),
)


def smoke_base_epoch(smoke_root: Path) -> str:
    descriptor = open_root(smoke_root)
    try:
        bodies = {relative: read_relative(descriptor, relative) for relative in _SMOKE_FILES}
    finally:
        os.close(descriptor)
    environment = sha256_json(
        {
            "lakefile": sha256_bytes(bodies[PurePosixPath("lakefile.toml")]),
            "lean_toolchain": sha256_bytes(bodies[PurePosixPath("lean-toolchain")]),
            "external_packages": (),
            "imports": ("Std", "AizimSmoke"),
        }
    )
    modules = {
        "AizimSmoke": sha256_bytes(bodies[PurePosixPath("AizimSmoke.lean")]),
        "AizimSmoke.Base": sha256_bytes(bodies[PurePosixPath("AizimSmoke/Base.lean")]),
    }
    descriptor = open_root(smoke_root)
    try:
        if any(read_relative(descriptor, path) != body for path, body in bodies.items()):
            raise DocumentIoError("SMOKE_SOURCE_CHANGED")
    finally:
        os.close(descriptor)
    return compute_base_epoch(environment, modules)


def materialize_smoke_project(project_root: Path, run_id: str, smoke_root: Path) -> Path:
    project_fd = open_root(project_root)
    source_fd = open_root(smoke_root)
    run_project = project_root / ".aizim" / "run" / run_id / "lean-project"
    try:
        destination_fd = ensure_tree(project_fd, (".aizim", "run", run_id, "lean-project"))
        try:
            for relative in _SMOKE_FILES:
                body = read_relative(source_fd, relative)
                try:
                    existing = read_relative(destination_fd, relative)
                except DocumentIoError:
                    create_relative(destination_fd, relative, body, mode=0o444)
                else:
                    if existing != body or mode_relative(destination_fd, relative) != 0o444:
                        raise DocumentIoError("RUN_PROJECT_MISMATCH")
                if read_relative(source_fd, relative) != body:
                    raise DocumentIoError("SMOKE_SOURCE_CHANGED")
        finally:
            os.close(destination_fd)
    finally:
        os.close(source_fd)
        os.close(project_fd)
    return run_project
