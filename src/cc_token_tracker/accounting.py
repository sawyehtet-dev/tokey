"""Usage accounting over parsed transcript records (Ticket 2).

Pure logic: no file reading, no tailing, no printing, no display. This operates
on records already produced by :func:`cc_token_tracker.parser.parse_line`, in
transcript order.

A single assistant turn is written as several transcript lines -- one per
content block (thinking, text, tool_use) -- all sharing the same
``message_id`` and carrying IDENTICAL usage numbers. We therefore dedupe by
``message_id``, counting each message exactly once and taking its usage from the
first line seen; we never sum across the lines of one message. (``uuid`` is
per-line and would over-count, so it is deliberately not used.)
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from cc_token_tracker.parser import TranscriptRecord

__all__ = ["MessageCost", "SessionAccounting", "account_usage"]


@dataclass(frozen=True)
class MessageCost:
    """Deduped cost for one assistant message.

    The four components are plain ints with absent counts coalesced to 0 (the
    parser stores absent as ``None``; the zeroing happens here, in accounting).
    ``message_total`` is their sum; ``session_total`` is the cumulative total
    through and including this message.
    """

    message_id: str | None
    input_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    output_tokens: int
    message_total: int
    session_total: int


@dataclass(frozen=True)
class SessionAccounting:
    """Per-message breakdown plus the cumulative session total.

    ``session_total`` equals ``messages[-1].session_total`` when there is at
    least one message, and 0 for empty input.
    """

    messages: list[MessageCost] = field(default_factory=list)
    session_total: int = 0


def _as_int(value: int | None) -> int:
    """Coalesce an absent (None) count to 0 for summing. Sum-only zeroing."""
    return value if value is not None else 0


def account_usage(
    records: Iterable[TranscriptRecord | None],
) -> SessionAccounting:
    """Compute deduped per-message costs and a running session total.

    Includes only records that carry a usage block (assistant messages); skips
    ``None`` results from the parser and records with no usage (user lines,
    tool_result lines, etc.). Records are deduped by ``message_id`` in
    first-seen order, each message counted once.
    """
    # message_id -> (message_id, Usage) for the first line seen of that message.
    first_seen: dict[object, tuple[str | None, object]] = {}
    order: list[object] = []

    for record in records:
        if record is None:
            continue
        usage = record.usage
        if usage is None:
            continue

        # Group by message_id. A usage-bearing record with no id (not expected
        # in real transcripts) gets a unique key so distinct messages are never
        # merged into one another.
        key: object = record.message_id if record.message_id is not None else object()
        if key in first_seen:
            # Same message, another content-block line: identical usage already
            # counted -- do not add it again.
            continue
        first_seen[key] = (record.message_id, usage)
        order.append(key)

    messages: list[MessageCost] = []
    running_total = 0
    for key in order:
        message_id, usage = first_seen[key]
        input_tokens = _as_int(usage.input_tokens)
        cache_creation = _as_int(usage.cache_creation_input_tokens)
        cache_read = _as_int(usage.cache_read_input_tokens)
        output_tokens = _as_int(usage.output_tokens)
        message_total = input_tokens + cache_creation + cache_read + output_tokens
        running_total += message_total
        messages.append(
            MessageCost(
                message_id=message_id,
                input_tokens=input_tokens,
                cache_creation_input_tokens=cache_creation,
                cache_read_input_tokens=cache_read,
                output_tokens=output_tokens,
                message_total=message_total,
                session_total=running_total,
            )
        )

    return SessionAccounting(messages=messages, session_total=running_total)
