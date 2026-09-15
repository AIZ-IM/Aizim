from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Protocol

from aizim.domain import sha256_json
from aizim.domain.serialization import JsonValue, canonical_json
from aizim.state.operations import AppendEventCommand
from aizim.state.projections import ProjectionRecord
from aizim.state.store_contracts import EventRecord

KINDS = frozenset({"task", "attempt", "memory", "inbox", "contribution", "review", "run"})
MEMORY_KINDS = frozenset(
    {"observation", "conjecture", "dead_end", "counterexample", "strategy", "summary"}
)
CONTRIBUTION_ROLES = frozenset(
    {
        "problem_formulation",
        "definitions",
        "strategy",
        "counterexample",
        "formalization",
        "semantic_review",
        "library_design",
        "exposition",
        "source_material",
    }
)
REVIEW_KINDS = frozenset(
    {"semantic_alignment", "research_value", "library_quality", "exposition", "release"}
)
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,47}")
_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")


class ResearchError(ValueError):
    pass


class ResearchStore(Protocol):
    def append_event(self, command: AppendEventCommand) -> EventRecord: ...
    def query_projection(self, name: str, entity_id: str) -> ProjectionRecord | None: ...
    def projections(self, name: str | None = None) -> tuple[ProjectionRecord, ...]: ...
    def query_events(self, run_id: str | None = None) -> tuple[EventRecord, ...]: ...


def text(value: JsonValue, *, limit: int = 65536, empty: bool = False) -> str:
    if type(value) is not str or (not empty and not value.strip()) or len(value.encode()) > limit:
        raise ResearchError("INVALID_TEXT")
    return value


def integer(value: JsonValue, *, minimum: int = 0, maximum: int = 9_007_199_254_740_991) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ResearchError("INVALID_INTEGER")
    return value


def texts(value: JsonValue) -> list[str]:
    if type(value) is not list or len(value) > 1000:
        raise ResearchError("INVALID_LIST")
    return [text(item) for item in value]


def object_value(value: JsonValue) -> dict[str, JsonValue]:
    if type(value) is not dict:
        raise ResearchError("INVALID_OBJECT")
    return value


def identifier(value: JsonValue) -> str:
    result = text(value, limit=48)
    if not _ID.fullmatch(result):
        raise ResearchError("INVALID_IDENTIFIER")
    return result


def record_id(kind: str, identity: str) -> str:
    if kind not in KINDS:
        raise ResearchError("INVALID_RECORD_KIND")
    return f"{kind}:{identifier(identity)}"


def unpack(record: ProjectionRecord) -> dict[str, JsonValue]:
    state = object_value(json.loads(record.state_json))
    payload = object_value(state.get("payload"))
    return object_value(payload.get("data"))


def get(store: ResearchStore, kind: str, identity: str) -> dict[str, JsonValue] | None:
    record = store.query_projection("research", record_id(kind, identity))
    return None if record is None else unpack(record)


def records(store: ResearchStore, kind: str) -> list[dict[str, JsonValue]]:
    return [
        unpack(item)
        for item in store.projections("research")
        if item.entity_id.startswith(f"{kind}:")
    ]


def save(
    store: ResearchStore,
    kind: str,
    identity: str,
    data: dict[str, JsonValue],
    *,
    actor: str,
    run_id: str | None = None,
) -> int:
    key = record_id(kind, identity)
    if not valid_record({"record_id": key, "kind": kind, "data": data}):
        raise ResearchError("INVALID_RESEARCH_RECORD")
    previous = get(store, kind, identity)
    if kind == "task" and previous is not None:
        for field in ("statement", "imports", "depends_on", "source_refs", "author", "target_hash"):
            if previous[field] != data[field]:
                raise ResearchError("TARGET_IS_IMMUTABLE")
    now = datetime.now(UTC).isoformat()
    data = {
        **data,
        "created_at": now if previous is None else previous.get("created_at", now),
        "updated_at": now,
    }
    store.append_event(
        AppendEventCommand(
            "ResearchRecordUpdated",
            text(actor, limit=256),
            run_id,
            None,
            {"record_id": key, "kind": kind, "data": data},
        )
    )
    updated = store.query_projection("research", key)
    if updated is None:
        raise ResearchError("RESEARCH_STATE_UNAVAILABLE")
    return updated.version


def task_hash(data: Mapping[str, JsonValue]) -> str:
    return sha256_json(
        {key: data[key] for key in ("statement", "imports", "depends_on", "source_refs")}
    )


def define_task(store: ResearchStore, params: dict[str, JsonValue]) -> int:
    required = {
        "task_id",
        "title",
        "statement",
        "author",
        "imports",
        "depends_on",
        "source_refs",
        "max_rounds",
        "max_failures",
        "timeout_seconds",
    }
    if params.keys() != required:
        raise ResearchError("INVALID_TASK_FIELDS")
    identity = identifier(params["task_id"])
    if get(store, "task", identity) is not None:
        raise ResearchError("TASK_ALREADY_EXISTS")
    dependencies = texts(params["depends_on"])
    if len(dependencies) != len(set(dependencies)) or identity in dependencies:
        raise ResearchError("INVALID_DEPENDENCIES")
    for dependency in dependencies:
        if get(store, "task", identifier(dependency)) is None:
            raise ResearchError("DEPENDENCY_NOT_FOUND")
    data = {
        **params,
        "status": "pending",
        "rounds": 0,
        "failures": 0,
        "target_hash": task_hash(params),
        "feedback": "",
        "verified_declarations": [],
        "paused": False,
    }
    return save(store, "task", identity, data, actor=text(params["author"]))


def operator_change(store: ResearchStore, params: dict[str, JsonValue]) -> int:
    action = text(params.get("action"))
    body = object_value(params.get("body"))
    if set(params) != {"action", "body"}:
        raise ResearchError("INVALID_OPERATOR_REQUEST")
    if action == "task.add":
        return define_task(store, body)
    author = text(body.get("author"), limit=256)
    if action in {"task.pause", "task.resume"}:
        if body.keys() != {"task_id", "author"}:
            raise ResearchError("INVALID_OPERATOR_REQUEST")
        identity = identifier(body["task_id"])
        task = require_task(store, identity)
        if task["status"] == "verified":
            raise ResearchError("TASK_ALREADY_VERIFIED")
        task["paused"] = action == "task.pause"
        if action == "task.resume" and task["status"] == "blocked":
            raise ResearchError("TASK_LIMIT_REACHED_CREATE_A_NEW_TARGET")
        return save(store, "task", identity, task, actor=author)
    identity = identifier(body.get("id"))
    task_id = text(body.get("task_id"), empty=True)
    if task_id:
        require_task(store, task_id)
    if action == "inbox.add":
        if body.keys() != {"id", "task_id", "author", "kind", "body"}:
            raise ResearchError("INVALID_INBOX_FIELDS")
        if body["kind"] not in {"hint", "question", "constraint"}:
            raise ResearchError("INVALID_INBOX_KIND")
        kind, data = "inbox", {**body, "consumed_by": []}
    elif action == "memory.add":
        if body.keys() != {"id", "task_id", "author", "kind", "body", "source_refs"}:
            raise ResearchError("INVALID_MEMORY_FIELDS")
        if body["kind"] not in MEMORY_KINDS:
            raise ResearchError("INVALID_MEMORY_KIND")
        kind, data = "memory", {**body, "origin": "operator", "worker_id": "", "verified": False}
    elif action == "contribution.add":
        if body.keys() != {"id", "task_id", "author", "role", "body", "source_refs"}:
            raise ResearchError("INVALID_CONTRIBUTION_FIELDS")
        if body["role"] not in CONTRIBUTION_ROLES:
            raise ResearchError("INVALID_CONTRIBUTION_ROLE")
        kind, data = "contribution", {**body, "identity_assurance": "operator_declared"}
    elif action == "review.add":
        if body.keys() != {"id", "task_id", "author", "kind", "verdict", "body"}:
            raise ResearchError("INVALID_REVIEW_FIELDS")
        task = require_task(store, task_id)
        if body["kind"] not in REVIEW_KINDS or body["verdict"] not in {
            "accept",
            "revise",
            "reject",
        }:
            raise ResearchError("INVALID_REVIEW")
        kind, data = (
            "review",
            {
                **body,
                "target_hash": task["target_hash"],
                "verified_declarations": task["verified_declarations"],
                "identity_assurance": "operator_declared",
            },
        )
    else:
        raise ResearchError("UNKNOWN_OPERATOR_ACTION")
    if get(store, kind, identity) is not None:
        raise ResearchError("RECORD_ALREADY_EXISTS")
    return save(store, kind, identity, data, actor=author)


def require_task(store: ResearchStore, identity: str) -> dict[str, JsonValue]:
    result = get(store, "task", identity)
    if result is None:
        raise ResearchError("TASK_NOT_FOUND")
    return result


def frontier(store: ResearchStore) -> list[dict[str, JsonValue]]:
    tasks = {text(task["task_id"]): task for task in records(store, "task")}
    return [
        task
        for _, task in sorted(tasks.items())
        if task["status"] == "pending"
        and integer(task["rounds"]) < integer(task["max_rounds"])
        and integer(task["failures"]) < integer(task["max_failures"])
        and not task["paused"]
        and all(
            tasks[dependency]["status"] == "verified" for dependency in texts(task["depends_on"])
        )
    ]


def valid_record(payload: Mapping[str, JsonValue]) -> bool:
    try:
        if payload.keys() != {"record_id", "kind", "data"}:
            return False
        kind = text(payload["kind"])
        data = object_value(payload["data"])
        for field in ("created_at", "updated_at"):
            if field in data:
                value = datetime.fromisoformat(text(data[field]))
                if value.utcoffset() != UTC.utcoffset(value):
                    return False
        data = {
            key: value for key, value in data.items() if key not in {"created_at", "updated_at"}
        }
        identity = identifier(data.get("task_id") if kind == "task" else data.get("id"))
        if payload["record_id"] != record_id(kind, identity) or len(canonical_json(data)) > 262144:
            return False
        if kind == "task":
            required = {
                "task_id",
                "title",
                "statement",
                "author",
                "imports",
                "depends_on",
                "source_refs",
                "max_rounds",
                "max_failures",
                "timeout_seconds",
                "status",
                "rounds",
                "failures",
                "target_hash",
                "feedback",
                "verified_declarations",
                "paused",
            }
            if data.keys() != required or data["status"] not in {
                "pending",
                "running",
                "verified",
                "blocked",
            }:
                return False
            statement = text(data["statement"])
            if ":=" in statement or re.search(
                r"\b(sorry|admit|axiom|unsafe|theorem|import|initialize)\b", statement
            ):
                return False
            text(data["title"], limit=1024)
            text(data["author"], limit=256)
            for module in texts(data["imports"]):
                if not _MODULE.fullmatch(module):
                    return False
            texts(data["depends_on"])
            texts(data["source_refs"])
            texts(data["verified_declarations"])
            text(data["feedback"], empty=True)
            integer(data["max_rounds"], minimum=1, maximum=1000)
            integer(data["max_failures"], minimum=1, maximum=1000)
            integer(data["timeout_seconds"], minimum=1, maximum=14400)
            integer(data["rounds"])
            integer(data["failures"])
            return type(data["paused"]) is bool and data["target_hash"] == task_hash(data)
        if kind in {"memory", "inbox", "contribution", "review"}:
            text(data.get("task_id"), empty=True)
            text(data.get("author"), limit=256)
            text(data.get("body"))
            for field in ("source_refs", "consumed_by", "verified_declarations"):
                if field in data:
                    texts(data[field])
        if kind == "memory":
            return (
                data.keys()
                == {
                    "id",
                    "task_id",
                    "author",
                    "kind",
                    "body",
                    "source_refs",
                    "origin",
                    "worker_id",
                    "verified",
                }
                and data["kind"] in MEMORY_KINDS
                and data["verified"] is False
                and data["origin"] in {"operator", "worker"}
                and type(data["worker_id"]) is str
                and type(data["source_refs"]) is list
            )
        if kind == "inbox":
            return (
                data.keys() == {"id", "task_id", "author", "kind", "body", "consumed_by"}
                and data["kind"] in {"hint", "question", "constraint"}
                and type(data["consumed_by"]) is list
            )
        if kind == "contribution":
            return (
                data.keys()
                == {"id", "task_id", "author", "role", "body", "source_refs", "identity_assurance"}
                and data["role"] in CONTRIBUTION_ROLES
                and data["identity_assurance"] == "operator_declared"
            )
        if kind == "review":
            return (
                data.keys()
                == {
                    "id",
                    "task_id",
                    "author",
                    "kind",
                    "verdict",
                    "body",
                    "target_hash",
                    "verified_declarations",
                    "identity_assurance",
                }
                and data["kind"] in REVIEW_KINDS
                and data["verdict"] in {"accept", "revise", "reject"}
                and data["identity_assurance"] == "operator_declared"
            )
        if kind == "attempt":
            return _valid_attempt(data)
        if kind == "run":
            return data.keys() == {
                "id",
                "status",
                "model",
                "participation",
                "started_at",
                "finished_at",
                "reason",
            } and data["status"] in {"running", "finished", "interrupted"}
    except (ResearchError, TypeError, KeyError, ValueError):
        return False
    return False


def _valid_attempt(data: dict[str, JsonValue]) -> bool:
    required = {
        "id",
        "task_id",
        "worker_id",
        "run_id",
        "target_hash",
        "round",
        "status",
        "feedback",
        "verified_declarations",
        "usage",
        "model",
        "inbox_ids",
    }
    if (
        not required <= data.keys()
        or data.keys() - required - {"controller_model", "controller_usage"}
        or data["status"]
        not in {
            "running",
            "verified",
            "failed",
            "interrupted",
        }
    ):
        return False
    identifier(data["task_id"])
    identifier(data["worker_id"])
    text(data["run_id"])
    text(data["model"])
    text(data["feedback"], empty=True)
    texts(data["inbox_ids"])
    texts(data["verified_declarations"])
    integer(data["round"], minimum=1)
    text(data.get("controller_model", ""), empty=True)
    for usage in (data["usage"], data.get("controller_usage")):
        if usage is None:
            continue
        values = object_value(usage)
        if values.keys() != {"input_tokens", "cached_input_tokens", "output_tokens"}:
            return False
        for value in values.values():
            integer(value)
        if integer(values["cached_input_tokens"]) > integer(values["input_tokens"]):
            return False
    return re.fullmatch(r"[0-9a-f]{64}", text(data["target_hash"])) is not None
