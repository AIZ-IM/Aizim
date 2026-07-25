from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from aizim.domain import ControllerProviderId
from aizim.runtime.provider_executables import (
    ResolvedExecutable,
    resolve_claude,
    resolve_codex,
)

from .claude_controller import ClaudeControllerBackend
from .codex_controller import CodexControllerBackend
from .controller_backend import ControllerBackend


@dataclass(frozen=True, slots=True)
class ControllerProviderRegistryError(RuntimeError):
    code: str

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True, slots=True)
class ResolvedControllerRuntime:
    provider: ControllerProviderId
    codex: ResolvedExecutable
    controller: ResolvedExecutable


type ControllerBackendFactory = Callable[
    [ResolvedControllerRuntime, Path, str | None, Mapping[str, str]],
    ControllerBackend,
]


@dataclass(frozen=True, slots=True)
class ControllerAdapter:
    provider: ControllerProviderId
    resolve: Callable[[Mapping[str, str]], ResolvedExecutable]
    backend: ControllerBackendFactory


def _codex_backend(
    runtime: ResolvedControllerRuntime,
    project: Path,
    model: str | None,
    environ: Mapping[str, str],
) -> ControllerBackend:
    return CodexControllerBackend(runtime.controller, model, project, environ)


def _claude_backend(
    runtime: ResolvedControllerRuntime,
    project: Path,
    model: str | None,
    environ: Mapping[str, str],
) -> ControllerBackend:
    return ClaudeControllerBackend(
        runtime.controller,
        runtime.codex,
        model,
        project,
        environ,
    )


_ADAPTERS = MappingProxyType(
    {
        "codex": ControllerAdapter(
            ControllerProviderId("codex"),
            resolve_codex,
            _codex_backend,
        ),
        "claude": ControllerAdapter(
            ControllerProviderId("claude"),
            resolve_claude,
            _claude_backend,
        ),
    }
)


def controller_provider_ids() -> tuple[str, ...]:
    return tuple(sorted(_ADAPTERS))


def parse_controller_provider(value: object) -> ControllerProviderId:
    if type(value) is not str:
        raise ControllerProviderRegistryError("CONTROLLER_PROVIDER_INVALID")
    try:
        return ControllerProviderId(value)
    except ValueError:
        raise ControllerProviderRegistryError("CONTROLLER_PROVIDER_INVALID") from None


def controller_adapter(provider: ControllerProviderId) -> ControllerAdapter:
    if type(provider) is not ControllerProviderId:
        raise ControllerProviderRegistryError("CONTROLLER_PROVIDER_INVALID")
    try:
        return _ADAPTERS[provider.value]
    except KeyError:
        raise ControllerProviderRegistryError("CONTROLLER_PROVIDER_UNSUPPORTED") from None


def resolve_controller_runtime(
    provider: ControllerProviderId,
    environ: Mapping[str, str],
) -> ResolvedControllerRuntime:
    codex = resolve_codex(environ)
    adapter = controller_adapter(provider)
    controller = codex if provider.value == "codex" else adapter.resolve(environ)
    return ResolvedControllerRuntime(provider, codex, controller)


def build_controller_backend(
    runtime: ResolvedControllerRuntime,
    project: Path,
    model: str | None,
    environ: Mapping[str, str],
) -> ControllerBackend:
    return controller_adapter(runtime.provider).backend(runtime, project, model, environ)


def production_controller(
    project: Path | None,
    provider: ControllerProviderId,
    model: str | None,
) -> ControllerBackend:
    if project is None:
        raise ControllerProviderRegistryError("CONTROLLER_BACKEND_UNAVAILABLE")
    environment = dict(os.environ)
    runtime = resolve_controller_runtime(provider, environment)
    return build_controller_backend(runtime, project, model, environment)
