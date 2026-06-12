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
    # Model string of the turn's LAST usage-bearing record, verbatim from the
    # transcript; None when the turn has no usage-bearing record. Additive
    # surface for pricing -- no token value depends on it.
    model: str | None = None


def _turn_model(turn: Turn) -> str | None:
    """Model of the turn's LAST usage-bearing record, or None without one.

    When a turn carries records from more than one model, the last
    usage-bearing record wins. The model is read verbatim off the record; it
    may itself be None when the transcript line omitted it.
    """
    model: str | None = None
    for record in turn.records:
        if record.usage is not None:
            model = record.model
    return model


def turn_costs(turns: Iterable[Turn]) -> list[TurnCost]:
    """Cost each turn independently, preserving turn order.

    For every turn, runs :func:`account_usage` over its records (which dedupes
    by message id and skips records with no usage) and attaches the result.
    """
    results: list[TurnCost] = []
    for turn in turns:
        accounting = account_usage(turn.records)
        results.append(
            TurnCost(
                complete=turn.complete,
                input_tokens=accounting.total_input_tokens,
                cache_creation_input_tokens=accounting.total_cache_creation_input_tokens,
                cache_read_input_tokens=accounting.total_cache_read_input_tokens,
                output_tokens=accounting.total_output_tokens,
                turn_total=accounting.session_total,
                accounting=accounting,
                model=_turn_model(turn),
            )
        )
    return results
