"""Live token-usage display.

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

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from cc_token_tracker.accounting import account_usage
from cc_token_tracker.reader import ReadResult, read_tick
from cc_token_tracker.segmentation import segment_turns
from cc_token_tracker.shim import DEFAULT_POINTER_PATH
from cc_token_tracker.turn_cost import TurnCost, turn_costs

__all__ = [
    "Frame",
    "compute_frame",
    "DisplayState",
    "render_panel",
    "run",
    "main",
    "DEFAULT_POINTER_PATH",
]

_LOG = logging.getLogger(__name__)

# One accent color carries the per-command delta (the differentiator) and its
# brief flash on a new prompt; everything else stays monochrome. See
# render_panel. The flash lasts about a second, derived from the poll interval.
_ACCENT = "cyan"
_FLASH_SECONDS = 1.0


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


def _num(value: int) -> str:
    """Group thousands so the figures stay readable at a glance."""
    return f"{value:,}"


def _figure_grid(columns: list[tuple[str, Text]]) -> Table:
    """A label-over-value grid: dim labels on top, figures beneath, spread
    evenly across the panel width."""
    grid = Table.grid(expand=True, padding=(0, 2))
    for _ in columns:
        grid.add_column(justify="center", ratio=1)
    grid.add_row(*(Text(label, style="dim") for label, _ in columns))
    grid.add_row(*(value for _, value in columns))
    return grid


def render_panel(frame: Frame, *, flash: bool = False) -> Panel:
    """Render one Frame to a rich Panel. Pure: Frame in, renderable out.

    No IO, no clock, no global state. The per-command delta is the visual focus
    and the only thing in the accent color; ``flash`` (decided by the loop, never
    here) briefly brightens it when a new completed delta lands. IN folds input
    and cache-creation together; CACHE READ is shown separately. The session row
    shows only the TOTAL the Frame exposes -- session IN/OUT are not on Frame and
    are deliberately not recomputed here (that would cross into accounting).
    """
    delta = frame.delta

    # LAST PROMPT: the visual focus.
    last_label = Text("LAST PROMPT", style="bold")
    if delta is None:
        last_body: Text | Table = Text(
            "waiting for first command", style="dim italic"
        )
    else:
        if not delta.complete:
            last_label.append("  running...", style="dim")
            value_style = "dim"
        elif flash:
            value_style = f"bold reverse {_ACCENT}"
        else:
            value_style = f"bold {_ACCENT}"
        in_tokens = delta.input_tokens + delta.cache_creation_input_tokens
        last_body = _figure_grid(
            [
                ("IN", Text(_num(in_tokens), style=value_style)),
                ("OUT", Text(_num(delta.output_tokens), style=value_style)),
                ("CACHE READ",
                 Text(_num(delta.cache_read_input_tokens), style=value_style)),
            ]
        )

    # SESSION TOTAL: only the whole-transcript TOTAL is exposed on Frame.
    session_body = _figure_grid(
        [("TOTAL", Text(_num(frame.session_total), style="bold"))]
    )

    body = Group(
        last_label,
        last_body,
        Rule(style="dim"),
        Text("SESSION TOTAL", style="bold"),
        session_body,
    )

    subtitle = (
        Text(os.path.basename(frame.transcript_path), style="dim")
        if frame.transcript_path
        else None
    )
    return Panel(
        body,
        title=Text("Tokey", style="bold"),
        subtitle=subtitle,
        box=box.ROUNDED,
        padding=(1, 4),
    )


_UNSET = object()


class _FlashState:
    """Loop-local render state for the new-prompt flash.

    Deliberately NOT part of DisplayState (the tested accounting-hold layer). It
    remembers the previous tick's delta total and, when a NEW completed delta
    lands, asks render_panel to flash the LAST PROMPT figures for about a second.
    """

    def __init__(self, interval: float = 1.0,
                 flash_seconds: float = _FLASH_SECONDS) -> None:
        self._prev_total: object = _UNSET
        self._ticks_left = 0
        self._flash_ticks = (
            max(1, round(flash_seconds / interval)) if interval > 0 else 1
        )

    def observe(self, frame: Frame) -> bool:
        """Fold one frame and return whether this tick should flash.

        Flash fires when a completed delta's total differs from the previous
        tick's total. The very first tick never flashes (nothing to compare to),
        so attaching to an already-running session stays quiet.
        """
        delta = frame.delta
        current = delta.turn_total if delta is not None else None
        if (
            delta is not None
            and delta.complete
            and self._prev_total is not _UNSET
            and current != self._prev_total
        ):
            self._ticks_left = self._flash_ticks
        self._prev_total = current
        flashing = self._ticks_left > 0
        if self._ticks_left > 0:
            self._ticks_left -= 1
        return flashing


def run(pointer_path: str | None = None, interval: float = 1.0) -> int:
    """Poll loop: read_tick, fold, render into a rich.Live panel, sleep.

    With pointer_path None it defaults to the shim's DEFAULT_POINTER_PATH (the
    same constant the shim writes), so the reader watches the file the shim
    maintains. The Live context redraws the panel in place each tick -- no
    per-tick newline. A single tick that raises is logged and skipped so one bad
    read cannot kill a long-running process. KeyboardInterrupt exits the Live
    cleanly, leaves the terminal usable, and returns 0.
    """
    if pointer_path is None:
        pointer_path = DEFAULT_POINTER_PATH

    state = DisplayState()
    flash = _FlashState(interval=interval)
    console = Console()
    try:
        with Live(console=console, auto_refresh=False, screen=False) as live:
            while True:
                try:
                    frame = state.update(read_tick(pointer_path))
                    live.update(
                        render_panel(frame, flash=flash.observe(frame)),
                        refresh=True,
                    )
                except Exception:  # noqa: BLE001 - one bad tick must not kill us
                    _LOG.exception("display tick failed; continuing")
                time.sleep(interval)
    except KeyboardInterrupt:
        pass
    return 0


def main() -> int:
    """Console-script entry point: run the display with its defaults.

    A thin wrapper so the console_scripts mapping and ``python -m`` share ONE
    path into run(). It adds no argv parsing, config, or behavior; run() already
    handles the poll loop, the clean KeyboardInterrupt exit, and the exit code.
    """
    return run()


if __name__ == "__main__":
    sys.exit(main())
