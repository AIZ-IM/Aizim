from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from aizim.agents import AgentRequest
from aizim.domain import AgentRole, sha256_json
from aizim.domain.serialization import JsonValue
from aizim.gateway.capabilities import GatewayFailure, GatewaySuccess, GatewayTool
from aizim.research import benchmark
from aizim.research.benchmark import BenchmarkOptions, configuration, create_project, run_benchmark
from aizim.research.benchmark_backend import CallCounts, CallMeter, ScriptedLadderBackend
from aizim.research.benchmark_ladder import load_ladder
from aizim.research.benchmark_metrics import summarize_metrics
from aizim.research.records import ResearchError, object_value


def _attempt(
    identity: str, task_id: str, *, charged: bool = True, infrastructure: bool = False,
    usage: JsonValue = None,
) -> dict[str, JsonValue]:
    return {
        "id": identity, "task_id": task_id, "worker_id": identity,
        "status": "failed" if infrastructure else "verified",
        "charged": charged, "infrastructure_failure": infrastructure,
        "model": "test/model", "usage": usage,
    }


def _metrics(
    *,
    attempts: list[dict[str, JsonValue]] | None = None,
    calls: dict[str, CallCounts] | None = None,
    verified: dict[str, list[str]] | None = None,
    error_code: str = "",
    prices: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    ladder = load_ladder()
    return summarize_metrics(
        ladder=ladder,
        configuration=configuration(ladder, BenchmarkOptions()),
        run_id="test-run",
        tasks=[],
        attempts=[] if attempts is None else attempts,
        calls={} if calls is None else calls,
        verified={} if verified is None else verified,
        wall_seconds=1.5,
        reason="",
        error_code=error_code,
        prices=prices,
    )


def test_ladder_is_fixed_ordered_and_hashes_project_and_proofs() -> None:
    ladder = load_ladder()
    assert ladder.version == "research-ladder-v1"
    assert len(ladder.targets) == 7
    assert ladder.targets[0].task_id == "nat_add_zero"
    assert ladder.targets[-1].depends_on == ("project_twice", "nat_add_zero")
    assert ladder.content_hash == load_ladder().content_hash
    seen: set[str] = set()
    for target in ladder.targets:
        assert set(target.depends_on) <= seen
        for dependency in target.depends_on:
            assert "{" + dependency + "}" in target.proof
        seen.add(target.task_id)
    config = configuration(ladder, BenchmarkOptions())
    assert sha256_json(config) != sha256_json(configuration(
        ladder, replace(BenchmarkOptions(), call_budget=401),
    ))
    assert sha256_json(config) != sha256_json(configuration(
        replace(ladder, content_hash="f" * 64), BenchmarkOptions(),
    ))


def test_new_projects_preserve_existing_output_and_previous_runs(tmp_path: Path) -> None:
    existing = tmp_path / "user-project.lean"
    existing.write_text("theorem untouched : True := by trivial\n")
    ladder = load_ladder()
    first, project = create_project(tmp_path, ladder)
    second, other = create_project(tmp_path, ladder)
    assert first != second and project != other
    assert first.parent == second.parent == tmp_path
    assert existing.read_text() == "theorem untouched : True := by trivial\n"
    assert (project / "BenchmarkLab.lean").read_bytes() == (
        other / "BenchmarkLab.lean").read_bytes()
    assert (project / "lean-toolchain").read_text().strip() == ladder.lean_toolchain


def test_metrics_charge_rounds_separately_and_include_failed_work() -> None:
    attempts = [
        _attempt("a", "nat_add_zero"),
        _attempt("b", "nat_zero_add", charged=False, infrastructure=True),
        _attempt("c", "nat_zero_add"),
    ]
    metrics = _metrics(
        calls={"a": CallCounts(5, 5), "b": CallCounts(2, 1), "c": CallCounts(5, 5)},
        attempts=attempts,
        verified={"nat_add_zero": ["declaration-a"], "nat_zero_add": ["declaration-c"]},
    )
    assert metrics["verified_targets"] == 2
    assert metrics["verify_rate"] == 2 / 7
    assert metrics["charged_rounds"] == 2
    assert metrics["charged_rounds_per_verified_target"] == 1
    assert metrics["charged_calls_per_verified_target"] == 5.5
    assert metrics["gateway_requests"] == 12
    assert metrics["infrastructure_failure_rate"] == 1 / 3
    assert metrics["configuration_hash"] == sha256_json(object_value(metrics["configuration"]))


def test_missing_measurements_remain_unknown_and_zero_denominators_are_null() -> None:
    metrics = _metrics(attempts=[_attempt("a", "nat_add_zero")], calls={"a": CallCounts(1, 0, 1)})
    assert metrics["verify_rate"] == 0
    assert metrics["charged_rounds_per_verified_target"] is None
    assert metrics["charged_calls"] is None
    assert metrics["charged_calls_per_verified_target"] is None
    usage = object_value(metrics["usage"])
    assert usage["unknown_stages"] == 1
    assert object_value(usage["total_tokens"])["input_tokens"] is None
    assert usage["known_cost_usd"] is None
    assert usage["estimated_cost_usd"] is None
    assert _metrics()["infrastructure_failure_rate"] is None


def test_measured_tokens_and_partial_prices_never_claim_a_total_cost() -> None:
    measured: dict[str, JsonValue] = {
        "input_tokens": 1000, "cached_input_tokens": 100, "output_tokens": 200,
    }
    prices: dict[str, JsonValue] = {"test/model": {
        "input_per_million": "2", "cached_input_per_million": "1", "output_per_million": "4",
    }}
    metrics = _metrics(attempts=[_attempt("a", "nat_add_zero", usage=measured)], prices=prices)
    usage = object_value(metrics["usage"])
    assert usage["estimated_cost_usd"] == "0.002700"
    assert object_value(usage["total_tokens"])["input_tokens"] == 1000
    partial = _metrics(attempts=[
        _attempt("a", "nat_add_zero", usage=measured), _attempt("b", "nat_zero_add"),
    ], prices=prices)
    usage = object_value(partial["usage"])
    assert usage["known_cost_usd"] == "0.002700"
    assert usage["estimated_cost_usd"] is None
    unpriced = object_value(_metrics(
        attempts=[_attempt("a", "nat_add_zero", usage=measured)],
    )["usage"])
    assert unpriced["estimated_cost_usd"] is None
    assert unpriced["unpriced_measured_stages"] == 1


def _request(tmp_path: Path, statement: str, facts: list[JsonValue]) -> AgentRequest:
    contract = {
        "statement": statement,
        "imports": ["Std", "BenchmarkLab.Research.Published"],
        "available_facts": facts,
    }
    return AgentRequest(
        "run", "worker", AgentRole.PROOF_EXPLORER, "worker prompt\n" + json.dumps(contract),
        None, tmp_path, tmp_path, "session", tmp_path / "gateway.sock", 60,
        {"document_id": "doc", "candidate_name": "candidate", "contribution_id": "contribution",
         "knowledge_epoch": 1},
    )


async def test_scripted_worker_reuses_verified_names_and_only_calls_gateway(
    tmp_path: Path,
) -> None:
    actions: list[tuple[GatewayTool, dict[str, JsonValue]]] = []

    class Channel:
        role = AgentRole.PROOF_EXPLORER

        async def call(self, operation, payload):
            actions.append((operation, payload))
            return GatewaySuccess({})

        async def aclose(self):
            pass

    async def connect(*_args):
        return Channel()

    ladder = load_ladder()
    target = ladder.target("identity_reuse")
    meter = cast(CallMeter, SimpleNamespace(connect=connect))
    backend = ScriptedLadderBackend(ladder, target, meter)
    name = "BenchmarkLab.Research.ActualPublishedIdentity"
    result = await backend.run(_request(tmp_path, target.statement, [
        {"name": name, "type": ladder.target("nat_add_zero").statement},
    ]))
    assert result.status == "submitted"
    assert result.usage is None
    assert [operation for operation, _ in actions] == [
        GatewayTool.LEAN_GOAL, GatewayTool.LEAN_MULTI_ATTEMPT, GatewayTool.DOCUMENT_APPLY,
        GatewayTool.CONTRIBUTION_SUBMIT, GatewayTool.KNOWLEDGE_READ,
    ]
    assert name in str(actions[1][1]["snippets"])
    submission = actions[3][1]
    assert submission["dependencies"] == [name]
    assert submission["complete_type"] == target.statement
    assert submission["candidate_name"] == "candidate"
    assert actions[0][1]["line"] == 5


async def test_missing_dependency_and_changed_target_cannot_submit(tmp_path: Path) -> None:
    ladder = load_ladder()
    target = ladder.target("identity_reuse")
    backend = ScriptedLadderBackend(ladder, target, cast(CallMeter, None))
    with pytest.raises(ResearchError, match="BENCHMARK_DEPENDENCY_MISSING"):
        await backend.run(_request(tmp_path, target.statement, []))
    with pytest.raises(ResearchError, match="BENCHMARK_TARGET_MISMATCH"):
        await backend.run(_request(tmp_path, "True", []))


@pytest.mark.parametrize("reason,charged", [
    ("REQUEST_RATE_EXCEEDED", False), ("WORKER_BUDGET_EXHAUSTED", False),
    ("RUN_BUDGET_EXHAUSTED", False), ("UNVERIFIED_DOCUMENT_APPLY", True),
    ("TOKEN_EXPIRED", True),
])
def test_call_meter_distinguishes_budget_charges_from_retry_denials(reason, charged) -> None:
    from aizim.gateway.capabilities import GatewayError
    from aizim.research.records import ResearchStore

    state = cast(ResearchStore, SimpleNamespace(query_events=lambda _run: (
        SimpleNamespace(envelope=SimpleNamespace(
            event_id="event", payload={"reason_code": reason},
        )),
    )))
    meter = CallMeter(state, "run")
    result = GatewayFailure(GatewayError("CAPABILITY_DENIED", "denied", "event"))
    assert meter.charged(result) is charged
    assert meter.charged(GatewayFailure(GatewayError("GATEWAY_UNAVAILABLE", "lost", None))) is None


async def test_failed_lean_startup_stores_metrics_without_model_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_host(*_args, **_kwargs):
        raise RuntimeError("private startup detail")

    async def forbidden_model_preflight(*_args, **_kwargs):
        raise AssertionError("scripted nightly must not need omp or magpie")

    monkeypatch.setattr(benchmark.WorkerExecutionHost, "open", fail_host)
    monkeypatch.setattr(benchmark, "preflight_omp_worker", forbidden_model_preflight)
    monkeypatch.setattr(benchmark, "run_security_gate", forbidden_model_preflight)
    destination, metrics = await run_benchmark(tmp_path, BenchmarkOptions())
    assert destination.is_file()
    assert (destination.parent / "lean-project/.aizim").is_dir()
    assert metrics["reason"] == "infrastructure_unavailable"
    assert metrics["error_code"] == "RuntimeError"
    assert metrics["verified_targets"] == 0
    assert metrics["attempts"] == 0
    assert "private startup detail" not in destination.read_text()
    assert object_value(metrics["configuration"])["model"] == "deterministic-tool-actions"


@pytest.mark.parametrize("options", [
    {"workers": 0}, {"call_budget": 10001}, {"timeout_seconds": 14401}, {"max_rounds": 0},
])
def test_invalid_budgets_are_rejected_before_project_creation(options) -> None:
    with pytest.raises(ResearchError, match="INVALID_BENCHMARK_CONFIGURATION"):
        BenchmarkOptions(**options)


async def test_real_backend_security_failure_prevents_any_model_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def rejected_security(_project):
        return SimpleNamespace(passed=False)

    def forbidden_backend(*_args, **_kwargs):
        raise AssertionError("backend must not start before a passing security gate")

    monkeypatch.setattr(benchmark, "run_security_gate", rejected_security)
    monkeypatch.setattr(benchmark, "resolve_omp", forbidden_backend)
    destination, metrics = await run_benchmark(tmp_path, BenchmarkOptions(backend="omp"))
    assert destination.is_file()
    assert metrics["error_code"] == "SECURITY_GATE_FAILED"
    assert metrics["reason"] == "infrastructure_unavailable"
    assert metrics["attempts"] == 0
    assert metrics["verified_targets"] == 0


def test_first_publication_preserves_the_predicted_project_epoch(tmp_path: Path) -> None:
    from pathlib import PurePosixPath

    from aizim.knowledge.lean_verifier import _with_import
    from aizim.lean.project import project_base_epoch

    _, project = create_project(tmp_path, load_ladder())
    module = "AizimResearch.FirstPublication"
    source = b"import Std\n\ntheorem published_identity (n : Nat) : n + 0 = n := rfl\n"
    predicted = project_base_epoch(project, {module: source})
    (project / "AizimResearch").mkdir()
    (project / "AizimResearch/FirstPublication.lean").write_bytes(source)
    manifest = _with_import(b"", PurePosixPath("AizimResearch/FirstPublication.lean"))
    (project / "AizimResearch.lean").write_bytes(manifest)
    assert project_base_epoch(project) == predicted


@pytest.fixture
def runtime_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "aizim"
    for relative_path in (
        "research/benchmark.py", "orchestration/worker.py", "knowledge/lean_verifier.py",
        "state/sql/001_foundation.sql", "agents/prompts/proof_worker.md",
    ):
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original\n")
    monkeypatch.setattr(benchmark, "__file__", str(root / "research/benchmark.py"))
    return root


@pytest.mark.parametrize("relative_path", [
    "orchestration/worker.py", "knowledge/lean_verifier.py",
    "state/sql/001_foundation.sql", "agents/prompts/proof_worker.md",
])
def test_runtime_edits_change_benchmark_comparison_hash(
    runtime_package: Path, relative_path: str,
) -> None:
    ladder, options = load_ladder(), BenchmarkOptions()
    before = configuration(ladder, options)
    (runtime_package / relative_path).write_text("changed\n")
    after = configuration(ladder, options)
    assert before["implementation_hash"] != after["implementation_hash"]
    assert sha256_json(before) != sha256_json(after)


def test_runtime_file_additions_removals_and_renames_change_fingerprint(
    runtime_package: Path,
) -> None:
    ladder, options = load_ladder(), BenchmarkOptions()
    original = configuration(ladder, options)
    added = runtime_package / "orchestration/new_worker.py"
    added.write_text("new worker\n")
    assert configuration(ladder, options) != original
    added.unlink()
    assert configuration(ladder, options) == original
    source = runtime_package / "orchestration/worker.py"
    renamed = source.with_name("renamed_worker.py")
    source.rename(renamed)
    assert configuration(ladder, options) != original
    renamed.unlink()
    assert configuration(ladder, options) != original


def test_runtime_fingerprint_ignores_install_location_and_generated_files(
    runtime_package: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ladder, options = load_ladder(), BenchmarkOptions()
    original = configuration(ladder, options)
    for relative_path in (
        "__pycache__/benchmark.cpython-315.pyc", "orchestration/worker.pyc",
        "orchestration/worker.pyo", ".cache/runtime.json", "research/.editor-state",
    ):
        path = runtime_package / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"generated")
    assert configuration(ladder, options) == original
    relocated = runtime_package.rename(tmp_path / "relocated-aizim")
    monkeypatch.setattr(benchmark, "__file__", str(relocated / "research/benchmark.py"))
    (relocated / "orchestration/worker.py").touch()
    assert configuration(ladder, options) == original
