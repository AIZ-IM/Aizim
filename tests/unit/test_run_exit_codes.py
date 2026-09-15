from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest

from aizim.agents import CodexBackend
from aizim.agents.codex_backend import CodexBackendError
from aizim.agents.macos_sandbox import SandboxHostError
from aizim.config.model import ConfigError
from aizim.lean.models import DocumentBrokerError
from aizim.orchestration import runner
from aizim.orchestration.codex_worker import CodexWorkerError
from aizim.orchestration.promotion_consumer import PromotionConsumerError
from aizim.orchestration.resources import ResourcePolicyError
from aizim.orchestration.runner import RunFailureCategory, _run_exit_code
from aizim.orchestration.worker import WorkerExecutionError
from aizim.runtime.provider_executables import ResolvedExecutable
from aizim.state.service import StateServiceLifecycleError


@pytest.mark.parametrize(
    ("error", "category"),
    (
        (ConfigError("config", "INVALID_CONFIG"), RunFailureCategory.CONFIGURATION),
        (ResourcePolicyError("DISK_FLOOR"), RunFailureCategory.READINESS),
        (CodexWorkerError("CODEX_EXECUTABLE_UNAVAILABLE"), RunFailureCategory.READINESS),
        (CodexBackendError("UNSUPPORTED_CODEX_VERSION"), RunFailureCategory.READINESS),
        (SandboxHostError("unsupported Codex CLI"), RunFailureCategory.READINESS),
        (StateServiceLifecycleError("state service unavailable"), RunFailureCategory.READINESS),
        (DocumentBrokerError("EPOCH_MISMATCH"), RunFailureCategory.AUTHORIZATION),
        (PromotionConsumerError("EPOCH_PROJECT_MISMATCH"), RunFailureCategory.AUTHORIZATION),
        (PromotionConsumerError("LEAN_VERIFICATION_FAILED"), RunFailureCategory.LEAN_VERIFICATION),
        (PromotionConsumerError("PROMOTION_FAILED"), RunFailureCategory.RUNTIME),
        (PromotionConsumerError("RUNTIME_FAILURE"), RunFailureCategory.RUNTIME),
        (WorkerExecutionError("BACKEND_FAILED"), RunFailureCategory.RUNTIME),
        (RuntimeError("UNEXPECTED"), RunFailureCategory.RUNTIME),
        (asyncio.CancelledError(), RunFailureCategory.RUNTIME),
        (KeyboardInterrupt(), RunFailureCategory.RUNTIME),
    ),
)
def test_run_failures_map_to_the_fixed_cli_contract(
    error: BaseException, category: RunFailureCategory
) -> None:
    assert _run_exit_code(error) == category


@pytest.mark.parametrize(
    ("error", "category"),
    (
        (CodexBackendError("UNSUPPORTED_CODEX_VERSION"), RunFailureCategory.READINESS),
        (SandboxHostError("unsupported Codex CLI"), RunFailureCategory.READINESS),
        (StateServiceLifecycleError("state service unavailable"), RunFailureCategory.READINESS),
        (asyncio.CancelledError(), RunFailureCategory.RUNTIME),
    ),
)
def test_synchronous_run_boundary_returns_the_mapped_failure_code(
    error: BaseException,
    category: RunFailureCategory,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fail(_project: Path, _backend: str):
        raise error

    monkeypatch.setattr(runner, "_run", fail)

    assert runner.run_autonomous_shared(Path("."), "codex") == category
    assert capsys.readouterr().err == "aizim run: autonomous-shared run failed\n"


def test_direct_codex_run_passes_the_single_resolved_descriptor_to_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = ResolvedExecutable(
        Path("/opt/aizim-test/codex"),
        "codex-cli 0.154.0",
        "a" * 64,
    )
    backend = cast(CodexBackend, object())
    resolutions: list[dict[str, str]] = []
    constructions: list[tuple[ResolvedExecutable, dict[str, str]]] = []

    def resolve(environment: dict[str, str]) -> ResolvedExecutable:
        resolutions.append(environment)
        if len(resolutions) > 1:
            raise AssertionError("ambient Codex was resolved more than once")
        return descriptor

    def create(
        executable: ResolvedExecutable,
        environment: dict[str, str],
    ) -> CodexBackend:
        constructions.append((executable, environment))
        return backend

    monkeypatch.setattr(runner, "resolve_codex", resolve)
    monkeypatch.setattr(runner, "create_codex_backend", create)
    environment = {"PATH": "/external/bin"}

    assert runner._create_external_codex_backend(environment) is backend
    assert resolutions == [environment]
    assert constructions == [(descriptor, environment)]
