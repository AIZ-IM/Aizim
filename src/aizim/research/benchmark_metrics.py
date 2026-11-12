from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from aizim.domain import sha256_json
from aizim.domain.serialization import JsonValue

from .benchmark_backend import CallCounts
from .benchmark_ladder import BenchmarkLadder
from .records import integer, object_value, text
from .report import estimate

_TOKEN_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens")


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _usage(
    attempts: list[dict[str, JsonValue]], prices: dict[str, JsonValue] | None,
) -> dict[str, JsonValue]:
    models: dict[str, dict[str, JsonValue]] = {}
    for attempt in attempts:
        stages = [(text(attempt["model"]), attempt.get("usage"))]
        if attempt.get("controller_model"):
            stages.append((text(attempt["controller_model"]), attempt.get("controller_usage")))
        for model, usage in stages:
            group = models.setdefault(model, {
                "model": model, "measured_stages": 0, "unknown_stages": 0,
                **{key: 0 for key in _TOKEN_KEYS},
                "known_usage_usd": None, "estimated_usd": None,
            })
            if usage is None:
                group["unknown_stages"] = integer(group["unknown_stages"]) + 1
                continue
            group["measured_stages"] = integer(group["measured_stages"]) + 1
            values = object_value(usage)
            for key in _TOKEN_KEYS:
                group[key] = integer(group[key]) + integer(values[key])
    known_cost = Decimal(0)
    priced_stages = 0
    unpriced_stages = 0
    unknown_stages = 0
    measured_stages = 0
    for model, group in models.items():
        measured = integer(group["measured_stages"])
        unknown = integer(group["unknown_stages"])
        measured_stages += measured
        unknown_stages += unknown
        rate = None if prices is None else prices.get(model)
        if rate is not None and measured:
            subtotal = estimate(group, object_value(rate))
            group["known_usage_usd"] = subtotal
            known_cost += Decimal(subtotal)
            priced_stages += measured
            if not unknown:
                group["estimated_usd"] = subtotal
        else:
            unpriced_stages += measured
    complete = bool(models) and not unknown_stages
    total_cost = format(known_cost, ".6f") if priced_stages else None
    return {
        "by_model": list(models.values()),
        "measured_stages": measured_stages,
        "unknown_stages": unknown_stages,
        "unpriced_measured_stages": unpriced_stages,
        "known_tokens": {
            key: sum(integer(group[key]) for group in models.values()) for key in _TOKEN_KEYS
        },
        "total_tokens": {
            key: sum(integer(group[key]) for group in models.values()) if complete else None
            for key in _TOKEN_KEYS
        },
        "known_cost_usd": total_cost,
        "estimated_cost_usd": total_cost if complete and not unpriced_stages else None,
    }


def summarize_metrics(
    *,
    ladder: BenchmarkLadder,
    configuration: dict[str, JsonValue],
    run_id: str,
    tasks: list[dict[str, JsonValue]],
    attempts: list[dict[str, JsonValue]],
    calls: dict[str, CallCounts],
    verified: dict[str, list[str]],
    wall_seconds: float,
    reason: str,
    error_code: str = "",
    prices: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    """Aggregate private measurements; `verified` comes from trusted target binding."""
    by_id = {text(task["task_id"]): task for task in tasks}
    target_rows: list[JsonValue] = []
    for target in ladder.targets:
        task_attempts = [item for item in attempts if item["task_id"] == target.task_id]
        charged = sum(item.get("charged") is True for item in task_attempts)
        measurements = [calls.get(text(item["worker_id"]), CallCounts()) for item in task_attempts]
        known_calls = sum(item.charged for item in measurements)
        unknown_calls = sum(item.unknown for item in measurements)
        task = by_id.get(target.task_id)
        declarations: list[JsonValue] = list(verified.get(target.task_id, ()))
        target_rows.append({
            "task_id": target.task_id,
            "status": "unstarted" if task is None else task["status"],
            "verified": bool(declarations),
            "verified_declarations": declarations,
            "depends_on": list(target.depends_on),
            "attempts": len(task_attempts),
            "charged_rounds": charged,
            "charged_calls": None if unknown_calls else known_calls,
            "known_charged_calls": known_calls,
            "unknown_call_outcomes": unknown_calls,
            "gateway_requests": sum(item.requests for item in measurements),
            "infrastructure_failures": sum(
                item.get("infrastructure_failure") is True for item in task_attempts
            ),
        })
    rows = [object_value(row) for row in target_rows]
    verified_count = sum(row["verified"] is True for row in rows)
    attempts_finished = sum(item["status"] != "running" for item in attempts)
    infrastructure_failures = sum(item.get("infrastructure_failure") is True for item in attempts)
    rounds = sum(integer(row["charged_rounds"]) for row in rows)
    known_calls = sum(integer(row["known_charged_calls"]) for row in rows)
    unknown_calls = sum(integer(row["unknown_call_outcomes"]) for row in rows)
    return {
        "schema_version": "research-benchmark-metrics-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "ladder_version": ladder.version,
        "ladder_hash": ladder.content_hash,
        "configuration": configuration,
        "configuration_hash": sha256_json(configuration),
        "reason": reason,
        "error_code": error_code,
        "wall_seconds": wall_seconds,
        "targets": len(ladder.targets),
        "verified_targets": verified_count,
        "verify_rate": _ratio(verified_count, len(ladder.targets)),
        "attempts": len(attempts),
        "finished_attempts": attempts_finished,
        "charged_rounds": rounds,
        "charged_rounds_per_verified_target": _ratio(rounds, verified_count),
        "charged_calls": None if unknown_calls else known_calls,
        "known_charged_calls": known_calls,
        "unknown_call_outcomes": unknown_calls,
        "charged_calls_per_verified_target": (
            None if unknown_calls else _ratio(known_calls, verified_count)
        ),
        "gateway_requests": sum(integer(row["gateway_requests"]) for row in rows),
        "infrastructure_failures": infrastructure_failures,
        "infrastructure_failure_rate": _ratio(infrastructure_failures, attempts_finished),
        "run_infrastructure_failure": reason == "infrastructure_unavailable",
        "usage": _usage(attempts, prices),
        "prices_per_million": prices,
        "per_target": target_rows,
        "publication": "local_private_record",
    }
