"""Per-session open/close markers written by the Claude Code hooks.

The roster's authoritative liveness signal. A transcript-mtime heuristic cannot
tell an idle-but-open session from a closed one (both just stop being written),
so liveness instead reads markers that Claude Code's SessionStart/SessionEnd
hooks drop here (see :mod:`cc_token_tracker.hook`):

- SessionStart writes an OPEN marker the instant a session starts, before its
  first prompt -- so a brand-new session shows in the roster immediately rather
  than only once its transcript file exists.
- SessionEnd OVERWRITES the same file as a CLOSED marker (a tombstone), so a
  session the user exits drops out of the roster on the next tick instead of
  lingering "active" while its transcript mtime ages out.

A marker is one small JSON file per session id under ``sessions/`` in the
tracker's base dir. The write is atomic (mkstemp + os.replace) so a polling
reader never sees a half-written file, and the parent dir is self-created so a
fresh install does not silently fail. Nothing here raises for filesystem or
parse reasons; a bad marker is skipped, a failed write returns False.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass

__all__ = [
    "DEFAULT_MARKERS_DIR",
    "OPEN",
    "CLOSED",
    "MARKER_STALE_AFTER_SECONDS",
    "MarkerInfo",
    "write_marker",
    "read_markers",
]

# Where the hooks write and the roster reads. Under the tracker base dir the
# (removed) pointer mechanism already created.
DEFAULT_MARKERS_DIR = os.path.expanduser("~/.claude/cc_token_tracker/sessions")

# The two hook events recorded, named so callers compare to a constant. The
# string values are Claude Code's ``hook_event_name`` verbatim.
OPEN = "SessionStart"
CLOSED = "SessionEnd"

# Crash-recovery bound. An OPEN marker whose last activity (the later of its own
# timestamp and the transcript mtime) is older than this is treated as dropped:
# it covers a hard kill / closed terminal where SessionEnd never fired. The
# normal close path is the CLOSED tombstone, which drops instantly regardless of
# this bound; this only keeps a crashed session's marker from lingering forever.
MARKER_STALE_AFTER_SECONDS = 2 * 3600.0

# Closed tombstones older than this are unlinked on read so the dir does not grow
# without bound. Matches the roster's 7-day discovery window.
_TOMBSTONE_TTL_SECONDS = 7 * 86400.0


@dataclass(frozen=True)
class MarkerInfo:
    """One session's latest marker: which transcript, which event, when.

    ``event`` is :data:`OPEN` or :data:`CLOSED`. ``ts`` is when the marker was
    written (epoch seconds). ``transcript_path`` is the transcript Claude Code
    will write -- known at SessionStart, before the file itself exists -- and is
    the key the roster matches a marker to a discovered session by.
    """

    session_id: str
    transcript_path: str
    cwd: str
    event: str
    ts: float


def write_marker(
    session_id: str,
    transcript_path: str,
    cwd: str,
    event: str,
    *,
    markers_dir: str | None = None,
    now: float | None = None,
) -> bool:
    """Atomically write one session's marker. Return True on success.

    The file is ``<markers_dir>/<session_id>.json``; SessionEnd overwrites the
    same path SessionStart wrote, turning the open marker into a closed
    tombstone. The temp file is created in the markers dir so ``os.replace``
    stays on one filesystem, and the parent dir is created first so a fresh
    install does not fail. Any OSError (bad permissions, an uncreatable dir)
    cleans up the temp file and returns False. Never raises.
    """
    if markers_dir is None:
        markers_dir = DEFAULT_MARKERS_DIR
    if now is None:
        now = time.time()
    # session_id becomes a filename: strip any path separators (it is a uuid in
    # practice, but a path component is never trusted raw).
    safe = os.path.basename(str(session_id))
    if not safe:
        return False
    target = os.path.join(markers_dir, f"{safe}.json")
    payload = json.dumps(
        {
            "session_id": session_id,
            "transcript_path": transcript_path,
            "cwd": cwd,
            "event": event,
            "ts": now,
        }
    )

    fd: int | None = None
    temp_path: str | None = None
    try:
        os.makedirs(markers_dir, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(dir=markers_dir)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None  # the file object owns the descriptor now
            handle.write(payload)
            handle.flush()
        os.replace(temp_path, target)
        return True
    except OSError:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        return False


def _parse_marker(path: str) -> MarkerInfo | None:
    """Read and validate one marker file, or None if unusable. Never raises."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            blob = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(blob, dict):
        return None
    transcript_path = blob.get("transcript_path")
    event = blob.get("event")
    ts = blob.get("ts")
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    if event not in (OPEN, CLOSED):
        return None
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    session_id = blob.get("session_id")
    cwd = blob.get("cwd")
    return MarkerInfo(
        session_id=session_id if isinstance(session_id, str) else "",
        transcript_path=transcript_path,
        cwd=cwd if isinstance(cwd, str) else "",
        event=event,
        ts=float(ts),
    )


def read_markers(
    markers_dir: str | None = None, *, now: float | None = None
) -> dict[str, MarkerInfo]:
    """Read every session marker, keyed by transcript path. Never raises.

    Each ``*.json`` under ``markers_dir`` is parsed; unreadable or malformed
    files are skipped. Returns a dict from ``transcript_path`` to its
    :class:`MarkerInfo` so the roster can match a marker to a discovered session
    in O(1); when two markers name the same transcript the newest ``ts`` wins.
    Closed tombstones older than the 7-day window are unlinked as dir hygiene
    (best-effort; an unlink failure is ignored). A missing markers dir yields an
    empty dict.
    """
    if markers_dir is None:
        markers_dir = DEFAULT_MARKERS_DIR
    if now is None:
        now = time.time()
    try:
        names = os.listdir(markers_dir)
    except OSError:
        return {}

    by_path: dict[str, MarkerInfo] = {}
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(markers_dir, name)
        marker = _parse_marker(path)
        if marker is None:
            continue
        if marker.event == CLOSED and now - marker.ts > _TOMBSTONE_TTL_SECONDS:
            try:
                os.unlink(path)
            except OSError:
                pass
            continue
        existing = by_path.get(marker.transcript_path)
        if existing is None or marker.ts >= existing.ts:
            by_path[marker.transcript_path] = marker
    return by_path
