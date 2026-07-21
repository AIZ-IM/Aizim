from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .. import __version__
from .doctor_command import run_doctor
from .init_command import run_init
from .state_command import run_state_serve
from .status_command import run_status


def _run_hidden_state(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="aizim state")
    commands = parser.add_subparsers(dest="state_command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--project", type=Path, required=True)
    arguments = parser.parse_args(argv)
    if arguments.state_command == "serve":
        return run_state_serve(arguments.project)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    command_line = tuple(sys.argv[1:] if argv is None else argv)
    if command_line[:1] == ("state",):
        return _run_hidden_state(command_line[1:])
    parser = argparse.ArgumentParser(prog="aizim")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")
    init = commands.add_parser("init")
    init.add_argument("project", type=Path)
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--project", type=Path, default=Path.cwd())
    doctor.add_argument("--json", action="store_true", dest="as_json")
    status = commands.add_parser("status")
    status.add_argument("--project", type=Path, default=Path.cwd())
    status.add_argument("--json", action="store_true", dest="as_json")
    arguments = parser.parse_args(command_line)
    if arguments.command == "init":
        return run_init(arguments.project)
    if arguments.command == "doctor":
        return run_doctor(arguments.project, arguments.as_json)
    if arguments.command == "status":
        return run_status(arguments.project, arguments.as_json)
    return 0
