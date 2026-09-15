from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from aizim.domain import compute_base_epoch, sha256_bytes, sha256_json

from .document_io import (
    DocumentIoError,
    create_relative,
    ensure_tree,
    mode_relative,
    open_root,
    read_relative,
    replace_relative,
)

_IGNORED = frozenset({".git", ".aizim", ".lake", ".venv", "node_modules", "build", "dist"})
_GENERATED = re.compile(rb"^import (AizimResearch\.[A-Za-z_][A-Za-z0-9_]*)\n", re.MULTILINE)
MANIFEST = PurePosixPath("AizimResearch.lean")

if TYPE_CHECKING:
    from .project import PublishedModule


def lakefile(root: Path) -> Path:
    for name in ("lakefile.toml", "lakefile.lean"):
        path = root / name
        if path.is_file() and not path.is_symlink():
            return path
    raise DocumentIoError("LAKEFILE_UNAVAILABLE")


def is_smoke(root: Path) -> bool:
    return (root / "AizimSmoke.lean").is_file() and (root / "AizimSmoke/Base.lean").is_file()


def publication_namespace(root: Path) -> str:
    return "AizimSmoke.Research" if is_smoke(root) else "AizimResearch"


def publication_manifest(root: Path) -> PurePosixPath:
    return PurePosixPath("AizimSmoke.lean") if is_smoke(root) else MANIFEST


def worker_path(root: Path, run_id: str, worker_id: str) -> PurePosixPath:
    base = "AizimSmoke" if is_smoke(root) else "AizimResearch"
    return PurePosixPath(base, "Workers", run_id, f"{worker_id}.lean")


def prepared_lakefile(path: PurePosixPath, source: bytes) -> bytes:
    marker = "Aizim generated research library"
    body = source.decode("utf-8")
    if marker in body:
        return source
    if path.name == "lakefile.lean":
        if re.search(r"(?m)^\s*lean_lib\s+AizimResearch\b", body):
            return source
        return (body + f"\n-- {marker}\n@[default_target]\nlean_lib AizimResearch\n").encode()
    parsed = tomllib.loads(body)
    targets = parsed.get("defaultTargets")
    if targets is None:
        targets = [
            item["name"] for kind in ("lean_lib", "lean_exe") for item in parsed.get(kind, [])
        ]
    if type(targets) is not list or any(type(target) is not str for target in targets):
        raise DocumentIoError("INVALID_LAKE_TARGETS")
    targets = list(dict.fromkeys([*targets, "AizimResearch"]))
    if "defaultTargets" in parsed:
        body, count = re.subn(r"(?m)^defaultTargets\s*=\s*\[[\s\S]*?\]", "", body, count=1)
        if count != 1:
            raise DocumentIoError("UNSUPPORTED_LAKE_TARGET_SYNTAX")
    body = f"defaultTargets = {json.dumps(targets)}\n" + body
    if not any(item.get("name") == "AizimResearch" for item in parsed.get("lean_lib", [])):
        body += '\n[[lean_lib]]\nname = "AizimResearch"\n'
    body += f"\n# {marker}\n"
    if tomllib.loads(body).get("defaultTargets") != targets:
        raise DocumentIoError("INVALID_LAKE_TARGETS")
    return body.encode()


def source_files(root: Path) -> tuple[PurePosixPath, ...]:
    files = {PurePosixPath("lean-toolchain"), PurePosixPath(lakefile(root).name)}
    if (root / "lake-manifest.json").is_file():
        files.add(PurePosixPath("lake-manifest.json"))
    for directory, folders, names in os.walk(root, followlinks=False):
        parent = Path(directory)
        folders[:] = sorted(
            name
            for name in folders
            if name not in _IGNORED
            and not name.startswith(".")
            and not (parent / name).is_symlink()
            and (parent / name) != root / "AizimResearch"
        )
        for name in sorted(names):
            if name.endswith(".lean") and not name.startswith("."):
                files.add(PurePosixPath((parent / name).relative_to(root).as_posix()))
    return tuple(sorted(files, key=str))


def allowed_imports(root: Path) -> tuple[str, ...]:
    if is_smoke(root):
        return ("Std",)
    names = {"Init", "Lean", "Std"}
    descriptor = open_root(root)
    try:
        for path in source_files(root):
            if path.suffix != ".lean":
                continue
            names.add(path.with_suffix("").as_posix().replace("/", "."))
            body = read_relative(descriptor, path).decode("utf-8")
            names.update(re.findall(r"(?m)^\s*import\s+([A-Za-z_][A-Za-z0-9_.]*)", body))
    finally:
        os.close(descriptor)
    if (root / ".lake/packages/mathlib").is_dir():
        names.add("Mathlib")
    return tuple(sorted(names))


def base_epoch(root: Path, extra: dict[str, bytes] | None = None) -> str:
    descriptor = open_root(root)
    try:
        bodies = {path: read_relative(descriptor, path) for path in source_files(root)}
        control = PurePosixPath(lakefile(root).name)
        bodies[control] = prepared_lakefile(control, bodies[control])
        manifest = bodies.get(MANIFEST, b"")
        bodies[MANIFEST] = _GENERATED.sub(b"", manifest)
        modules = {
            path.with_suffix("").as_posix().replace("/", "."): sha256_bytes(body)
            for path, body in bodies.items()
            if path.suffix == ".lean" and path.name != "lakefile.lean"
        }
        for match in _GENERATED.finditer(manifest):
            name = match.group(1).decode()
            modules[name] = sha256_bytes(read_relative(descriptor, module_path(name)))
        for name, source in (extra or {}).items():
            if not _GENERATED.fullmatch(f"import {name}\n".encode()):
                raise DocumentIoError("INVALID_PUBLISHED_MODULE")
            digest = sha256_bytes(source)
            if name in modules and modules[name] != digest:
                raise DocumentIoError("PROMOTION_MODULE_MISMATCH")
            modules[name] = digest
        controls = {
            str(path): sha256_bytes(body)
            for path, body in bodies.items()
            if (path.suffix != ".lean" or path.name == "lakefile.lean")
            and path.name != "lake-manifest.json"
        }
        lock = json.loads(bodies.get(PurePosixPath("lake-manifest.json"), b"{}"))
        if type(lock) is not dict or type(lock.get("packages", [])) is not list:
            raise DocumentIoError("INVALID_LAKE_MANIFEST")
        controls["lake_dependencies"] = sha256_json(lock.get("packages", []))
    finally:
        os.close(descriptor)
    return compute_base_epoch(sha256_json(controls), modules)


def module_path(name: str) -> PurePosixPath:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", name) is None:
        raise DocumentIoError("INVALID_PUBLISHED_MODULE")
    return PurePosixPath(*name.split(".")).with_suffix(".lean")


def materialize(root: Path, run_id: str, source_root: Path) -> Path:
    source_fd, project_fd = open_root(source_root), open_root(root)
    result = root / ".aizim/run" / run_id / "lean-project"
    try:
        destination = ensure_tree(project_fd, (".aizim", "run", run_id, "lean-project"))
        try:
            for path in source_files(source_root):
                original_source = read_relative(source_fd, path)
                body = (
                    prepared_lakefile(path, original_source)
                    if path.name in {"lakefile.toml", "lakefile.lean"}
                    else original_source
                )
                try:
                    existing = read_relative(destination, path)
                except DocumentIoError:
                    create_relative(destination, path, body, mode=0o444)
                else:
                    if existing != body and not (
                        path == MANIFEST and _GENERATED.sub(b"", existing) == body
                    ):
                        raise DocumentIoError("RUN_PROJECT_MISMATCH")
                if read_relative(source_fd, path) != original_source:
                    raise DocumentIoError("PROJECT_SOURCE_CHANGED")
            try:
                read_relative(destination, MANIFEST)
            except DocumentIoError:
                create_relative(destination, MANIFEST, b"", mode=0o444)
        finally:
            os.close(destination)
        # Lake package sources are supervisor-owned. Build output stays in the run copy.
        packages = source_root / ".lake/packages"
        if packages.is_dir():
            cache = result / ".lake"
            cache.mkdir(mode=0o700, exist_ok=True)
            link = cache / "packages"
            if not link.exists():
                link.symlink_to(packages.resolve(strict=True), target_is_directory=True)
            elif link.resolve(strict=True) != packages.resolve(strict=True):
                raise DocumentIoError("DEPENDENCY_ROOT_CHANGED")
    finally:
        os.close(project_fd)
        os.close(source_fd)
    return result


def sync_modules(
    project_root: Path, run_project: Path, published: tuple[PublishedModule, ...]
) -> str:
    destination, project = open_root(run_project), open_root(project_root)
    try:
        manifest = _GENERATED.sub(b"", read_relative(destination, MANIFEST))
        names: set[str] = set()
        for item in sorted(published, key=lambda entry: entry.module):
            if item.module in names or not _GENERATED.fullmatch(f"import {item.module}\n".encode()):
                raise DocumentIoError("PROMOTION_MODULE_MISMATCH")
            names.add(item.module)
            artifact = PurePosixPath(
                ".aizim/artifacts", item.run_id, "promotions", item.content_hash
            )
            source = read_relative(project, artifact)
            if (
                sha256_bytes(source) != item.content_hash
                or mode_relative(project, artifact) != 0o600
            ):
                raise DocumentIoError("PROMOTION_MODULE_MISMATCH")
            path = module_path(item.module)
            try:
                old = read_relative(destination, path)
            except DocumentIoError:
                create_relative(destination, path, source, mode=0o444)
            else:
                if old != source or mode_relative(destination, path) != 0o444:
                    raise DocumentIoError("PROMOTION_MODULE_MISMATCH")
            manifest += f"import {item.module}\n".encode()
        if read_relative(destination, MANIFEST) != manifest:
            replace_relative(destination, MANIFEST, manifest)
    finally:
        os.close(project)
        os.close(destination)
    return base_epoch(run_project)
