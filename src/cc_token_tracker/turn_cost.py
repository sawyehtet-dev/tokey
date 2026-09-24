"""Per-turn cost: compose accounting over each turn, then price it.

Pure logic, no IO. ``segment_turns`` groups records into turns; ``account_usage``
dedupes cost over records. This module runs ``account_usage`` over each turn's
records to produce the headline per-command number, one ``TurnCost`` per turn,
and owns the two dollar helpers every render surface shares: :func:`turn_usd`
for one turn and :func:`session_cost` for a whole session.

Accounting is reused as-is -- this module does not reimplement dedup or summing,
and pricing is the frozen :func:`cc_token_tracker.pricing.turn_cost_usd` table.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from cc_token_tracker.accounting import SessionAccounting, account_usage
from cc_token_tracker.pricing import turn_cost_usd
from cc_token_tracker.segmentation import Turn

__all__ = ["TurnCost", "session_cost", "turn_costs", "turn_usd"]


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
    # 1-hour-TTL share of cache_creation_input_tokens, for pricing only; it is
    # already inside that count and inside turn_total.
    cache_creation_1h_input_tokens: int = 0


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
                cache_creation_input_tokens=(
                    accounting.total_cache_creation_input_tokens
                ),
                cache_read_input_tokens=accounting.total_cache_read_input_tokens,
                output_tokens=accounting.total_output_tokens,
                turn_total=accounting.session_total,
                accounting=accounting,
                model=_turn_model(turn),
                cache_creation_1h_input_tokens=(
                    accounting.total_cache_creation_1h_input_tokens
                ),
            )
        )
    return results


def turn_usd(cost: TurnCost) -> float | None:
    """One turn's dollar cost via the frozen pricing table, or None.

    Pricing is :func:`cc_token_tracker.pricing.turn_cost_usd` over the SAME four
    component counts the turn already carries, plus the 1-hour share of its cache
    write so that share bills at the 1-hour rate -- nothing is recomputed here. No
    ``costUSD`` is passed: parsed records do not carry one today, so the table
    compute applies (``turn_cost_usd`` accepts one for when a caller has it).
    None means the turn's model is unknown or absent; the caller renders that
    honestly (``$?``), never as $0.00.
    """
    return turn_cost_usd(
        cost.model,
        cost.input_tokens,
        cost.output_tokens,
        cost.cache_creation_input_tokens,
        cost.cache_read_input_tokens,
        cache_write_1h_tokens=cost.cache_creation_1h_input_tokens,
    )


def session_cost(costs: Iterable[TurnCost]) -> tuple[float, bool]:
    """(sum of per-turn dollar costs, whether any turn went unpriced).

    The single source of truth for a session's dollar total. Each turn is priced
    individually with its OWN model, then the dollars are summed -- never
    aggregate tokens times a single rate, since a session can mix models. A turn
    that cannot be priced is left out of the sum and flips the unpriced flag so
    the renderer can mark the total as partial. EXCEPTION: a zero-token
    unpriceable turn (the just-opened in-flight prompt before any usage lands)
    contributes $0 exactly on any model, so it neither shifts the sum nor raises
    the flag -- otherwise the marker would flash on every new prompt.
    """
    total = 0.0
    unpriced = False
    for cost in costs:
        usd = turn_usd(cost)
        if usd is not None:
            total += usd
        elif cost.turn_total:
            unpriced = True
    return total, unpriced
