from __future__ import annotations

from pathlib import Path

import pytest

from aizim.agents.codex_events import CodexEventError, TransportEventHasher, parse_final_message


def _malformed_document(attack: str) -> bytes:
    if attack == "nonfinite":
        return b'{"value":NaN}'
    if attack == "deep":
        return b'{"value":' + b"[" * 1200 + b"0" + b"]" * 1200 + b"}"
    return b'{"value":' + b"9" * 5000 + b"}"


@pytest.mark.parametrize("attack", ("nonfinite", "deep", "large_integer"))
def test_transport_event_parser_maps_hostile_json_to_fixed_error(attack: str) -> None:
    with pytest.raises(CodexEventError, match="CODEX_JSONL_INVALID"):
        TransportEventHasher().add(_malformed_document(attack))


@pytest.mark.parametrize("attack", ("nonfinite", "deep", "large_integer"))
def test_final_message_parser_maps_hostile_json_to_fixed_error(tmp_path: Path, attack: str) -> None:
    result = tmp_path / "result.json"
    result.write_bytes(_malformed_document(attack))

    with pytest.raises(CodexEventError, match="CODEX_RESULT_INVALID"):
        parse_final_message(result)
