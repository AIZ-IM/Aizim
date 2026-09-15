from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path

from aizim.domain.serialization import JsonValue
from aizim.lean.source_layout import source_files

from .records import ResearchError, ResearchStore, records, text

_WORDS = re.compile(r"[\w]+", re.UNICODE)
_DECLARATION = re.compile(
    r"(?m)^\s*(?:noncomputable\s+)?(theorem|lemma|def|abbrev|structure|class)\s+([^\s(:{\[]+)([^\n]*)"
)


def tokens(value: str) -> list[str]:
    return [word.casefold() for word in _WORDS.findall(value)]


def ranked(
    query: str, documents: list[dict[str, JsonValue]], limit: int
) -> list[dict[str, JsonValue]]:
    if not 1 <= limit <= 100:
        raise ResearchError("INVALID_SEARCH_LIMIT")
    terms = tokens(query)
    if not terms or not documents:
        return []
    words = [tokens(json.dumps(document, ensure_ascii=False)) for document in documents]
    frequencies = [Counter(row) for row in words]
    average = sum(map(len, words)) / len(words) or 1.0
    scores: list[tuple[float, int]] = []
    for index, (document_words, counts) in enumerate(zip(words, frequencies, strict=True)):
        score = 0.0
        for term in set(terms):
            occurrence = counts[term]
            if not occurrence:
                continue
            frequency = sum(term in row for row in frequencies)
            inverse = math.log(1 + (len(words) - frequency + 0.5) / (frequency + 0.5))
            score += (
                inverse
                * occurrence
                * 2.2
                / (occurrence + 1.2 * (0.25 + 0.75 * len(document_words) / average))
            )
        if score:
            scores.append((score, index))
    scores.sort(key=lambda item: (-item[0], item[1]))
    return [{**documents[index], "score": round(score, 6)} for score, index in scores[:limit]]


def memory_search(
    store: ResearchStore,
    query: str,
    *,
    task_id: str = "",
    worker_id: str = "",
    limit: int = 10,
) -> list[dict[str, JsonValue]]:
    documents = []
    for entry in records(store, "memory"):
        if entry["task_id"] not in {"", task_id}:
            continue
        if entry["worker_id"] not in {"", worker_id}:
            continue
        documents.append(entry)
    return ranked(query, documents, limit)


def declarations(root: Path, *, include_dependencies: bool = False) -> list[dict[str, JsonValue]]:
    roots = [root.resolve(strict=True)]
    if include_dependencies:
        packages = root / ".lake/packages"
        if packages.is_dir():
            roots.extend(
                path.resolve(strict=True)
                for path in sorted(packages.iterdir())
                if path.is_dir() and (path / "lean-toolchain").is_file()
            )
    documents: list[dict[str, JsonValue]] = []
    for project in roots:
        try:
            files = source_files(project)
        except ValueError:
            continue
        for relative in files:
            if relative.suffix != ".lean" or relative.name == "lakefile.lean":
                continue
            path = project / relative
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
                continue
            source = path.read_text(encoding="utf-8")
            for match in _DECLARATION.finditer(source):
                tail = source[match.start() : match.start() + 1500]
                signature = tail.split(":=", 1)[0].strip()
                prefix = source[max(0, match.start() - 500) : match.start()]
                docstring = prefix.rsplit("/-", 1)[-1].split("-/", 1)[0] if "/-" in prefix else ""
                documents.append(
                    {
                        "name": match.group(2),
                        "kind": match.group(1),
                        "signature": signature,
                        "docstring": docstring,
                        "module": relative.with_suffix("").as_posix().replace("/", "."),
                        "file": relative.as_posix(),
                        "line": source.count("\n", 0, match.start()) + 1,
                        "library": project.name,
                    }
                )
    return documents


def lean_search(
    root: Path,
    query: str,
    *,
    mode: str = "text",
    limit: int = 10,
    include_dependencies: bool = True,
) -> list[dict[str, JsonValue]]:
    text(query, limit=4096)
    if mode not in {"text", "name", "type"} or not 1 <= limit <= 100:
        raise ResearchError("INVALID_SEARCH_MODE")
    docs = declarations(root, include_dependencies=include_dependencies)
    if mode == "text":
        return ranked(query, docs, limit)
    if mode == "name":
        return [entry for entry in docs if query.casefold() in text(entry["name"]).casefold()][
            :limit
        ]
    # A discoverability aid only; Lean checks every proposed use of a result.
    pattern = ".*?".join(re.escape(part) for part in re.split(r"\?[A-Za-z_][\w]*|_", query))
    return [entry for entry in docs if re.search(pattern, text(entry["signature"]), re.DOTALL)][
        :limit
    ]
