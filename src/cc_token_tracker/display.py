"""Live token-usage display (Ticket 7).

A long-running process that renders Claude Code token usage. Each tick it reads
the current transcript through the existing reader, runs the existing pipeline,
and shows two numbers: the per-command delta (the current or most-recent turn)
and the session total (the whole current transcript).

This layer CONSUMES the layers below and reimplements none of them. The session
total is account_usage over the records, the single accounting source; it is not
recomputed by summing turn totals.

Session-total semantics are RESET: the total tracks the current transcript only.
When the reader returns a new transcript_path, the frame rebases to that
transcript alone. This falls out of recomputing from the current tick's records
each tick; no cross-session state is held.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass

from cc_token_tracker.accounting import account_usage
from cc_token_tracker.reader import ReadResult, read_tick
from cc_token_tracker.segmentation import segment_turns
from cc_token_tracker.shim import DEFAULT_POINTER_PATH
from cc_token_tracker.turn_cost import TurnCost, turn_costs

__all__ = ["Frame", "compute_frame", "DisplayState", "run", "DEFAULT_POINTER_PATH"]

_LOG = logging.getLogger(__name__)

# Terminal control for in-place redraw (no scroll spam).
_HIDE_CURSOR = "\x1b[?25l"
_SHOW_CURSOR = "\x1b[?25h"
_CLEAR_LINE = "\x1b[2K"


@dataclass(frozen=True)
class Frame:
    """One render's worth of state.

    delta is the most-recent turn's TurnCost (the per-command number) or None
    when the transcript has no turns yet. session_total is the transcript-wide
    total from account_usage. transcript_path is the transcript this frame
    describes, or None for the initial waiting frame.
    """

    delta: TurnCost | None
    session_total: int
    transcript_path: str | None


# The frame shown before any real transcript has been seen, and the frame a
# no-op tick holds onto until there is a better one.
_WAITING_FRAME = Frame(delta=None, session_total=0, transcript_path=None)


def compute_frame(result: ReadResult) -> Frame:
    """Build a Frame from one ReadResult. Pure; never raises.

    session_total comes from account_usage over all records (the single
    accounting source). The per-command delta is the LAST TurnCost from the
    existing segment_turns / turn_costs pipeline, or None when there are no
    turns. Empty records and a turnless transcript both yield a zero/None frame
    rather than an error.
    """
    session_total = account_usage(result.records).session_total
    costs = turn_costs(segment_turns(result.records))
    delta = costs[-1] if costs else None
    return Frame(
        delta=delta,
        session_total=session_total,
        transcript_path=result.transcript_path,
    )


class DisplayState:
    """Holds the last good Frame across ticks."""

    def __init__(self) -> None:
        self._last = _WAITING_FRAME

    @property
    def last_frame(self) -> Frame:
        return self._last

    def update(self, result: ReadResult) -> Frame:
        """Fold one ReadResult into the display state and return the frame.

        A no-op tick (transcript_path is None: pointer absent, empty, or
        unreadable) HOLDS the last good frame unchanged. The hold is keyed off
        the missing path, never off empty records, so a glitchy tick cannot blank
        a real reading.

        A tick with a real transcript_path always recomputes a fresh frame for
        THAT transcript and makes it the new last frame. A real but still
        turnless transcript therefore shows its own zero/waiting frame, never the
        prior session's stale total. This is what makes a session switch rebase
        to the new transcript alone (RESET).
        """
        if result.transcript_path is None:
            return self._last
        frame = compute_frame(result)
        self._last = frame
        return frame


def _format_frame(frame: Frame) -> str:
    """Render a frame to a single cosmetic line. Not a tested contract."""
    name = os.path.basename(frame.transcript_path) if frame.transcript_path else "no session"
    if frame.delta is None:
        delta_part = "waiting for first command"
    else:
        d = frame.delta
        in_flight = "" if d.complete else " (in-flight)"
        delta_part = (
            "delta in=%d cache_create=%d cache_read=%d out=%d total=%d%s"
            % (
                d.input_tokens,
                d.cache_creation_input_tokens,
                d.cache_read_input_tokens,
                d.output_tokens,
                d.turn_total,
                in_flight,
            )
        )
    return "%s | session %d | %s" % (delta_part, frame.session_total, name)


def _render(frame: Frame) -> None:
    """Redraw the frame in place on stdout."""
    sys.stdout.write("\r" + _CLEAR_LINE + _format_frame(frame))
    sys.stdout.flush()


def run(pointer_path: str | None = None, interval: float = 1.0) -> int:
    """Poll loop: read_tick, fold, render, sleep. Returns an exit code.

    With pointer_path None it defaults to the shim's DEFAULT_POINTER_PATH (the
    same constant the shim writes), so the reader watches the file the shim
    maintains. A single tick that raises is logged and skipped so one bad read
    cannot kill a long-running process. KeyboardInterrupt restores the terminal
    and exits cleanly (0).
    """
    if pointer_path is None:
        pointer_path = DEFAULT_POINTER_PATH

    state = DisplayState()
    sys.stdout.write(_HIDE_CURSOR)
    sys.stdout.flush()
    try:
        while True:
            try:
                frame = state.update(read_tick(pointer_path))
                _render(frame)
            except Exception:  # noqa: BLE001 - one bad tick must not kill us
                _LOG.exception("display tick failed; continuing")
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        # Restore the terminal: show the cursor and move off the status line.
        sys.stdout.write(_SHOW_CURSOR + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(run())
