"""Parse a single Claude Code transcript line into a typed record.

This turns one JSONL line into a frozen record that *holds* the relevant
fields. No boundary detection, no accumulation, no token accounting happen
here; those belong to other modules. The record carries its fields; it does
not interpret them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

__all__ = ["TranscriptRecord", "Usage", "parse_line"]


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
    # Stored raw -- no whitespace collapse or truncation here; that is the
    # renderer's job. Carried so a turn's opening record holds the text
    # downstream needs.
    text: str | None = None
    # Verbatim ``message.model`` (the model string as the transcript JSONL
    # carries it); ``None`` when absent. Held, not interpreted -- pricing
    # happens elsewhere.
    model: str | None = None
    # Verbatim top-level ``cwd`` (the session's real working directory); ``None``
    # when absent. The roster decodes this into a readable ``~``-relative title,
    # since the project-dir name on disk is a lossy dash-encoding of this path.
    cwd: str | None = None


def _str_or_none(value: object) -> str | None:
    """Keep a field only when it is a real string, else ``None``.

    Guards the ``str | None`` annotations against a malformed transcript. This
    is not cosmetic: ``message_id`` becomes a dict key in accounting, so an
    unhashable value (a list or object where an id belongs) would raise
    ``TypeError`` deep in the pipeline, and ``model`` reaches a regex in
    pricing, which rejects a non-string the same way. Dropping the field
    instead keeps every downstream layer on its documented never-raises path.
    """
    return value if isinstance(value, str) else None


def _int_or_none(value: object) -> int | None:
    """Keep a token count only when it is a real integer, else ``None``.

    Guards the ``int | None`` annotation against a malformed transcript: a
    string/float/null where a count belongs becomes ``None`` (a dropped field)
    rather than poisoning downstream arithmetic with a ``TypeError``. ``bool`` is
    an ``int`` subclass but never a valid count, so it is rejected too."""
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _parse_usage(raw: object) -> Usage | None:
    """Build a ``Usage`` from ``message.usage``, or ``None`` when it is absent,
    not an object, or carries no tokens at all. Counts are coerced to
    ``int``-or-``None`` so a malformed value never reaches accounting.

    An all-zero block is treated as absent because Claude Code writes one on
    ``<synthetic>`` notices ("You've hit your session limit", API errors). Left
    in, such a record would count as the turn's last usage-bearing record: its
    unpriceable model would drop the turn's real tokens from the dollar sum,
    and the context estimate would read 0 against no known window.
    """
    if not isinstance(raw, dict):
        return None
    usage = Usage(
        input_tokens=_int_or_none(raw.get("input_tokens")),
        output_tokens=_int_or_none(raw.get("output_tokens")),
        cache_creation_input_tokens=_int_or_none(
            raw.get("cache_creation_input_tokens")
        ),
        cache_read_input_tokens=_int_or_none(raw.get("cache_read_input_tokens")),
    )
    counts = (
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_creation_input_tokens,
        usage.cache_read_input_tokens,
    )
    return usage if any(counts) else None


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

    return TranscriptRecord(
        type=type_val,
        message_id=_str_or_none(message.get("id")),
        role=_str_or_none(message.get("role")),
        usage=_parse_usage(message.get("usage")),
        stop_reason=_str_or_none(message.get("stop_reason")),
        is_meta=bool(obj.get("isMeta", False)),
        is_sidechain=bool(obj.get("isSidechain", False)),
        is_tool_result=_is_tool_result(type_val, message),
        text=_str_or_none(message.get("content")),
        model=_str_or_none(message.get("model")),
        cwd=_str_or_none(obj.get("cwd")),
    )
