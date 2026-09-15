from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from aizim.domain import compute_base_epoch, sha256_bytes, sha256_json

from . import source_layout
from .document_io import (
    DocumentIoError,
    create_relative,
    ensure_tree,
    mode_relative,
    open_root,
    read_relative,
    replace_relative,
)

_SMOKE_FILES: Final = (
    PurePosixPath("lean-toolchain"),
    PurePosixPath("lakefile.toml"),
    PurePosixPath("AizimSmoke.lean"),
    PurePosixPath("AizimSmoke/Base.lean"),
)
_MANIFEST: Final = PurePosixPath("AizimSmoke.lean")
_RESEARCH_IMPORT: Final = re.compile(
    rb"^import (AizimSmoke\.Research\.[A-Za-z_][A-Za-z0-9_]*)\n", re.MULTILINE
)
_RUN_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_HASH: Final = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class PublishedModule:
    run_id: str
    module: str
    content_hash: str

    def __post_init__(self) -> None:
        if (
            _RUN_ID.fullmatch(self.run_id) is None
            or re.fullmatch(
                r"(?:AizimSmoke\.Research|AizimResearch)\.[A-Za-z_][A-Za-z0-9_]*", self.module
            )
            is None
            or _HASH.fullmatch(self.content_hash) is None
        ):
            raise DocumentIoError("INVALID_PUBLISHED_MODULE")


def smoke_base_epoch(smoke_root: Path) -> str:
    return project_base_epoch(smoke_root)


def project_base_epoch(project_root: Path, extra_modules: Mapping[str, bytes] | None = None) -> str:
    if not source_layout.is_smoke(project_root):
        return source_layout.base_epoch(project_root, dict(extra_modules or {}))
    descriptor = open_root(project_root)
    try:
        bodies = {relative: read_relative(descriptor, relative) for relative in _SMOKE_FILES}
        manifest = _base_manifest(bodies[_MANIFEST])
        modules = {
            "AizimSmoke": sha256_bytes(manifest),
            "AizimSmoke.Base": sha256_bytes(bodies[PurePosixPath("AizimSmoke/Base.lean")]),
        }
        for name in _research_imports(bodies[_MANIFEST]):
            modules[name] = sha256_bytes(read_relative(descriptor, _module_path(name)))
        for name, source in ({} if extra_modules is None else extra_modules).items():
            if (
                _RESEARCH_IMPORT.fullmatch(f"import {name}\n".encode()) is None
                or type(source) is not bytes
            ):
                raise DocumentIoError("INVALID_PUBLISHED_MODULE")
            digest = sha256_bytes(source)
            if name in modules and modules[name] != digest:
                raise DocumentIoError("PROMOTION_MODULE_MISMATCH")
            modules[name] = digest
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
    return compute_base_epoch(environment, modules)


def materialize_smoke_project(project_root: Path, run_id: str, smoke_root: Path) -> Path:
    if not source_layout.is_smoke(smoke_root):
        return source_layout.materialize(project_root, run_id, smoke_root)
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
                    if not _matches_source(
                        relative, body, existing, mode_relative(destination_fd, relative)
                    ):
                        raise DocumentIoError("RUN_PROJECT_MISMATCH")
                if read_relative(source_fd, relative) != body:
                    raise DocumentIoError("SMOKE_SOURCE_CHANGED")
        finally:
            os.close(destination_fd)
    finally:
        os.close(source_fd)
        os.close(project_fd)
    return run_project


def sync_published_modules(
    project_root: Path, run_project: Path, published: tuple[PublishedModule, ...]
) -> str:
    if not source_layout.is_smoke(run_project):
        return source_layout.sync_modules(project_root, run_project, published)
    by_name = {item.module: item for item in published}
    if len(by_name) != len(published):
        raise DocumentIoError("PROMOTION_MODULE_MISMATCH")
    destination, project = open_root(run_project), open_root(project_root)
    try:
        manifest = read_relative(destination, _MANIFEST)
        imports: list[bytes] = []
        for name, item in sorted(by_name.items()):
            artifact = (
                PurePosixPath(".aizim")
                / "artifacts"
                / item.run_id
                / "promotions"
                / item.content_hash
            )
            source = read_relative(project, artifact)
            if (
                mode_relative(project, artifact) != 0o600
                or sha256_bytes(source) != item.content_hash
            ):
                raise DocumentIoError("PROMOTION_MODULE_MISMATCH")
            target = _module_path(name)
            try:
                existing = read_relative(destination, target)
            except DocumentIoError:
                create_relative(destination, target, source, mode=0o444)
            else:
                if existing != source or mode_relative(destination, target) != 0o444:
                    raise DocumentIoError("PROMOTION_MODULE_MISMATCH")
            imports.append(f"import {name}\n".encode())
        expected = _base_manifest(manifest) + b"".join(imports)
        if manifest != expected:
            replace_relative(destination, _MANIFEST, expected)
    finally:
        os.close(project)
        os.close(destination)
    return project_base_epoch(run_project)


def _base_manifest(manifest: bytes) -> bytes:
    return _RESEARCH_IMPORT.sub(b"", manifest)


def _research_imports(manifest: bytes) -> tuple[str, ...]:
    names = tuple(match.group(1).decode() for match in _RESEARCH_IMPORT.finditer(manifest))
    if len(set(names)) != len(names):
        raise DocumentIoError("PROMOTION_MODULE_MISMATCH")
    return names


def _module_path(name: str) -> PurePosixPath:
    return PurePosixPath(*name.split(".")).with_suffix(".lean")


def _matches_source(relative: PurePosixPath, source: bytes, existing: bytes, mode: int) -> bool:
    if relative != _MANIFEST:
        return existing == source and mode == 0o444
    if not existing.startswith(source) or mode not in {0o444, 0o600}:
        return False
    additions = existing[len(source) :]
    return not additions or all(
        _RESEARCH_IMPORT.fullmatch(line + b"\n") is not None for line in additions.splitlines()
    )
