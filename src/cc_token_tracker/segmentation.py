"""Segment an ordered record stream into turns.

Pure logic: no IO, no file reading, no printing. A "turn" is one typed user
prompt plus the whole fan-out of assistant/tool_result records it triggers,
up to and including the assistant message that ends the turn
(``stop_reason == "end_turn"``).

Per-turn cost later is just ``account_usage`` applied to one turn's records;
this module only groups, it does not account.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from cc_token_tracker.parser import TranscriptRecord

__all__ = ["Turn", "segment_turns"]


@dataclass(frozen=True)
class Turn:
    """One turn: its records in transcript order, plus whether it closed.

    ``complete`` is True when an assistant ``end_turn`` record closed the turn,
    and False for a still-in-flight trailing turn (the live view needs it).
    """

    records: list[TranscriptRecord] = field(default_factory=list)
    complete: bool = False


def _is_typed_prompt(record: TranscriptRecord) -> bool:
    """A genuine typed user prompt -- the thing that opens a turn."""
    return (
        record.type == "user"
        and not record.is_tool_result
        and not record.is_meta
        and not record.is_sidechain
    )


def segment_turns(records: Iterable[TranscriptRecord | None]) -> list[Turn]:
    """Group an ordered record stream into turns.

    - A new turn OPENS at a typed user prompt.
    - Following records join the current turn, up to and INCLUDING the assistant
      ``end_turn`` record, which CLOSES it (``complete=True``).
    - ``is_meta`` / ``is_sidechain`` records are dropped entirely.
    - ``is_tool_result`` user records do not open a turn; they stay inside it.
    - Records before the first typed prompt are ignored.
    - A trailing turn with no ``end_turn`` seen is still emitted,
      ``complete=False``.
    """
    turns: list[Turn] = []
    current: list[TranscriptRecord] | None = None

    for record in records:
        if record is None:
            continue
        # Dropped entirely: never added to a turn, never open one.
        if record.is_meta or record.is_sidechain:
            continue

        if _is_typed_prompt(record):
            # A new prompt supersedes any still-open turn, which had no
            # end_turn and is therefore incomplete.
            if current is not None:
                turns.append(Turn(records=current, complete=False))
            current = [record]
            continue

        if current is None:
            # Before the first typed prompt: ignore.
            continue

        current.append(record)
        if record.type == "assistant" and record.stop_reason == "end_turn":
            turns.append(Turn(records=current, complete=True))
            current = None

    if current is not None:
        # Trailing in-flight turn with no end_turn seen.
        turns.append(Turn(records=current, complete=False))

    return turns
