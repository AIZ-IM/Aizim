from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from .. import __version__
from ..domain import AgentRole
from ..gateway.sidecar import run_gateway_sidecar, scrub_session_arguments
from ..orchestration.control_plane import ControllerProvider
from ..orchestration.runner import run_autonomous_shared
from .control_command import (
    run_controller_configure,
    run_controller_show,
    run_worker_assign,
    run_worker_list,
    run_worker_register,
)
from .doctor_command import run_doctor
from .init_command import run_init
from .security_probe_command import run_security_probe
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


def _run_hidden_gateway(command_line: list[str], process_argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(prog="aizim gateway")
    commands = parser.add_subparsers(dest="gateway_command", required=True)
    sidecar = commands.add_parser("sidecar")
    sidecar.add_argument("--broker-socket", type=Path, required=True)
    sidecar.add_argument("--session-id", required=True)
    arguments = parser.parse_args(command_line[1:])
    session_ids = [arguments.session_id]
    arguments.session_id = ""
    scrub_session_arguments(command_line)
    if process_argv is not None:
        scrub_session_arguments(process_argv)
    return run_gateway_sidecar(arguments.broker_socket, session_ids.pop())


def main(argv: Sequence[str] | None = None) -> int:
    process_argv = sys.argv if argv is None else None
    command_line = list(sys.argv[1:] if argv is None else argv)
    argv = None
    if command_line[:1] == ["state"]:
        return _run_hidden_state(command_line[1:])
    if command_line[:1] == ["gateway"]:
        return _run_hidden_gateway(command_line, process_argv)
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
    controller = commands.add_parser("controller")
    controller_commands = controller.add_subparsers(
        dest="controller_command", required=True
    )
    configure = controller_commands.add_parser("configure")
    configure.add_argument("--project", type=Path, required=True)
    configure.add_argument(
        "--provider", type=ControllerProvider, choices=tuple(ControllerProvider), required=True
    )
    configure.add_argument("--model")
    show = controller_commands.add_parser("show")
    show.add_argument("--project", type=Path, default=Path.cwd())
    show.add_argument("--json", action="store_true", dest="as_json")
    worker = commands.add_parser("worker")
    worker_commands = worker.add_subparsers(dest="worker_command", required=True)
    register = worker_commands.add_parser("register")
    register.add_argument("--project", type=Path, required=True)
    register.add_argument("--worker-id", required=True)
    register.add_argument("--role", type=AgentRole, choices=tuple(AgentRole), required=True)
    assign = worker_commands.add_parser("assign")
    assign.add_argument("--project", type=Path, required=True)
    assign.add_argument("--worker-id", required=True)
    assign.add_argument("--task", required=True)
    list_workers = worker_commands.add_parser("list")
    list_workers.add_argument("--project", type=Path, default=Path.cwd())
    list_workers.add_argument("--json", action="store_true", dest="as_json")
    run = commands.add_parser("run")
    run.add_argument("--project", type=Path, required=True)
    run.add_argument("--profile", choices=("autonomous-shared",), required=True)
    run.add_argument("--backend", choices=("fake", "codex"), required=True)
    security_probe = commands.add_parser("security-probe")
    security_probe.add_argument("--project", type=Path, required=True)
    security_probe.add_argument("--backend", choices=("codex",), required=True)
    security_probe.add_argument("--no-model", action="store_true", required=True)
    arguments = parser.parse_args(command_line)
    if arguments.command == "init":
        return run_init(arguments.project)
    if arguments.command == "doctor":
        return run_doctor(arguments.project, arguments.as_json)
    if arguments.command == "status":
        return run_status(arguments.project, arguments.as_json)
    if arguments.command == "controller":
        if arguments.controller_command == "configure":
            return run_controller_configure(
                arguments.project, arguments.provider, arguments.model
            )
        if arguments.controller_command == "show":
            return run_controller_show(arguments.project, arguments.as_json)
    if arguments.command == "worker":
        if arguments.worker_command == "register":
            return run_worker_register(
                arguments.project, arguments.worker_id, arguments.role
            )
        if arguments.worker_command == "assign":
            return run_worker_assign(
                arguments.project, arguments.worker_id, arguments.task
            )
        if arguments.worker_command == "list":
            return run_worker_list(arguments.project, arguments.as_json)
    if arguments.command == "run":
        return run_autonomous_shared(arguments.project, arguments.backend)
    if arguments.command == "security-probe":
        return run_security_probe(arguments.project)
    return 0
