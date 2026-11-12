"""Run the versioned Lean ladder: python -m aizim.research.benchmark --help."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from aizim import __version__
from aizim.agents import AgentBackend
from aizim.agents.omp_backend import OmpAgentBackend, OmpBackendDependencies, preflight_omp_worker
from aizim.agents.platform_sandbox import sandbox_adapter
from aizim.cli.init_command import run_init
from aizim.config import LEAN_LSP_MCP_VERSION, LEANCLIENT_VERSION, MCP_VERSION
from aizim.config.model import LeanRuntimeMode, ResourcePolicy
from aizim.domain import sha256_file, sha256_json
from aizim.domain.serialization import JsonValue
from aizim.omp.executable import resolve_omp
from aizim.orchestration.resources import ResourceGovernor
from aizim.orchestration.worker_host import WorkerExecutionHost
from aizim.security_gate import run_security_gate
from aizim.state import StateService, StateServiceConfig

from .benchmark_backend import CallMeter, MeasuredBackend, ScriptedLadderBackend
from .benchmark_ladder import BenchmarkLadder, load_ladder, task_parameters
from .benchmark_metrics import summarize_metrics
from .engine import (
    RESEARCH_GATEWAY_LIMITS,
    LeanExecutor,
    ResearchEngine,
    reason_code,
    verified_for_worker,
)
from .records import ResearchError, define_task, object_value, records, save, text
from .report import estimate, load_prices


@dataclass(frozen=True, slots=True)
class BenchmarkOptions:
    backend: Literal["scripted", "omp"] = "scripted"
    workers: int = 1
    max_rounds: int = 2
    timeout_seconds: int = 180
    call_budget: int = 400
    max_seconds: int = 1800

    def __post_init__(self) -> None:
        bounds = (
            (self.workers, 64), (self.max_rounds, 1000),
            (self.timeout_seconds, 14400), (self.call_budget, 10000),
            (self.max_seconds, 86400),
        )
        if self.backend not in {"scripted", "omp"} or any(
            type(value) is not int or not 1 <= value <= maximum for value, maximum in bounds
        ):
            raise ResearchError("INVALID_BENCHMARK_CONFIGURATION")


def create_project(output_root: Path, ladder: BenchmarkLadder) -> tuple[Path, Path]:
    """Always reserve a fresh private child; never accept an existing project as input."""
    output_root.mkdir(parents=True, exist_ok=True)
    run_root = Path(tempfile.mkdtemp(prefix="benchmark-", dir=output_root)).resolve()
    project = run_root / "lean-project"
    project.mkdir(mode=0o700)
    for name, content in ladder.project_files:
        with (project / name).open("x") as stream:
            stream.write(content)
    return run_root, project


def configuration(ladder: BenchmarkLadder, options: BenchmarkOptions) -> dict[str, JsonValue]:
    source_root = Path(__file__).parents[1]
    # Relative names and content cover the installed runtime without depending on a Git checkout.
    sources = {
        path.relative_to(source_root).as_posix(): sha256_file(path)
        for path in source_root.rglob("*")
        if path.is_file()
        and path.suffix not in {".pyc", ".pyo"}
        and not any(
            part == "__pycache__" or part.startswith(".")
            for part in path.relative_to(source_root).parts
        )
    }
    return {
        "backend": options.backend,
        "backend_identity": None,
        "model": None,
        "workers": options.workers,
        "max_rounds": options.max_rounds,
        "max_failures": options.max_rounds,
        "timeout_seconds": options.timeout_seconds,
        "call_budget": options.call_budget,
        "max_seconds": options.max_seconds,
        "ladder_hash": ladder.content_hash,
        "lean_toolchain": ladder.lean_toolchain,
        "aizim_version": __version__,
        "lean_lsp_mcp_version": LEAN_LSP_MCP_VERSION,
        "leanclient_version": LEANCLIENT_VERSION,
        "mcp_version": MCP_VERSION,
        "implementation_hash": sha256_json(sources),
    }


async def run_benchmark(
    output_root: Path, options: BenchmarkOptions,
    *, prices: dict[str, JsonValue] | None = None,
) -> tuple[Path, dict[str, JsonValue]]:
    ladder = load_ladder()
    config = configuration(ladder, options)
    config["prices_per_million"] = prices
    run_root, project = create_project(output_root, ladder)
    run_id = "benchmark-" + run_root.name.removeprefix("benchmark-")
    started = time.monotonic()
    reason, error_code = "", ""
    tasks: list[dict[str, JsonValue]] = []
    attempts: list[dict[str, JsonValue]] = []
    verified: dict[str, list[str]] = {}
    meter: CallMeter | None = None
    try:
        if run_init(project) != 0:
            raise ResearchError("BENCHMARK_INITIALIZATION_FAILED")
        if options.backend == "omp":
            try:
                security = await run_security_gate(project)
            except Exception:  # BROAD_EXCEPT_OK: same fail-closed gate as research run
                raise ResearchError("SECURITY_GATE_FAILED") from None
            if not security.passed:
                raise ResearchError("SECURITY_GATE_FAILED")
        with StateService(StateServiceConfig(project, run_id)) as state:
            host: WorkerExecutionHost | None = None
            meter = CallMeter(state, run_id)
            run_record: dict[str, JsonValue] = {
                "id": run_id, "status": "running", "model": "pending",
                "participation": "autonomous", "started_at": _now(),
                "finished_at": "", "reason": "",
            }
            try:
                for target in ladder.targets:
                    define_task(state, task_parameters(
                        target, max_rounds=options.max_rounds,
                        timeout_seconds=options.timeout_seconds, call_budget=options.call_budget,
                    ))
                backend: AgentBackend | None = None
                if options.backend == "omp":
                    environment = dict(os.environ)
                    executable = resolve_omp(environment)
                    backend = OmpAgentBackend(OmpBackendDependencies(
                        executable, sandbox_adapter(executable.path).compile,
                        environment, connect=meter.connect,
                    ))
                    model = await preflight_omp_worker(project, environment)
                    identity = backend.identity
                else:
                    model = "deterministic-tool-actions"
                    identity = ScriptedLadderBackend(ladder, ladder.targets[0], meter).identity
                config["model"] = model
                config["backend_identity"] = {
                    "name": identity.name, "version": identity.version,
                    "executable_sha256": identity.executable_sha256,
                }
                run_record["model"] = model
                save(state, "run", run_id, run_record, actor="research_supervisor", run_id=run_id)
                governor = ResourceGovernor(
                    ResourcePolicy(
                        max_proof_workers=options.workers, scratch_slots=options.workers,
                    ),
                    disk_free=lambda path: shutil.disk_usage(path).free,
                )
                governor.validate(project, LeanRuntimeMode.SHARED)
                host = await WorkerExecutionHost.open(
                    state, project, project, governor, run_id, continue_on_failure=True,
                    gateway_limits=RESEARCH_GATEWAY_LIMITS,
                )
                await host.prewarm()

                async def execute(task, attempt, context):
                    selected = backend or ScriptedLadderBackend(
                        ladder, ladder.target(text(task["task_id"])), meter,
                    )
                    executor = LeanExecutor(state, project, host, MeasuredBackend(selected, meter))
                    return await executor(task, attempt, context)

                engine = ResearchEngine(state, execute, run_id, model, workers=options.workers)
                try:
                    async with asyncio.timeout(options.max_seconds):
                        await engine.run()
                    reason = engine.reason
                except TimeoutError:
                    engine.request_stop()
                    reason = "time_limit"
            except Exception as error:  # BROAD_EXCEPT_OK: retain metrics for failed prerequisites
                reason = "infrastructure_unavailable"
                error_code = reason_code(error) or type(error).__name__
            finally:
                try:
                    if host is not None:
                        await host.aclose()
                except Exception as error:  # BROAD_EXCEPT_OK: cleanup failure must retain metrics
                    reason = "infrastructure_unavailable"
                    error_code = reason_code(error) or type(error).__name__
                tasks, attempts = records(state, "task"), records(state, "attempt")
                for task in tasks:
                    declarations: list[str] = []
                    for attempt in attempts:
                        if attempt["task_id"] == task["task_id"]:
                            declarations.extend(verified_for_worker(
                                state, run_id, text(attempt["worker_id"]), task, attempt, project,
                            ))
                    verified[text(task["task_id"])] = list(dict.fromkeys(declarations))
                run_record.update({
                    "status": "interrupted" if reason else "finished",
                    "finished_at": _now(), "reason": reason,
                })
                save(state, "run", run_id, run_record, actor="research_supervisor", run_id=run_id)
    except Exception as error:  # BROAD_EXCEPT_OK: initialization/state startup boundary
        reason = "infrastructure_unavailable"
        error_code = reason_code(error) or type(error).__name__
    metrics = summarize_metrics(
        ladder=ladder, configuration=config, run_id=run_id, tasks=tasks, attempts=attempts,
        calls={} if meter is None else meter.workers, verified=verified,
        wall_seconds=time.monotonic() - started,
        reason=reason, error_code=error_code, prices=prices,
    )
    destination = run_root / "metrics.json"
    with destination.open("x") as stream:
        json.dump(metrics, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    return destination, metrics


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--backend", choices=("scripted", "omp"), default="scripted")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--max-rounds", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--call-budget", type=int, default=400)
    parser.add_argument("--max-seconds", type=int, default=1800)
    parser.add_argument("--prices", type=Path, help="Model price table in research export format")
    args = parser.parse_args(argv)
    try:
        options = BenchmarkOptions(
            args.backend, args.workers, args.max_rounds, args.timeout_seconds,
            args.call_budget, args.max_seconds,
        )
        prices = load_prices(args.prices)
        if prices is not None:
            for rate in prices.values():
                estimate(
                    {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0},
                    object_value(rate),
                )
    except (ResearchError, OSError, ValueError):
        parser.error("invalid benchmark options or price table")
    destination, metrics = asyncio.run(run_benchmark(args.output_dir, options, prices=prices))
    print(f"Metrics: {destination}")
    print(f"Verified: {metrics['verified_targets']}/{metrics['targets']}")
    if metrics["reason"]:
        print(f"Reason: {metrics['reason']}")
        return 2
    return 0 if metrics["verified_targets"] == metrics["targets"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
