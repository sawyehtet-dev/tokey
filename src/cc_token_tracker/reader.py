"""Poll-based reader: turn the live transcript into records.

One tick per call. This module does not own a sleep or poll loop; the caller
drives cadence. Each tick does a full re-read of the resolved transcript (no
byte-offset tracking) and parses every line with the existing parse_line.
Records that parse to None are dropped.

Full re-read is deliberate: account_usage dedupes by message_id in first-seen
order, so re-reading the whole file each tick is idempotent downstream.

The reader never raises. Any failure (no transcript resolved, transcript
missing or unreadable) resolves to a no-op tick: an empty ReadResult with
transcript_path None. A truncated final line that does not parse is dropped
while the prior records are kept.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from cc_token_tracker.parser import TranscriptRecord, parse_line

__all__ = [
    "ReadResult",
    "read_transcript",
    "find_active_transcript",
]


@dataclass(frozen=True)
class ReadResult:
    """Records parsed from the current transcript on one tick.

    records holds parsed TranscriptRecord values in transcript order (the same
    shape Turn uses for its records). transcript_path is the transcript resolved
    on this tick, or None for a no-op tick.
    """

    records: list[TranscriptRecord] = field(default_factory=list)
    transcript_path: str | None = None


def read_transcript(transcript_path: str | None) -> ReadResult:
    """Full re-read one already-resolved transcript path into a ReadResult.

    This owns the transcript read only -- it does not decide WHICH file. The path
    is resolved upstream by find_active_transcript and fed here, keeping
    resolution and reading separate.

    A None path -- nothing resolved this tick -- is the no-op tick: an empty
    ReadResult with transcript_path None, the same shape an unreadable transcript
    yields. Otherwise every line is parsed with parse_line and records that parse
    to None are dropped. Never raises.
    """
    if transcript_path is None:
        return ReadResult()

    try:
        with open(transcript_path, "r", encoding="utf-8",
                  errors="replace") as handle:
            records: list[TranscriptRecord] = []
            for line in handle:
                record = parse_line(line.rstrip("\n"))
                if record is not None:
                    records.append(record)
    except OSError:
        return ReadResult()

    return ReadResult(records=records, transcript_path=transcript_path)


def find_active_transcript() -> str | None:
    """Resolve WHICH transcript is active by recency, with no configuration.

    Locate Claude Code's projects directory (~/.claude/projects), recursively
    scan it for *.jsonl transcripts, and return the path of the most recently
    modified one. Returns None when that directory does not exist or holds no
    .jsonl file.

    Resolution only: this opens nothing for reading and parses nothing. It just
    answers which path; read_transcript does the reading. Per-entry filesystem
    access is wrapped so a transient failure (a file deleted mid-scan, a
    permission error on a single entry) skips that entry and continues rather
    than crashing the scan.
    """
    projects_dir = os.path.expanduser("~/.claude/projects")
    if not os.path.isdir(projects_dir):
        return None

    newest_path: str | None = None
    newest_mtime: float | None = None
    for root, _dirs, files in os.walk(projects_dir):
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            candidate = os.path.join(root, name)
            try:
                mtime = os.path.getmtime(candidate)
            except OSError:
                continue  # vanished mid-scan or unreadable: skip this entry
            if newest_mtime is None or mtime > newest_mtime:
                newest_mtime = mtime
                newest_path = candidate
    return newest_path
