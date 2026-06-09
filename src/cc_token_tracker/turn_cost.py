"""Per-turn cost: compose accounting over each turn.

Pure logic, no IO. ``segment_turns`` groups records into turns; ``account_usage``
dedupes cost over records. This module runs ``account_usage`` over each turn's
records to produce the headline per-command number, one ``TurnCost`` per turn.

Accounting is reused as-is -- this module does not reimplement dedup or summing.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from cc_token_tracker.accounting import SessionAccounting, account_usage
from cc_token_tracker.segmentation import Turn

__all__ = ["TurnCost", "turn_costs"]


@dataclass(frozen=True)
class TurnCost:
    """Deduped cost of one turn.

    The four components are turn-level totals (summed across the turn's deduped
    messages); ``turn_total`` is their sum and equals ``accounting.session_total``.
    ``complete`` is carried from the source :class:`Turn` so callers can tell a
    finished turn from the in-flight one. ``accounting`` is the attached
    :func:`account_usage` result, kept for callers that want the per-message
    breakdown.
    """

    complete: bool
    input_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    output_tokens: int
    turn_total: int
    accounting: SessionAccounting


def turn_costs(turns: Iterable[Turn]) -> list[TurnCost]:
    """Cost each turn independently, preserving turn order.

    For every turn, runs :func:`account_usage` over its records (which dedupes
    by message id and skips records with no usage) and attaches the result.
    """
    results: list[TurnCost] = []
    for turn in turns:
        accounting = account_usage(turn.records)
        messages = accounting.messages
        results.append(
            TurnCost(
                complete=turn.complete,
                input_tokens=sum(m.input_tokens for m in messages),
                cache_creation_input_tokens=sum(m.cache_creation_input_tokens for m in messages),
                cache_read_input_tokens=sum(m.cache_read_input_tokens for m in messages),
                output_tokens=sum(m.output_tokens for m in messages),
                turn_total=accounting.session_total,
                accounting=accounting,
            )
        )
    return results
