"""Shared record factories for the test suite.

These build :class:`cc_token_tracker.parser.TranscriptRecord` values directly,
exactly as the parser would yield them, so tests across modules construct
identical fixtures instead of each reinventing them. Factories whose shape is
specific to one module's tests (segmentation's stop-reason-only ``assistant``,
the usage/sessions line builders) deliberately stay local to those files.
"""

from __future__ import annotations

from cc_token_tracker.parser import TranscriptRecord, Usage
from cc_token_tracker.reader import ReadResult


def prompt(mid):
    """A genuine typed user prompt (opens a turn)."""
    return TranscriptRecord(type="user", message_id=mid, role="user")


def typed(mid, text):
    """A typed user prompt that opens a turn AND carries its raw text, exactly as
    parse_line retains string message.content."""
    return TranscriptRecord(type="user", message_id=mid, role="user", text=text)


def tool_result(mid):
    """A user line carrying a tool_result (does NOT open a turn)."""
    return TranscriptRecord(type="user", message_id=mid, role="user",
                            is_tool_result=True)


def assistant(mid, input_tokens, output_tokens, cache_creation, cache_read,
              stop_reason="end_turn", model=None):
    return TranscriptRecord(
        type="assistant", message_id=mid, role="assistant", stop_reason=stop_reason,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens,
                    cache_creation_input_tokens=cache_creation,
                    cache_read_input_tokens=cache_read),
        model=model,
    )


def read_result(records, path):
    return ReadResult(records=records, transcript_path=path)
