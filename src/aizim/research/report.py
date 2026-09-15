from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from aizim.cli.state_client import load_projections
from aizim.domain import sha256_json
from aizim.domain.serialization import JsonValue
from aizim.runtime.layout import ProjectLayout

from .records import ResearchError, integer, object_value, text


def snapshot(project: Path, prices: dict[str, JsonValue] | None = None) -> dict[str, JsonValue]:
    layout = ProjectLayout.from_lean_project(project)
    layout.validate_runtime()
    grouped: dict[str, JsonValue] = {
        name: []
        for name in (
            "task",
            "attempt",
            "memory",
            "inbox",
            "contribution",
            "review",
            "run",
        )
    }
    formal_evidence: list[JsonValue] = []
    for record in load_projections(layout):
        if record.projection_name == "verified_declarations":
            formal_evidence.append(record.state)
        if record.projection_name != "research":
            continue
        payload = object_value(record.state.get("payload"))
        kind = text(payload["kind"])
        entries = grouped.get(kind)
        if type(entries) is list:
            entries.append(object_value(payload["data"]))
    attempts = grouped["attempt"]
    assert type(attempts) is list
    totals: dict[str, dict[str, JsonValue]] = {}
    for item in attempts:
        attempt = object_value(item)
        stages = [(text(attempt["model"]), attempt["usage"])]
        if attempt.get("controller_model"):
            stages.append((text(attempt["controller_model"]), attempt.get("controller_usage")))
        for model, usage in stages:
            total = totals.setdefault(
                model,
                {
                    "model": model,
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                    "unknown_stages": 0,
                    "known_usage_usd": None,
                    "estimated_usd": None,
                },
            )
            if usage is None:
                total["unknown_stages"] = integer(total["unknown_stages"]) + 1
                continue
            values = object_value(usage)
            for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
                total[key] = integer(total[key]) + integer(values[key])
    for model, total in totals.items():
        rate = None if prices is None else prices.get(model)
        if rate is not None:
            total["known_usage_usd"] = estimate(total, object_value(rate))
            if total["unknown_stages"] == 0:
                total["estimated_usd"] = total["known_usage_usd"]
    body: dict[str, JsonValue] = {
        "project": layout.root.name,
        "generated_at": datetime.now(UTC).isoformat(),
        "records": grouped,
        "formal_evidence": formal_evidence,
        "usage": list(totals.values()),
        "usage_scope": (
            "Measured worker and controller usage. Unknown stages are not zero-cost stages. "
            "Known usage cost is a subtotal; a total estimate requires complete usage."
        ),
        "identity_assurance": "operator_declared",
        "publication": "local_private_record",
        "prices_per_million": prices,
    }
    body["content_hash"] = sha256_json(
        {key: value for key, value in body.items() if key != "generated_at"}
    )
    return body


def estimate(usage: Mapping[str, JsonValue], rates: Mapping[str, JsonValue]) -> str:
    keys = {"input_per_million", "cached_input_per_million", "output_per_million"}
    if rates.keys() != keys:
        raise ResearchError("INVALID_PRICE_TABLE")
    try:
        prices = {key: Decimal(text(value, limit=64)) for key, value in rates.items()}
    except InvalidOperation:
        raise ResearchError("INVALID_PRICE_TABLE") from None
    if any(not value.is_finite() or value < 0 for value in prices.values()):
        raise ResearchError("INVALID_PRICE_TABLE")
    cached = integer(usage["cached_input_tokens"])
    inputs = integer(usage["input_tokens"])
    outputs = integer(usage["output_tokens"])
    if cached > inputs:
        raise ResearchError("INVALID_USAGE")
    cost = (
        (inputs - cached) * prices["input_per_million"]
        + cached * prices["cached_input_per_million"]
        + outputs * prices["output_per_million"]
    ) / Decimal(1_000_000)
    return format(cost.quantize(Decimal("0.000001")), "f")


def load_prices(path: Path | None) -> dict[str, JsonValue] | None:
    return None if path is None else object_value(json.loads(path.read_text()))
