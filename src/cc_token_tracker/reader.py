"""Poll-based reader: turn the live transcript into records.

One tick per call. This module does not own a sleep or poll loop; the caller
drives cadence. Each tick reads a pointer file that names the current
transcript, does a full re-read of that transcript (no byte-offset tracking),
and parses every line with the existing parse_line. Records that parse to None
are dropped.

Full re-read is deliberate: account_usage dedupes by message_id in first-seen
order, so re-reading the whole file each tick is idempotent downstream.

The reader never raises. Any failure (pointer absent, pointer empty, transcript
missing or unreadable) resolves to a no-op tick: an empty ReadResult with
transcript_path None. A truncated final line that does not parse is dropped
while the prior records are kept.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cc_token_tracker.parser import TranscriptRecord, parse_line

__all__ = ["ReadResult", "read_tick"]


@dataclass(frozen=True)
class ReadResult:
    """Records parsed from the current transcript on one tick.

    records holds parsed TranscriptRecord values in transcript order (the same
    shape Turn uses for its records). transcript_path is the transcript named by
    the pointer on this tick, or None for a no-op tick.
    """

    records: list[TranscriptRecord] = field(default_factory=list)
    transcript_path: str | None = None


def _read_text(path: str) -> str | None:
    """Read a whole file as text, or None when it cannot be read.

    Decoding uses errors="replace" so a final line truncated mid multibyte
    character cannot raise: that line just fails to parse later and is dropped,
    leaving the prior lines intact.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _read_pointer(pointer_path: str) -> str | None:
    """Return the transcript path named by the pointer file, or None.

    None covers a pointer that is absent, unreadable, empty, or whitespace only.
    """
    content = _read_text(pointer_path)
    if content is None:
        return None
    transcript_path = content.strip()
    if not transcript_path:
        return None
    return transcript_path


def read_tick(pointer_path: str) -> ReadResult:
    """Produce a ReadResult for the transcript the pointer currently names.

    Steps: read the pointer file for the current transcript path, full re-read
    that transcript, parse every line with parse_line, and drop lines that parse
    to None. Never raises; every failure mode returns an empty no-op ReadResult.
    """
    transcript_path = _read_pointer(pointer_path)
    if transcript_path is None:
        return ReadResult()

    content = _read_text(transcript_path)
    if content is None:
        return ReadResult()

    records: list[TranscriptRecord] = []
    for line in content.split("\n"):
        record = parse_line(line)
        if record is not None:
            records.append(record)

    return ReadResult(records=records, transcript_path=transcript_path)
