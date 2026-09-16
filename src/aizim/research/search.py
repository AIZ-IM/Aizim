from __future__ import annotations

import json
import math
import re
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path
from threading import RLock

from aizim.domain.serialization import JsonValue

from .compiled_index import compiled_index
from .declaration_index import DeclarationIndex, source_index
from .records import ResearchError, ResearchStore, records, text

_WORDS = re.compile(r"[\w]+", re.UNICODE)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_CORPORA: OrderedDict[str, SearchCorpus] = OrderedDict()
_LOCK = RLock()


def tokens(value: str) -> list[str]:
    result = []
    for word in _WORDS.findall(value):
        result.append(word.casefold())
        parts = _CAMEL.sub(" ", word).replace("_", " ").split()
        if len(parts) > 1:
            result.extend(part.casefold() for part in parts)
    return result


class SearchCorpus:
    """Compute document frequencies once, then score only matching postings."""

    def __init__(self, documents: list[dict[str, JsonValue]]) -> None:
        self.documents = documents
        self.lengths: list[int] = []
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for index, document in enumerate(documents):
            if "signature" in document:
                words = (
                    tokens(str(document.get("full_name", document.get("name", "")))) * 3
                    + tokens(str(document.get("signature", "")))
                    + tokens(str(document.get("docstring", ""))) * 2
                )
            else:
                words = tokens(json.dumps(document, ensure_ascii=False))
            self.lengths.append(len(words))
            for term, count in Counter(words).items():
                self.postings[term].append((index, count))
        self.average = sum(self.lengths) / len(documents) if documents else 1.0

    def search(self, query: str, limit: int) -> list[dict[str, JsonValue]]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ResearchError("INVALID_SEARCH_LIMIT")
        scores: dict[int, float] = defaultdict(float)
        for term in sorted(set(tokens(query))):
            postings = self.postings.get(term, [])
            frequency = len(postings)
            inverse = math.log(1 + (len(self.documents) - frequency + 0.5) / (frequency + 0.5))
            for index, occurrence in postings:
                scores[index] += (
                    inverse
                    * occurrence
                    * 2.2
                    / (
                        occurrence
                        + 1.2 * (0.25 + 0.75 * self.lengths[index] / (self.average or 1.0))
                    )
                )
        for index in scores:
            doc = self.documents[index]
            if query.casefold().strip() in {
                str(doc.get("name", "")).casefold(),
                str(doc.get("full_name", "")).casefold(),
            }:
                scores[index] += 10.0
        ranked_scores = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        return [
            {**self.documents[index], "score": round(score, 6)}
            for index, score in ranked_scores[:limit]
        ]


def ranked(
    query: str, documents: list[dict[str, JsonValue]], limit: int
) -> list[dict[str, JsonValue]]:
    return SearchCorpus(documents).search(query, limit)


def memory_search(
    store: ResearchStore,
    query: str,
    *,
    task_id: str = "",
    worker_id: str = "",
    limit: int = 10,
) -> list[dict[str, JsonValue]]:
    documents = [
        entry
        for entry in records(store, "memory")
        if entry["task_id"] in {"", task_id} and entry["worker_id"] in {"", worker_id}
    ]
    return ranked(query, documents, limit)


def declarations(root: Path, *, include_dependencies: bool = False) -> list[dict[str, JsonValue]]:
    return source_index(root, include_dependencies=include_dependencies).documents


def _index(root: Path, include_dependencies: bool) -> DeclarationIndex:
    has_compiler = (root / ".aizim/search/compiler-v1.json").is_file()
    source = source_index(root, include_dependencies=include_dependencies or has_compiler)
    index = compiled_index(root, source) if has_compiler else None
    if index is not None:
        indexed = {
            (entry["library"], entry["file"], entry["full_name"]) for entry in index.documents
        }
        remaining = [
            entry
            for entry in source.documents
            if (entry["library"], entry["file"], entry["full_name"]) not in indexed
        ]
        if remaining:
            index = DeclarationIndex(
                [*index.documents, *remaining], index.fingerprint, index.files, 0, "mixed"
            )
    index = index or source
    if include_dependencies:
        return index
    documents = [
        entry
        for entry in index.documents
        if entry.get("project_local", entry["library"] == root.name)
    ]
    return DeclarationIndex(
        documents, index.fingerprint + ":project", index.files, index.parsed_files, index.origin
    )


def lean_search(
    root: Path,
    query: str,
    *,
    mode: str = "text",
    limit: int = 10,
    include_dependencies: bool = True,
) -> list[dict[str, JsonValue]]:
    text(query, limit=4096)
    if mode not in {"text", "name", "type"} or type(limit) is not int or not 1 <= limit <= 100:
        raise ResearchError("INVALID_SEARCH_MODE")
    root = root.resolve(strict=True)
    index = _index(root, include_dependencies)
    if mode == "text":
        with _LOCK:
            corpus = _CORPORA.get(index.fingerprint)
            if corpus is None:
                corpus = _CORPORA[index.fingerprint] = SearchCorpus(index.documents)
            _CORPORA.move_to_end(index.fingerprint)
            while len(_CORPORA) > 4:
                _CORPORA.popitem(last=False)
        return corpus.search(query, limit)
    if mode == "name":
        return [
            dict(entry)
            for entry in index.documents
            if query.casefold() in str(entry["full_name"]).casefold()
        ][:limit]
    # Textual wildcard matching is a discovery aid, not Lean type unification.
    pattern = ".*?".join(
        re.escape(part) for part in re.split(r"\?[A-Za-z_][\w]*|(?<!\w)_(?!\w)", query)
    )
    return [
        dict(entry)
        for entry in index.documents
        if re.search(pattern, str(entry["signature"]), re.DOTALL)
    ][:limit]
