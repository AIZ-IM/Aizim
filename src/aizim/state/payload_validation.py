from __future__ import annotations

import re

from aizim.domain.serialization import JsonValue


def patch_edits_payload(value: JsonValue) -> bool:
    return type(value) is list and all(
        type(item) is dict
        and set(item) == {"start_byte", "end_byte", "replacement_hex"}
        and type(item["start_byte"]) is int
        and item["start_byte"] >= 0
        and type(item["end_byte"]) is int
        and item["end_byte"] >= item["start_byte"]
        and type(item["replacement_hex"]) is str
        and len(item["replacement_hex"]) % 2 == 0
        and re.fullmatch(r"[0-9a-f]*", item["replacement_hex"]) is not None
        for item in value
    )
