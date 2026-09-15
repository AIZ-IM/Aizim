from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
from pathlib import Path

from aizim.domain.serialization import JsonValue
from aizim.research.records import (
    CONTRIBUTION_ROLES,
    MEMORY_KINDS,
    REVIEW_KINDS,
    ResearchError,
    object_value,
)
from aizim.research.report import load_prices, snapshot
from aizim.research.search import lean_search, ranked
from aizim.runtime.layout import ProjectLayout
from aizim.state.control_operations import ControlOperationError

from .state_client import StateClientError, call_control_operation


def add_research_parser(commands) -> None:
    root = commands.add_parser(
        "research", help="Lean research targets, memory, human guidance, and reports"
    )
    verbs = root.add_subparsers(dest="research_command", required=True)
    for name in ("task", "memory", "inbox", "contribution", "review"):
        group = verbs.add_parser(name)
        actions = group.add_subparsers(dest="research_action", required=True)
        add = actions.add_parser("add")
        _common(add)
        add.add_argument("--author", default="operator", help="Operator-declared attribution")
        add.add_argument("--task-id", required=name in {"task", "review"}, default="")
        if name == "task":
            add.add_argument("--title", required=True)
            add.add_argument(
                "--statement",
                required=True,
                help="Lean proposition or binders and result type; no proof",
            )
            add.add_argument("--import", dest="imports", action="append", default=None)
            add.add_argument("--depends-on", action="append", default=[])
            add.add_argument("--source-ref", action="append", default=[])
            add.add_argument("--max-rounds", type=int, default=6)
            add.add_argument("--max-failures", type=int, default=3)
            add.add_argument("--timeout-seconds", type=int, default=300)
        else:
            add.add_argument("--id", default=None)
            add.add_argument("--body", required=True)
            if name == "memory":
                add.add_argument("--kind", choices=sorted(MEMORY_KINDS), required=True)
                add.add_argument("--source-ref", action="append", default=[])
            elif name == "inbox":
                add.add_argument(
                    "--kind", choices=("hint", "question", "constraint"), default="hint"
                )
            elif name == "contribution":
                add.add_argument("--role", choices=sorted(CONTRIBUTION_ROLES), required=True)
                add.add_argument("--source-ref", action="append", default=[])
            else:
                add.add_argument("--kind", choices=sorted(REVIEW_KINDS), required=True)
                add.add_argument("--verdict", choices=("accept", "revise", "reject"), required=True)
        listing = actions.add_parser("list")
        _common(listing)
        if name == "task":
            for action in ("pause", "resume"):
                command = actions.add_parser(action)
                _common(command)
                command.add_argument("--task-id", required=True)
                command.add_argument("--author", default="operator")
        if name == "memory":
            search = actions.add_parser("search")
            _common(search)
            search.add_argument("query")
            search.add_argument("--task-id", default="")
            search.add_argument("--limit", type=int, default=10)
    search = verbs.add_parser("search", help="Offline Lean declaration search")
    _common(search)
    search.add_argument("query")
    search.add_argument("--mode", choices=("text", "name", "type"), default="text")
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--project-only", action="store_true")
    run = verbs.add_parser("run")
    _common(run)
    run.add_argument("--watch", action="store_true")
    run.add_argument("--max-seconds", type=int)
    dashboard = verbs.add_parser("dashboard")
    _common(dashboard)
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--prices", type=Path)
    export = verbs.add_parser("export")
    _common(export)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--format", choices=("json", "html"), default="html")
    export.add_argument("--prices", type=Path)


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true", dest="as_json")


def run_research_command(args: argparse.Namespace) -> int:
    try:
        return _run(args)
    except (ResearchError, ValueError, OSError, StateClientError, ControlOperationError) as error:
        print(f"aizim research: {error}", file=sys.stderr)
        return 4
    except Exception as error:
        print(f"aizim research: {type(error).__name__}", file=sys.stderr)
        return 6


def _run(args: argparse.Namespace) -> int:
    command = args.research_command
    layout = ProjectLayout.from_lean_project(args.project)
    layout.validate_runtime()
    if command == "search":
        _print(
            lean_search(
                layout.root,
                args.query,
                mode=args.mode,
                limit=args.limit,
                include_dependencies=not args.project_only,
            )
        )
        return 0
    if command == "run":
        from aizim.research.runner import run_research

        from .security_probe_command import run_security_probe

        if args.max_seconds is not None and args.max_seconds <= 0:
            raise ResearchError("INVALID_TIME_LIMIT")
        if run_security_probe(layout.root) != 0:
            raise ResearchError("SECURITY_GATE_FAILED")
        result = asyncio.run(
            run_research(layout.root, watch=args.watch, max_seconds=args.max_seconds)
        )
        _print(result)
        return (
            0
            if not any(result.get(name, 0) for name in ("blocked", "pending", "running", "paused"))
            else 5
        )
    if command == "dashboard":
        from aizim.research.dashboard import serve

        serve(layout.root, port=args.port, prices=load_prices(args.prices))
        return 0
    if command == "export":
        from aizim.research.dashboard import exposition

        data = snapshot(layout.root, load_prices(args.prices))
        export_body = (
            exposition(data)
            if args.format == "html"
            else json.dumps(data, ensure_ascii=False, indent=2)
        )
        descriptor = os.open(
            args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(export_body)
        print(f"Exported local research record to {args.output.resolve()}")
        return 0
    action = args.research_action
    if action in {"list", "search"}:
        all_records = object_value(snapshot(layout.root)["records"])
        result = all_records[command]
        if action == "search":
            if type(result) is not list:
                raise ResearchError("INVALID_RESEARCH_STATE")
            items = [
                object_value(item)
                for item in result
                if object_value(item)["task_id"] in {"", args.task_id}
            ]
            result = ranked(args.query, items, args.limit)
        _print(result)
        return 0
    body: dict[str, JsonValue]
    if action in {"pause", "resume"}:
        body = {"task_id": args.task_id, "author": args.author}
    elif command == "task":
        body = {
            "task_id": args.task_id,
            "title": args.title,
            "statement": args.statement,
            "author": args.author,
            "imports": args.imports or ["Std"],
            "depends_on": args.depends_on,
            "source_refs": args.source_ref,
            "max_rounds": args.max_rounds,
            "max_failures": args.max_failures,
            "timeout_seconds": args.timeout_seconds,
        }
    else:
        body = {
            "id": args.id or command + "-" + secrets.token_hex(12),
            "task_id": args.task_id,
            "author": args.author,
            "body": args.body,
        }
        if command == "contribution":
            body.update(role=args.role, source_refs=args.source_ref)
        else:
            body["kind"] = args.kind
            if command == "memory":
                body["source_refs"] = args.source_ref
            if command == "review":
                body["verdict"] = args.verdict
    version = call_control_operation(
        layout, "control.research", {"action": f"{command}.{action}", "body": body}
    )
    _print({"id": body.get("id", body.get("task_id")), "version": version})
    return 0


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))
