from __future__ import annotations


def checked_usage(value: object) -> dict[str, int] | None:
    """Validate provider-neutral token counts observed for one attempt."""
    if type(value) is not dict:
        return None
    result: dict[str, int] = {}
    for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
        amount = value.get(key)
        if type(amount) is not int or amount < 0:
            return None
        result[key] = amount
    return result if result["cached_input_tokens"] <= result["input_tokens"] else None
