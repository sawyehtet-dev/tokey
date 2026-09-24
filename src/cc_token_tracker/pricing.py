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

# prices as of 2026-09-24, source: platform.claude.com/docs/en/about-claude/pricing
# cache_write is the 5-minute TTL rate (1.25x input). 1-hour writes bill at
# 2x input on every model, so they are priced off ``input`` via
# _ONE_HOUR_WRITE_MULTIPLIER rather than stored as another column.
# Rates are dollars per million tokens.
_RATES_PER_MTOK: dict[str, dict[str, float]] = {
    # cache reads on Fable/Mythos 5.1 are 0.025x input ($0.25), not the usual
    # 0.1x. Mythos is the Project Glasswing twin of Fable at identical rates.
    "claude-fable-5-1": {
        "input": 10.00, "output": 50.00, "cache_write": 12.50, "cache_read": 0.25,
    },
    "claude-mythos-5-1": {
        "input": 10.00, "output": 50.00, "cache_write": 12.50, "cache_read": 0.25,
    },
    "claude-fable-5": {
        "input": 10.00, "output": 50.00, "cache_write": 12.50, "cache_read": 1.00,
    },
    "claude-mythos-5": {
        "input": 10.00, "output": 50.00, "cache_write": 12.50, "cache_read": 1.00,
    },
    # cache reads on Opus 5.5 are 0.05x input ($0.20), not the usual 0.1x.
    "claude-opus-5-5": {
        "input": 4.00, "output": 20.00, "cache_write": 5.00, "cache_read": 0.20,
    },
    "claude-opus-5": {
        "input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50,
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
    # the launch "intro" $2/$10 is now the standard price: the 2026-09-01
    # increase to $3/$15 was cancelled. Nothing pending on this row.
    "claude-sonnet-5": {
        "input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.20,
    },
    "claude-sonnet-4-6": {
        "input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30,
    },
    "claude-haiku-4-5": {
        "input": 1.00, "output": 5.00, "cache_write": 1.25, "cache_read": 0.10,
    },
}

_MTOK = 1_000_000

# 1-hour cache writes cost 2x base input on every model (pricing page,
# "Prompt caching" multipliers). Claude Code writes almost all of its cache
# with the 1-hour TTL, so this is most of the cache-write bill in practice.
_ONE_HOUR_WRITE_MULTIPLIER = 2.0

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
    *,
    cache_write_1h_tokens: int = 0,
) -> float | None:
    """Dollar cost of one turn, or None when the model is unknown.

    ``cache_write_tokens`` is the turn's TOTAL cache write; ``cache_write_1h_tokens``
    is the part of it written with the 1-hour TTL, billed at 2x input instead of
    the 5-minute rate. It is clamped to the total, so a malformed split can
    never price more write tokens than the turn carried.

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
    one_hour = min(max(cache_write_1h_tokens, 0), cache_write_tokens)
    return (
        input_tokens * rates["input"]
        + output_tokens * rates["output"]
        + (cache_write_tokens - one_hour) * rates["cache_write"]
        + one_hour * rates["input"] * _ONE_HOUR_WRITE_MULTIPLIER
        + cache_read_tokens * rates["cache_read"]
    ) / _MTOK
