from __future__ import annotations

import json
from pathlib import Path


def test_checked_in_codex_result_schema_is_exact_and_closed() -> None:
    schema_path = Path(__file__).parents[2] / "src/aizim/agents/codex_result.schema.json"

    schema = json.loads(schema_path.read_text())

    assert schema["type"] == "object"
    assert schema["required"] == ["status", "summary"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["status"]["enum"] == ["submitted", "abstained", "failed"]
    assert schema["properties"]["summary"] == {
        "type": "string",
        "minLength": 1,
        "maxLength": 2000,
    }
