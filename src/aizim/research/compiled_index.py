from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
from collections import OrderedDict
from pathlib import Path
from threading import RLock

from aizim.config import LEAN_TOOLCHAIN
from aizim.domain import sha256_json
from aizim.domain.serialization import JsonValue

from .declaration_index import (
    DeclarationIndex,
    cached_documents,
    file_stamp,
    project_roots,
    read_cache,
    source_index,
    write_cache,
)
from .records import ResearchError, object_value, text

_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_EXTRACTOR = Path(__file__).with_name("ExtractDeclarations.lean")
_LOCK = RLock()
_LOADED: OrderedDict[
    Path, tuple[list[JsonValue], dict[str, JsonValue], list[dict[str, JsonValue]]]
] = OrderedDict()


def _run(command: list[str], root: Path, timeout: int) -> str:
    with subprocess.Popen(
        command,
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise ResearchError("DECLARATION_INDEX_TIMEOUT") from None
        if process.returncode:
            raise ResearchError("DECLARATION_INDEX_FAILED: " + (stderr or stdout)[-2000:])
        return stdout


def _lake(root: Path, supplied: Path | None) -> Path:
    toolchain = (root / "lean-toolchain").read_text().strip()
    if toolchain != LEAN_TOOLCHAIN:
        raise ResearchError(f"COMPILER_INDEX_REQUIRES_TOOLCHAIN: {LEAN_TOOLCHAIN}")
    candidate = supplied or Path(shutil.which("lake") or "/nonexistent/lake")
    candidate = candidate.expanduser().resolve()
    if candidate.name == "elan" or not candidate.is_file():
        elan_root = Path(os.environ.get("ELAN_HOME", str(Path.home() / ".elan")))
        candidate = (
            elan_root / "toolchains" / toolchain.replace("/", "--").replace(":", "---") / "bin/lake"
        )
    if not candidate.is_file():
        raise ResearchError("INSTALLED_LAKE_REQUIRED: pass --lake /path/to/toolchain/bin/lake")
    version = _run([str(candidate.with_name("lean")), "--version"], root, 10)
    match = re.search(r"version ([^, )]+)", version)
    if match is None or match[1] != toolchain.rsplit(":v", 1)[-1]:
        raise ResearchError("COMPILER_INDEX_TOOLCHAIN_MISMATCH")
    return candidate


def _artifacts(root: Path) -> dict[str, JsonValue]:
    artifacts: dict[str, JsonValue] = {}
    for project in project_roots(root, True):
        library = project / ".lake/build/lib/lean"
        if not library.is_dir():
            continue
        for directory, folders, names in os.walk(library, followlinks=False):
            parent = Path(directory)
            folders[:] = sorted(name for name in folders if not (parent / name).is_symlink())
            for name in sorted(names):
                if ".olean" in name:
                    path = parent / name
                    artifacts[str(path)] = file_stamp(path)
    return artifacts


def _source_locations(
    source: DeclarationIndex, root: Path
) -> dict[str, tuple[str, str, bool] | None]:
    """Resolve module suffixes too, for Lake projects with a custom srcDir."""
    locations: dict[str, tuple[str, str, bool] | None] = {}
    roots = sorted(project_roots(root, True), key=lambda path: len(path.parts), reverse=True)
    for name in source.files:
        path = Path(name)
        project = next(project for project in roots if path.is_relative_to(project))
        relative = path.relative_to(project)
        value = (relative.as_posix(), project.name, project == root)
        parts = relative.with_suffix("").parts
        for start in range(len(parts)):
            module = ".".join(parts[start:])
            if module in locations and locations[module] != value:
                locations[module] = None
            else:
                locations[module] = value
    return locations


def build_compiler_index(
    root: Path, modules: list[str], *, lake: Path | None = None, timeout: int = 120
) -> dict[str, JsonValue]:
    root = root.resolve(strict=True)
    if not modules or any(_MODULE.fullmatch(module) is None for module in modules):
        raise ResearchError("INDEX_REQUIRES_VALID_MODULES")
    if not 1 <= timeout <= 3600:
        raise ResearchError("INVALID_INDEX_TIMEOUT")
    lake = _lake(root, lake)
    # Ask Lake to check its dependency traces. A stale module is an error, not a build request.
    _run(
        [str(lake), "--no-cache", "--no-build", "build", *[f"+{m}:olean" for m in modules]],
        root,
        timeout,
    )
    source = source_index(root, include_dependencies=True)
    artifacts = _artifacts(root)
    binary = lake.with_name("lean")
    artifacts[str(binary)] = file_stamp(binary)
    artifacts[str(_EXTRACTOR)] = file_stamp(_EXTRACTOR)
    stdout = _run(
        [str(lake), "env", str(binary), "--run", str(_EXTRACTOR), *modules], root, timeout
    )
    locations = _source_locations(source, root)
    rows: list[JsonValue] = []
    for line in stdout.splitlines():
        entry = object_value(json.loads(line))
        module = text(entry.get("module"))
        name = text(entry.get("full_name"))
        relative = Path(*module.split(".")).with_suffix(".lean")
        location = locations.get(module)
        entry.update(
            name=name.rsplit(".", 1)[-1],
            file=location[0] if location else relative.as_posix(),
            library=location[1] if location else "toolchain",
            origin="compiler",
            project_local=location[2] if location else False,
        )
        rows.append(entry)
    documents = cached_documents(rows)
    documents.sort(key=lambda entry: (str(entry["module"]), str(entry["full_name"])))
    if source_index(root, include_dependencies=True).fingerprint != source.fingerprint:
        raise ResearchError("INDEX_SOURCE_CHANGED_RETRY")
    if any(file_stamp(Path(path)) != stamp for path, stamp in artifacts.items()):
        raise ResearchError("INDEX_ARTIFACT_CHANGED_RETRY")
    payload: dict[str, JsonValue] = {
        "source_fingerprint": source.fingerprint,
        "modules": list(dict.fromkeys(modules)),
        "artifacts": artifacts,
        "documents": list(documents),
        "fingerprint": sha256_json(
            {"source": source.fingerprint, "artifacts": artifacts, "modules": modules}
        ),
    }
    write_cache(root, "compiler-v1", payload)
    if read_cache(root, "compiler-v1").get("fingerprint") != payload["fingerprint"]:
        raise ResearchError("COMPILER_INDEX_CACHE_UNAVAILABLE")
    return {
        "origin": "compiler",
        "declarations": len(rows),
        "modules": list(modules),
        "fingerprint": payload["fingerprint"],
    }


def compiled_index(root: Path, source: DeclarationIndex) -> DeclarationIndex | None:
    try:
        stamp = file_stamp(root / ".aizim/search/compiler-v1.json")
        with _LOCK:
            cached = _LOADED.get(root)
            if cached is None or cached[0] != stamp:
                payload = read_cache(root, "compiler-v1")
                docs = cached_documents(payload.get("documents"))
                _LOADED[root] = stamp, payload, docs
            else:
                _, payload, docs = cached
            _LOADED.move_to_end(root)
            while len(_LOADED) > 2:
                _LOADED.popitem(last=False)
        if payload.get("source_fingerprint") != source.fingerprint:
            return None
        artifacts = object_value(payload.get("artifacts"))
        if not artifacts or any(file_stamp(Path(p)) != stamp for p, stamp in artifacts.items()):
            return None
        return DeclarationIndex(docs, text(payload.get("fingerprint")), source.files, 0, "compiler")
    except (OSError, ValueError):
        return None
