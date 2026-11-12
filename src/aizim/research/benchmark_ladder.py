from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from string import Formatter

from aizim.domain import sha256_json
from aizim.domain.serialization import JsonValue

from .records import ResearchError, identifier, object_value, text, texts

_FIXTURE = Path(__file__).parent / "fixtures" / "ladder-v1.json"


@dataclass(frozen=True, slots=True)
class LadderTarget:
    task_id: str
    title: str
    statement: str
    imports: tuple[str, ...]
    depends_on: tuple[str, ...]
    proof: str


@dataclass(frozen=True, slots=True)
class BenchmarkLadder:
    version: str
    content_hash: str
    lean_toolchain: str
    project_files: tuple[tuple[str, str], ...]
    targets: tuple[LadderTarget, ...]

    def target(self, task_id: str) -> LadderTarget:
        for target in self.targets:
            if target.task_id == task_id:
                return target
        raise ResearchError("BENCHMARK_TARGET_NOT_FOUND")


def load_ladder() -> BenchmarkLadder:
    """Load the fixed, packaged fixture; edits require a new ladder version."""
    body = object_value(json.loads(_FIXTURE.read_text()))
    files = object_value(body["project_files"])
    project_files: list[tuple[str, str]] = []
    for name, content in files.items():
        # The fixture can create only direct source/control files in a fresh project.
        if Path(name).name != name or name.startswith(".") or "\\" in name:
            raise ResearchError("INVALID_BENCHMARK_PROJECT_FILE")
        project_files.append((name, text(content)))
    targets: list[LadderTarget] = []
    seen: set[str] = set()
    raw_targets = body["targets"]
    if type(raw_targets) is not list or not raw_targets:
        raise ResearchError("INVALID_BENCHMARK_LADDER")
    for raw in raw_targets:
        item = object_value(raw)
        task_id = identifier(item["task_id"])
        dependencies = tuple(texts(item["depends_on"]))
        proof = text(item["proof"])
        references = {field for _, field, _, _ in Formatter().parse(proof) if field is not None}
        if task_id in seen or not set(dependencies) <= seen or references != set(dependencies):
            raise ResearchError("INVALID_BENCHMARK_DEPENDENCIES")
        targets.append(LadderTarget(
            task_id, text(item["title"]), text(item["statement"]),
            tuple(texts(item["imports"])), dependencies, proof,
        ))
        seen.add(task_id)
    return BenchmarkLadder(
        text(body["version"]), sha256_json(body), text(body["lean_toolchain"]),
        tuple(project_files), tuple(targets),
    )


def task_parameters(
    target: LadderTarget, *, max_rounds: int, timeout_seconds: int, call_budget: int,
) -> dict[str, JsonValue]:
    return {
        "task_id": target.task_id,
        "title": target.title,
        "statement": target.statement,
        "author": "benchmark",
        "imports": list(target.imports),
        "depends_on": list(target.depends_on),
        "source_refs": [],
        "max_rounds": max_rounds,
        "max_failures": max_rounds,
        "timeout_seconds": timeout_seconds,
        "call_budget": call_budget,
    }
