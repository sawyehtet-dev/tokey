"""Per-turn dollar pricing keyed on transcript model strings.

Pure logic: no IO, no clock, no global state. The rate table is keyed on the
model string exactly as it appears in the transcript JSONL (``message.model``);
a trailing ``-YYYYMMDD`` date suffix is stripped before a second lookup so a
dated id like ``claude-haiku-4-5-20251001`` still prices. A model the table
does not know yields ``None``: the caller decides how to render the unknown.
"""

from __future__ import annotations

import re

__all__ = ["normalize_model", "turn_cost_usd"]

# prices as of 2026-06-12, source: platform.claude.com/docs/en/about-claude/pricing
# cache_write uses the 5-minute TTL multiplier (1.25x input); 1-hour cache
# writes are billed higher, so turns carrying 1h-TTL writes would undercount.
# Rates are dollars per million tokens.
_RATES_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-fable-5": {
        "input": 10.00, "output": 50.00, "cache_write": 12.50, "cache_read": 1.00,
    },
    "claude-opus-4-8": {
        "input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50,
    },
    "claude-opus-4-7": {
        "input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50,
    },
    "claude-opus-4-6": {
        "input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50,
    },
    "claude-opus-4-5": {
        "input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50,
    },
    "claude-sonnet-4-6": {
        "input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30,
    },
    "claude-haiku-4-5": {
        "input": 1.00, "output": 5.00, "cache_write": 1.25, "cache_read": 0.10,
    },
}

_MTOK = 1_000_000

# A dated model id ends in -YYYYMMDD (e.g. claude-haiku-4-5-20251001).
_DATE_SUFFIX = re.compile(r"-\d{8}$")


def normalize_model(model: str) -> str:
    """Strip a trailing ``-YYYYMMDD`` date suffix, if present."""
    return _DATE_SUFFIX.sub("", model)


def turn_cost_usd(
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int,
    cache_read_tokens: int,
    cost_usd: float | None = None,
) -> float | None:
    """Dollar cost of one turn, or None when the model is unknown.

    ``cost_usd`` is an authoritative pre-computed cost (a transcript record's
    ``costUSD`` field) when the caller has one: it is returned as-is and the
    table compute is skipped. It is never assumed to exist; absent (None), the
    cost is computed from the rate table. The model is looked up verbatim
    first, then with the date suffix normalized away; only after both miss --
    or when ``model`` itself is None -- does this return None.
    """
    if cost_usd is not None:
        return float(cost_usd)
    if model is None:
        return None
    rates = _RATES_PER_MTOK.get(model)
    if rates is None:
        rates = _RATES_PER_MTOK.get(normalize_model(model))
    if rates is None:
        return None
    return (
        input_tokens * rates["input"]
        + output_tokens * rates["output"]
        + cache_write_tokens * rates["cache_write"]
        + cache_read_tokens * rates["cache_read"]
    ) / _MTOK
