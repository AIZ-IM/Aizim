from __future__ import annotations

from typing import Final

from aizim.domain.serialization import JsonValue

from .capabilities import GatewayTool

_TEXT: Final[dict[str, JsonValue]] = {"type": "string", "minLength": 1}
_LINE: Final[dict[str, JsonValue]] = {"type": "integer", "minimum": 1}
_NONNEGATIVE: Final[dict[str, JsonValue]] = {"type": "integer", "minimum": 0}
_BOOLEAN: Final[dict[str, JsonValue]] = {"type": "boolean"}
_STRING_LIST: Final[dict[str, JsonValue]] = {"type": "array", "items": _TEXT}
_JSON_OBJECT: Final[dict[str, JsonValue]] = {"type": "object"}


def _object(
    properties: dict[str, JsonValue], required: tuple[str, ...] = ()
) -> dict[str, JsonValue]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


PAYLOAD_SCHEMAS: Final[dict[GatewayTool, dict[str, JsonValue]]] = {
    GatewayTool.STATE_QUERY: _object({"query": _TEXT}, ("query",)),
    GatewayTool.SCHEDULE_PROPOSE: _object(
        {"directive_id": _TEXT, "worker_id": _TEXT, "role": _TEXT},
        ("directive_id", "worker_id", "role"),
    ),
    GatewayTool.EVIDENCE_QUERY: _object({"query": _TEXT}, ("query",)),
    GatewayTool.SEARCH_BROKERED: _object({"query": _TEXT}, ("query",)),
    GatewayTool.EVIDENCE_SUBMIT: _object(
        {"evidence_id": _TEXT, "content_hash": _TEXT}, ("evidence_id", "content_hash")
    ),
    GatewayTool.CANDIDATE_SUBMIT: _object(
        {"candidate_name": _TEXT, "complete_type": _TEXT},
        ("candidate_name", "complete_type"),
    ),
    GatewayTool.AUDIT_SUBMIT: _object(
        {"audit_id": _TEXT, "verdict": _TEXT}, ("audit_id", "verdict")
    ),
    GatewayTool.PROJECT_READ: _object({"path": _TEXT}, ("path",)),
    GatewayTool.LEAN_GOAL: _object(
        {"document_id": _TEXT, "line": _LINE, "column": _LINE},
        ("document_id", "line"),
    ),
    GatewayTool.LEAN_MULTI_ATTEMPT: _object(
        {
            "document_id": _TEXT,
            "line": _LINE,
            "snippets": {"type": "array", "items": _TEXT, "minItems": 1},
            "column": _LINE,
        },
        ("document_id", "line", "snippets"),
    ),
    GatewayTool.LEAN_DIAGNOSTICS: _object(
        {"document_id": _TEXT, "start_line": _LINE, "end_line": _LINE},
        ("document_id",),
    ),
    GatewayTool.ALIGNMENT_SUBMIT: _object(
        {"review_id": _TEXT, "verdict": _TEXT}, ("review_id", "verdict")
    ),
    GatewayTool.FORMAL_GRAPH_READ: _object(
        {"after_knowledge_epoch": _NONNEGATIVE}, ("after_knowledge_epoch",)
    ),
    GatewayTool.TASK_EDGE_SUBMIT: _object(
        {"edge_id": _TEXT, "source": _TEXT, "target": _TEXT},
        ("edge_id", "source", "target"),
    ),
    GatewayTool.DOCUMENT_APPLY: _object(
        {"document_id": _TEXT, "accepted": _TEXT, "import_module": _TEXT},
        ("document_id", "accepted"),
    ),
    GatewayTool.CONTRIBUTION_SUBMIT: _object(
        {
            "document_id": _TEXT,
            "contribution_id": _TEXT,
            "candidate_name": _TEXT,
            "complete_type": _TEXT,
            "imports": _STRING_LIST,
            "dependencies": _STRING_LIST,
            "assumptions": _STRING_LIST,
            "evidence_links": _STRING_LIST,
        },
        (
            "document_id",
            "contribution_id",
            "candidate_name",
            "complete_type",
            "imports",
            "dependencies",
            "assumptions",
            "evidence_links",
        ),
    ),
    GatewayTool.KNOWLEDGE_READ: _object(
        {"after_knowledge_epoch": _NONNEGATIVE, "wait": _BOOLEAN},
        ("after_knowledge_epoch",),
    ),
    GatewayTool.TRACE_READ: _object({"run_id": _TEXT}, ("run_id",)),
    GatewayTool.ANNOTATION_SUBMIT: _object(
        {"annotation_id": _TEXT, "target_id": _TEXT, "text": _TEXT},
        ("annotation_id", "target_id", "text"),
    ),
    GatewayTool.STATE_APPEND: _object(
        {"event_type": _TEXT, "payload": _JSON_OBJECT}, ("event_type", "payload")
    ),
    GatewayTool.DOCUMENT_RESOLVE_PATH: _object({"document_id": _TEXT}, ("document_id",)),
    GatewayTool.LEAN_BUILD: _object(
        {"clean": _BOOLEAN, "fetch_cache": _BOOLEAN}, ("clean", "fetch_cache")
    ),
    GatewayTool.LEAN_VERIFY: _object(
        {"document_id": _TEXT, "theorem_name": _TEXT, "scan_source": _BOOLEAN},
        ("document_id", "theorem_name", "scan_source"),
    ),
    GatewayTool.PROMOTION_ENQUEUE: _object({"contribution_id": _TEXT}, ("contribution_id",)),
    GatewayTool.PROMOTION_PUBLISH: _object({"contribution_id": _TEXT}, ("contribution_id",)),
    GatewayTool.ENVIRONMENT_APPROVE: _object({"transition_id": _TEXT}, ("transition_id",)),
    GatewayTool.CAPABILITY_MINT: _object(
        {
            "worker_id": _TEXT,
            "role": _TEXT,
            "operations": _STRING_LIST,
            "expires_at": _TEXT,
        },
        ("worker_id", "role", "operations", "expires_at"),
    ),
}

if set(PAYLOAD_SCHEMAS) != set(GatewayTool):
    raise RuntimeError("INCOMPLETE_GATEWAY_TOOL_SCHEMAS")


def input_schema(tool: GatewayTool) -> dict[str, JsonValue]:
    return _object({"payload": PAYLOAD_SCHEMAS[tool]}, ("payload",))
