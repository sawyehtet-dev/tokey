"""Session liveness classification: active / closing / dropped (v0.6.0, T1).

A pure, presentation-scope helper. A session's liveness is derived ONLY from
how long ago its transcript was last written -- the file mtime captured during
discovery (``SessionSummary.last_write``, an ``os.stat`` field). No transcript
is opened or re-read to derive activity; the mtime is free from the stat the
discovery pass already does.

The boundaries are exact and authoritative (``age = now - last_write``, in
seconds, half-open on the low side):

    age < 600            -> "active"
    600 <= age < 720     -> "closing"
    age >= 720           -> "dropped"

This module decides the LABEL only. Roster scope -- which states stay on screen
and which count toward the header's active figure -- lives with the panel
assembly that consumes these labels; see :func:`cc_token_tracker.roster.build_roster_view`.
"""

from __future__ import annotations

from cc_token_tracker.markers import (
    CLOSED,
    MARKER_STALE_AFTER_SECONDS,
    OPEN,
)

__all__ = [
    "ACTIVE",
    "CLOSING",
    "DROPPED",
    "CLOSING_AFTER_SECONDS",
    "DROPPED_AFTER_SECONDS",
    "classify_liveness",
    "classify_with_marker",
]

# The three labels, named so callers compare against a constant rather than a
# bare string literal.
ACTIVE = "active"
CLOSING = "closing"
DROPPED = "dropped"

# A session reads "active" while younger than this, "closing" from here until
# DROPPED_AFTER_SECONDS, and "dropped" at or beyond it. The bounds are exact:
# an age of exactly 600 is closing, exactly 720 is dropped.
CLOSING_AFTER_SECONDS = 600.0
DROPPED_AFTER_SECONDS = 720.0


def classify_liveness(now: float, last_write: float) -> str:
    """Label one session from its transcript mtime: active/closing/dropped.

    ``now`` and ``last_write`` are POSIX timestamps; ``last_write`` is the
    transcript file mtime (``os.stat(...).st_mtime``). The age is
    ``now - last_write``; see the module docstring for the exact, authoritative
    boundaries. A negative age (clock skew making a file look future-dated) is
    younger than every boundary and so reads "active".
    """
    age = now - last_write
    if age < CLOSING_AFTER_SECONDS:
        return ACTIVE
    if age < DROPPED_AFTER_SECONDS:
        return CLOSING
    return DROPPED


def classify_with_marker(
    now: float,
    last_write: float | None,
    marker_event: str | None,
    marker_ts: float | None,
) -> str:
    """Liveness from a session marker, falling back to transcript mtime.

    The marker (written by the SessionStart/SessionEnd hooks; see
    :mod:`cc_token_tracker.markers`) is authoritative when present:

    - a CLOSED marker (``markers.CLOSED`` / "SessionEnd") -> DROPPED at once,
      even if the transcript was just written, so an exited session leaves the
      roster on the next tick instead of lingering "active" for ten minutes;
    - an OPEN marker (``markers.OPEN`` / "SessionStart") -> ACTIVE, unless its
      last activity (the later of the marker's own timestamp and the transcript
      mtime) is older than :data:`cc_token_tracker.markers.MARKER_STALE_AFTER_SECONDS`,
      which means a crash or hard kill left the marker un-closed -> DROPPED
      (crash recovery).

    With no marker (legacy sessions, or a machine whose Claude Code does not run
    the hooks) liveness falls back to the transcript-mtime
    :func:`classify_liveness` -- the pre-marker behavior, unchanged. ``now``,
    ``last_write`` and ``marker_ts`` are POSIX timestamps; ``last_write`` may be
    ``None`` only for a marker-only session (no transcript yet), which always has
    an OPEN marker and so never reaches the fallback.
    """
    if marker_event == CLOSED:
        return DROPPED
    if marker_event == OPEN:
        beats = [t for t in (marker_ts, last_write) if t is not None]
        last_activity = max(beats) if beats else now
        if now - last_activity > MARKER_STALE_AFTER_SECONDS:
            return DROPPED
        return ACTIVE
    # No marker: pre-marker, mtime-only behavior, unchanged.
    return classify_liveness(now, last_write if last_write is not None else now)
