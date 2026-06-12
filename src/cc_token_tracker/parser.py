"""Parse a single Claude Code transcript line into a typed record.

This turns one JSONL line into a frozen record that *holds* the relevant
fields. No boundary detection, no accumulation, no token accounting happen
here; those belong to other modules. The record carries its fields; it does
not interpret them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

__all__ = ["Usage", "TranscriptRecord", "parse_line"]


@dataclass(frozen=True)
class Usage:
    """Raw token-usage block from ``message.usage``.

    Holds the four counts as-is. Absent counts stay ``None`` -- we do not
    invent a zero. No arithmetic happens here; accounting happens elsewhere.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None


@dataclass(frozen=True)
class TranscriptRecord:
    """One parsed transcript line.

    Optional fields default to ``None`` / ``False`` when the source line omits
    them, rather than to invented values.
    """

    type: str
    message_id: str | None = None
    role: str | None = None
    usage: Usage | None = None
    stop_reason: str | None = None
    is_meta: bool = False
    is_sidechain: bool = False
    is_tool_result: bool = False
    # Verbatim ``message.content`` when it is a plain string (a typed user
    # prompt); ``None`` when content is a block list (tool_use/tool_result/etc).
    # Stored raw -- no whitespace collapse or truncation here; that is display's
    # job. Carried so a turn's opening record holds the text downstream needs.
    text: str | None = None
    # Verbatim ``message.model`` (the model string as the transcript JSONL
    # carries it); ``None`` when absent. Held, not interpreted -- pricing
    # happens elsewhere.
    model: str | None = None


def _parse_usage(raw: object) -> Usage | None:
    """Build a ``Usage`` from ``message.usage``, or ``None`` when it is absent
    or not an object."""
    if not isinstance(raw, dict):
        return None
    return Usage(
        input_tokens=raw.get("input_tokens"),
        output_tokens=raw.get("output_tokens"),
        cache_creation_input_tokens=raw.get("cache_creation_input_tokens"),
        cache_read_input_tokens=raw.get("cache_read_input_tokens"),
    )


def _is_tool_result(type_val: str, message: dict) -> bool:
    """True when a user line's content is/contains a ``tool_result`` block --
    i.e. the line is NOT a typed prompt. Any non-user line, or a user line whose
    content is a plain string/other block, is False. Defensive: never raises."""
    if type_val != "user":
        return False
    content = message.get("content")
    blocks = content if isinstance(content, list) else [content]
    return any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in blocks
    )


def parse_line(line: str) -> TranscriptRecord | None:
    """Parse one JSONL transcript line into a :class:`TranscriptRecord`.

    Returns ``None`` for anything malformed or partial -- invalid/incomplete
    JSON, a JSON value that is not an object, or an object without a usable
    string ``type``. Never raises.
    """
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        # ValueError covers json.JSONDecodeError (bad/partial/empty JSON);
        # TypeError covers a non-string argument.
        return None

    if not isinstance(obj, dict):
        return None

    type_val = obj.get("type")
    if not isinstance(type_val, str):
        return None

    # "message" may be absent or non-object; treat either as empty so the
    # nested reads below stay defensive.
    message = obj.get("message")
    if not isinstance(message, dict):
        message = {}

    content = message.get("content")
    return TranscriptRecord(
        type=type_val,
        message_id=message.get("id"),
        role=message.get("role"),
        usage=_parse_usage(message.get("usage")),
        stop_reason=message.get("stop_reason"),
        is_meta=bool(obj.get("isMeta", False)),
        is_sidechain=bool(obj.get("isSidechain", False)),
        is_tool_result=_is_tool_result(type_val, message),
        text=content if isinstance(content, str) else None,
        model=message.get("model"),
    )
