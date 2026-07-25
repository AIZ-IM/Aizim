from __future__ import annotations

import asyncio  # noqa: ANYIO_OK - doctor is a synchronous CLI boundary
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from aizim.agents.linux_sandbox import LinuxSandboxAdapter
from aizim.domain import ControllerProviderId
from aizim.domain.serialization import JsonValue
from aizim.orchestration.controller_providers import (
    ControllerProviderRegistryError,
    ResolvedControllerRuntime,
    build_controller_backend,
    controller_adapter,
    parse_controller_provider,
)
from aizim.runtime.layout import ProjectLayout
from aizim.runtime.provider_executables import (
    ProviderExecutableError,
    ResolvedExecutable,
    resolve_claude,
    resolve_codex,
)

from .state_client import StateClientError, load_projections

type DoctorStatus = Literal["PASS", "FAIL", "SKIP"]


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    id: str
    status: DoctorStatus
    detail: str


@dataclass(frozen=True, slots=True)
class _ControllerSelection:
    provider: ControllerProviderId
    model: str | None


def provider_checks(
    layout: ProjectLayout,
    environ: Mapping[str, str],
) -> tuple[DoctorCheck, ...]:
    environment = dict(environ)
    selection, configuration, provider = _controller_selection(layout)
    codex, worker = _worker_codex(environment)
    sandbox = (
        _skip("sandbox_exec", "worker Codex prerequisite failed")
        if codex is None
        else _sandbox_check(codex.path)
    )
    controller, executable = _controller_executable(
        selection,
        provider,
        codex,
        worker,
        environment,
    )
    authentication = _controller_auth(
        selection,
        controller,
        codex,
        layout,
        environment,
    )
    return configuration, provider, executable, authentication, worker, sandbox


def _controller_selection(
    layout: ProjectLayout,
) -> tuple[_ControllerSelection | None, DoctorCheck, DoctorCheck]:
    try:
        records = load_projections(layout)
    except StateClientError:
        return (
            None,
            DoctorCheck(
                "controller_configuration",
                "FAIL",
                "controller configuration is unavailable",
            ),
            _skip("controller_provider", "controller configuration failed"),
        )
    matches = tuple(
        record
        for record in records
        if (record.projection_name, record.entity_id) == ("controller", "primary")
    )
    if not matches:
        return (
            None,
            DoctorCheck(
                "controller_configuration",
                "FAIL",
                "CONTROLLER_NOT_CONFIGURED",
            ),
            _skip("controller_provider", "controller is not configured"),
        )
    configuration = DoctorCheck(
        "controller_configuration",
        "PASS",
        "persisted controller configuration",
    )
    if len(matches) != 1:
        return (
            None,
            configuration,
            DoctorCheck(
                "controller_provider",
                "FAIL",
                "CONTROLLER_PROVIDER_INVALID",
            ),
        )
    state = matches[0].state
    payload = state.get("payload")
    provider_value = payload.get("provider") if type(payload) is dict else None
    model_value: JsonValue | None = payload.get("model") if type(payload) is dict else None
    try:
        provider = parse_controller_provider(provider_value)
    except ControllerProviderRegistryError as error:
        return None, configuration, DoctorCheck("controller_provider", "FAIL", error.code)
    if model_value is not None and (type(model_value) is not str or not model_value):
        return (
            None,
            configuration,
            DoctorCheck(
                "controller_provider",
                "FAIL",
                "CONTROLLER_MODEL_INVALID",
            ),
        )
    try:
        controller_adapter(provider)
    except ControllerProviderRegistryError as error:
        return None, configuration, DoctorCheck("controller_provider", "FAIL", error.code)
    return (
        _ControllerSelection(provider, model_value),
        configuration,
        DoctorCheck("controller_provider", "PASS", f"provider={provider.value}"),
    )


def _worker_codex(
    environ: Mapping[str, str],
) -> tuple[ResolvedExecutable | None, DoctorCheck]:
    try:
        executable = resolve_codex(environ)
    except ProviderExecutableError as error:
        return None, DoctorCheck("worker_codex", "FAIL", error.code)
    return executable, DoctorCheck("worker_codex", "PASS", _descriptor_detail(executable))


def _controller_executable(
    selection: _ControllerSelection | None,
    provider: DoctorCheck,
    codex: ResolvedExecutable | None,
    worker: DoctorCheck,
    environ: Mapping[str, str],
) -> tuple[ResolvedExecutable | None, DoctorCheck]:
    if selection is None or provider.status != "PASS":
        return None, _skip("controller_executable", "controller provider prerequisite failed")
    if selection.provider.value == "codex":
        if codex is None:
            return None, DoctorCheck(
                "controller_executable",
                "FAIL",
                worker.detail,
            )
        return codex, DoctorCheck(
            "controller_executable",
            "PASS",
            _descriptor_detail(codex),
        )
    try:
        executable = resolve_claude(environ)
    except ProviderExecutableError as error:
        return None, DoctorCheck("controller_executable", "FAIL", error.code)
    return executable, DoctorCheck(
        "controller_executable",
        "PASS",
        _descriptor_detail(executable),
    )


def _controller_auth(
    selection: _ControllerSelection | None,
    controller: ResolvedExecutable | None,
    codex: ResolvedExecutable | None,
    layout: ProjectLayout,
    environ: Mapping[str, str],
) -> DoctorCheck:
    if selection is None or controller is None:
        return _skip("controller_auth", "controller executable prerequisite failed")
    if codex is None:
        return _skip("controller_auth", "Codex sandbox prerequisite failed")
    runtime = ResolvedControllerRuntime(selection.provider, codex, controller)
    try:
        backend = build_controller_backend(runtime, layout.root, selection.model, environ)
        asyncio.run(backend.preflight())
    except Exception:  # noqa: BROAD_EXCEPT_OK - readiness boundary emits no provider output
        return DoctorCheck("controller_auth", "FAIL", "controller authentication failed")
    return DoctorCheck(
        "controller_auth",
        "PASS",
        f"{selection.provider.value} authentication ready",
    )


def _descriptor_detail(executable: ResolvedExecutable) -> str:
    return f"path={executable.path} version={executable.version} sha256={executable.sha256}"


def _skip(identifier: str, detail: str) -> DoctorCheck:
    return DoctorCheck(identifier, "SKIP", detail)


def _sandbox_check(
    executable: Path,
    platform: str | None = None,
) -> DoctorCheck:
    host = sys.platform if platform is None else platform
    if host == "darwin":
        sandbox = Path("/usr/bin/sandbox-exec")
        passed = sandbox.is_file() and os.access(sandbox, os.X_OK)
        return DoctorCheck(
            "sandbox_exec",
            "PASS" if passed else "FAIL",
            "sandbox-exec available" if passed else "sandbox mechanism is unavailable",
        )
    if host == "linux":
        try:
            LinuxSandboxAdapter.for_executable(executable).validate_host()
        except (OSError, RuntimeError, ValueError):
            return DoctorCheck("sandbox_exec", "FAIL", "sandbox mechanism is unavailable")
        return DoctorCheck("sandbox_exec", "PASS", "Codex Linux sandbox available")
    return DoctorCheck("sandbox_exec", "FAIL", "sandbox mechanism is unavailable")
