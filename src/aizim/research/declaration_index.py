"""Disposable, versioned declaration caches; research state remains in StateService."""

from __future__ import annotations

import json
import os
import stat
from collections import OrderedDict
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import RLock

from aizim.domain import sha256_bytes, sha256_json
from aizim.domain.serialization import JsonValue
from aizim.lean.document_io import (
    DocumentIoError,
    create_relative,
    open_root,
    read_relative,
    replace_relative,
)
from aizim.lean.source_layout import source_files

from .records import ResearchError, object_value
from .source_declarations import extract_source

_VERSION = 1
_LOCK = RLock()
_CACHES: OrderedDict[tuple[Path, bool], dict[str, JsonValue]] = OrderedDict()


@dataclass(frozen=True)
class DeclarationIndex:
    documents: list[dict[str, JsonValue]]
    fingerprint: str
    files: dict[str, JsonValue]
    parsed_files: int
    origin: str = "source"


def file_stamp(path: Path) -> list[JsonValue]:
    s = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(s.st_mode):
        raise OSError("Not a regular index input")
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns]


def read_cache(root: Path, name: str) -> dict[str, JsonValue]:
    descriptor = open_root(root)
    try:
        body = read_relative(descriptor, PurePosixPath(".aizim", "search", name + ".json"))
        result = object_value(json.loads(body))
        return result if result.get("version") == _VERSION else {}
    except (DocumentIoError, OSError, ValueError):
        return {}
    finally:
        os.close(descriptor)


def write_cache(root: Path, name: str, payload: dict[str, JsonValue]) -> None:
    if not (root / ".aizim").is_dir() or (root / ".aizim").is_symlink():
        return
    descriptor = open_root(root)
    relative = PurePosixPath(".aizim", "search", name + ".json")
    body = json.dumps({**payload, "version": _VERSION}, ensure_ascii=False).encode()
    try:
        try:
            replace_relative(descriptor, relative, body)
        except DocumentIoError:
            create_relative(descriptor, relative, body)
    except (DocumentIoError, OSError):
        # A missing/unwritable disposable cache must not prevent source search.
        pass
    finally:
        os.close(descriptor)


def cached_documents(value: JsonValue) -> list[dict[str, JsonValue]]:
    if type(value) is not list:
        raise ResearchError("INVALID_DECLARATION_CACHE")
    result = []
    for item in value:
        doc = object_value(item)
        for key in (
            "name",
            "full_name",
            "signature",
            "module",
            "file",
            "library",
            "kind",
            "origin",
        ):
            if type(doc.get(key)) is not str:
                raise ResearchError("INVALID_DECLARATION_CACHE")
        if type(doc.get("line")) is not int or type(doc.get("docstring")) is not str:
            raise ResearchError("INVALID_DECLARATION_CACHE")
        result.append(doc)
    return result


def project_roots(root: Path, include_dependencies: bool) -> list[Path]:
    roots = [root]
    packages = root / ".lake/packages"
    if include_dependencies and packages.is_dir():
        roots.extend(
            path.resolve(strict=True)
            for path in sorted(packages.iterdir())
            if path.is_dir()
            and any((path / name).is_file() for name in ("lakefile.toml", "lakefile.lean"))
        )
    return list(dict.fromkeys(roots))


def source_index(
    root: Path, *, include_dependencies: bool = False, rebuild: bool = False
) -> DeclarationIndex:
    root = root.resolve(strict=True)
    key, cache_name = (
        (root, include_dependencies),
        f"source-v{_VERSION}-{int(include_dependencies)}",
    )
    with _LOCK:
        cached = {} if rebuild else _CACHES.get(key)
        if cached is None:
            cached = read_cache(root, cache_name)
        old_files = cached.get("files", {})
        if type(old_files) is not dict:
            old_files = {}
        files: dict[str, JsonValue] = {}
        controls: dict[str, JsonValue] = {}
        documents: list[dict[str, JsonValue]] = []
        parsed_files = 0
        for project in project_roots(root, include_dependencies):
            descriptor = open_root(project)
            try:
                for relative in source_files(project):
                    path = project / relative
                    try:
                        stamp = file_stamp(path)
                    except (OSError, ValueError):
                        continue
                    if relative.suffix != ".lean" or relative.name == "lakefile.lean":
                        controls[str(path)] = sha256_bytes(read_relative(descriptor, relative))
                        continue
                    if path.stat().st_size > 4 * 1024 * 1024:
                        continue
                    old = old_files.get(str(path))
                    rows = None
                    if type(old) is dict and old.get("stamp") == stamp:
                        with suppress(ValueError):
                            rows = cached_documents(old.get("declarations"))
                    if rows is None:
                        source = read_relative(descriptor, relative)
                        if file_stamp(path) != stamp:
                            raise ResearchError("INDEX_SOURCE_CHANGED_RETRY")
                        rows = extract_source(
                            source.decode("utf-8"),
                            module=relative.with_suffix("").as_posix().replace("/", "."),
                            file=relative.as_posix(),
                            library=project.name,
                        )
                        parsed_files += 1
                        old = {"digest": sha256_bytes(source)}
                    assert type(old) is dict
                    files[str(path)] = {
                        "stamp": stamp,
                        "digest": old.get("digest", ""),
                        "declarations": list(rows),
                    }
                    documents.extend(rows)
            finally:
                os.close(descriptor)
        fingerprint = sha256_json(
            {
                "controls": controls,
                "files": {
                    path: object_value(data)["stamp"] for path, data in sorted(files.items())
                },
                "version": _VERSION,
            }
        )
        payload: dict[str, JsonValue] = {"fingerprint": fingerprint, "files": files}
        if cached.get("fingerprint") != fingerprint or parsed_files:
            write_cache(root, cache_name, payload)
        _CACHES[key] = payload
        _CACHES.move_to_end(key)
        while len(_CACHES) > 4:
            _CACHES.popitem(last=False)
        return DeclarationIndex(documents, fingerprint, files, parsed_files)
