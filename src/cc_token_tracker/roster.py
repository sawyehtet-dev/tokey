"""Multi-session roster view: the v0.5 default tokey screen.

One panel listing every discovered session (newest first, 7-day window), the
active one marked ▶ and auto-expanded inline with a context gauge plus the
SAME hero (LAST PROMPT) section the single-session panel renders -- composed
from display's existing component functions (``_figure_grid``, ``_cost_figure``,
``_num``), never reimplemented. The RECENT strip was dropped product-wide in
v0.6.0; the roster renders the hero only. No keyboard input: the view
is render-only, and the active row follows recency exactly like the v0.3+
auto-follow (the newest transcript is the active one, so it is always the top
row).

Liveness scope (v0.6.0): each row carries an active/closing/dropped label from
its transcript mtime (:mod:`cc_token_tracker.liveness`). Dropped sessions leave
the roster; the header counts the live ("active") ones only; closing sessions
stay visible but uncounted. The footer's all-sessions totals are unaffected and
still cover every discovered session.

Honesty markers carried into every cell:
- COST: ``$?`` when nothing in the session could be priced; a trailing ``?``
  (``$1.23?``) when the figure is partial because some turn went unpriced.
- CONTEXT: ``?`` when the limit is unknown (model absent from the limits
  table); a trailing ``?`` (``104%?``) when the estimate exceeds the limit.
The percent is an ESTIMATE from the last prompt's input-side token counts; see
:mod:`cc_token_tracker.context`.
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, replace

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from cc_token_tracker.display import (
    _ACCENT,
    MAX_PANEL_WIDTH,
    DisplayState,
    Frame,
    _cost_figure,
    _figure_grid,
    _FlashState,
    _num,
    _read_for_tick,
)
from cc_token_tracker.liveness import ACTIVE, DROPPED, classify_liveness
from cc_token_tracker.sessions import SessionCache, SessionSummary

__all__ = [
    "ROSTER_LIMIT",
    "RosterView",
    "build_roster_view",
    "percent_figure",
    "cost_figure",
    "age_figure",
    "render_roster",
    "run",
    "main",
]

_LOG = logging.getLogger(__name__)

# At most this many session rows render; overflow becomes a "+N more" line
# above the footer. The footer totals still cover every discovered session.
ROSTER_LIMIT = 10

# Width of the expanded row's context bar, in cells.
_BAR_WIDTH = 30

# Fixed widths for the numeric columns so the collapsed rows, which render as
# separate grids around the expanded block, stay column-aligned. PROJECT takes
# the remaining width.
_COL_MARKER = 2
_COL_TOTAL_TOK = 11
_COL_COST = 9
_COL_CONTEXT = 8
_COL_LAST = 8


@dataclass(frozen=True)
class RosterView:
    """One render pass's presentation scope over the session summaries.

    ``sessions`` is the on-screen roster: every summary whose liveness is not
    "dropped" (so active + closing), newest first, each carrying its freshly
    computed ``state``. ``active_count`` counts the "active" ones ONLY --
    closing sessions stay on screen as rows but are never counted. Dropped
    sessions are absent from ``sessions`` entirely. This is presentation, not
    accounting: the cost and token figures inside each summary are reused
    verbatim, never recomputed here.
    """

    sessions: list[SessionSummary]
    active_count: int


def build_roster_view(
    summaries: list[SessionSummary], *, now: float
) -> RosterView:
    """Stamp liveness onto ``summaries`` and derive the panel's roster scope.

    Each summary is re-stamped with ``state = classify_liveness(now,
    last_write)`` (the field is presentation-only; see
    :class:`cc_token_tracker.sessions.SessionSummary`). The roster keeps the
    non-dropped ones in the given order; the active count is the number of
    "active" survivors. Pure given ``now``: no IO, no re-parsing, no touching
    of the frozen cost outputs.
    """
    staged = [
        replace(summary, state=classify_liveness(now, summary.last_write))
        for summary in summaries
    ]
    sessions = [summary for summary in staged if summary.state != DROPPED]
    active_count = sum(1 for summary in sessions if summary.state == ACTIVE)
    return RosterView(sessions=sessions, active_count=active_count)


def _active_header(active_count: int) -> Text:
    """Top line: how many sessions are live right now -- "active" only.

    Closing sessions render as rows below but are deliberately left out of this
    figure; dropped sessions are gone from the roster entirely. Presentation
    only: this count never touches the footer's cost/token totals.
    """
    return Text.assemble(
        (str(active_count), f"bold {_ACCENT}"),
        (" active", "dim"),
    )


def percent_figure(percent: float | None) -> str:
    """The CONTEXT cell: ``NN%``, ``NNN%?`` past 100, ``?`` when unknown.

    An unknown limit yields ``?`` (the limits table never guesses). A percent
    above 100 keeps its number but gains a trailing ``?`` -- the estimate
    overflowed the documented window, and the marker says so instead of
    clamping to a clean-looking 100%.
    """
    if percent is None:
        return "?"
    figure = f"{round(percent)}%"
    return figure + "?" if percent > 100 else figure


def cost_figure(total_cost_usd: float, unpriced: bool) -> str:
    """A session's COST cell, with the unpriced marker surviving.

    A fully unpriceable session ($0 summed, flag set) renders ``$?`` -- the
    same never-$0.00 rule as the per-turn figure. A partial total (some turns
    priced, some not) renders its dollars with a trailing ``?`` so the figure
    reads as a floor, not a total.
    """
    if unpriced and total_cost_usd == 0.0:
        return "$?"
    figure = f"${total_cost_usd:.2f}"
    return figure + "?" if unpriced else figure


def age_figure(age_seconds: float) -> str:
    """Humanized age for the LAST column: ``4m ago``, ``1h ago``, ``2d ago``.

    Sub-minute ages render ``now``. The discovery window caps ages at days,
    so no larger unit is needed.
    """
    minutes = int(age_seconds // 60)
    if minutes < 1:
        return "now"
    if minutes < 60:
        return f"{minutes}m ago"
    hours = int(age_seconds // 3600)
    if hours < 24:
        return f"{hours}h ago"
    return f"{int(age_seconds // 86400)}d ago"


def _row_grid() -> Table:
    """One column spec shared by the header and every row grid.

    Fixed widths on the marker and numeric columns keep separate grids
    aligned (the expanded block splits the rows into grids above and below
    it); PROJECT flexes to the remaining width and truncates with an
    ellipsis.
    """
    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(width=_COL_MARKER)                       # ▶ marker
    grid.add_column(justify="left", ratio=1, no_wrap=True,
                    overflow="ellipsis")                     # PROJECT
    grid.add_column(justify="right", width=_COL_TOTAL_TOK)   # TOTAL TOK
    grid.add_column(justify="right", width=_COL_COST)        # COST
    grid.add_column(justify="right", width=_COL_CONTEXT)     # CONTEXT
    grid.add_column(justify="right", width=_COL_LAST)        # LAST
    return grid


def _header_row() -> Table:
    grid = _row_grid()
    grid.add_row(
        Text(""),
        Text("PROJECT", style="dim"),
        Text("TOTAL TOK", style="dim"),
        Text("COST", style="dim"),
        Text("CONTEXT", style="dim"),
        Text("LAST", style="dim"),
    )
    return grid


def _session_row(summary: SessionSummary, *, now: float) -> Table:
    """One collapsed row. The active row gets the ▶ marker, bold project, and
    ``active`` in the LAST column; others show their humanized age, dim."""
    active = summary.is_active
    style = "" if active else "dim"
    last_cell = (
        Text("active", style=_ACCENT)
        if active
        else Text(age_figure(now - summary.last_write), style="dim")
    )
    grid = _row_grid()
    grid.add_row(
        Text("▶", style=_ACCENT) if active else Text(""),
        Text(summary.project, style="bold" if active else "dim",
             no_wrap=True, overflow="ellipsis"),
        Text(_num(summary.total_tokens), style=style),
        Text(cost_figure(summary.total_cost_usd, summary.unpriced),
             style=style),
        Text(percent_figure(summary.context_percent), style=style),
        last_cell,
    )
    return grid


def _context_lines(summary: SessionSummary) -> list:
    """The expanded row's context gauge: the used/limit line, the bar, and the
    percent line, honest about every unknown.

    Unknown used or limit renders ``?`` in its slot. The bar and the
    ``~Nk left`` remainder need a real percent/limit, so they are omitted
    (not faked) when the limit is unknown; an over-100 estimate fills the bar
    completely and shows ``~0k left`` beside the ``NNN%?`` marker.
    """
    used, limit = summary.context_used, summary.context_limit
    used_figure = _num(used) if used is not None else "?"
    limit_figure = _num(limit) if limit is not None else "?"
    lines: list = [
        Text.assemble(
            ("CONTEXT", "bold"),
            (" · ", "dim"),
            (f"{used_figure} / {limit_figure} tokens", ""),
        )
    ]
    percent = summary.context_percent
    if percent is None:
        lines.append(Text("context limit unknown for this model", style="dim"))
        return lines
    filled = round(min(percent, 100.0) / 100.0 * _BAR_WIDTH)
    lines.append(
        Text("█" * filled, style=_ACCENT)
        + Text("░" * (_BAR_WIDTH - filled), style="dim")
    )
    remaining_k = max(0, (limit or 0) - (used or 0)) // 1000
    lines.append(
        Text.assemble(
            (percent_figure(percent), "bold"),
            (" · ", "dim"),
            (f"~{remaining_k}k left", "dim"),
        )
    )
    return lines


def _hero_section(frame: Frame, *, flash: bool) -> list:
    """The LAST PROMPT block, composed from display's components with the SAME
    labels, folding, and styles as render_panel (running... dimming, flash
    accent, IN folding cache creation into input). The figure logic itself --
    grid layout, thousands grouping, the $?-never-$0.00 cost cell -- is
    display's, reused not reimplemented."""
    delta = frame.delta
    label = Text("LAST PROMPT", style="bold")
    if delta is None:
        return [label, Text("waiting for first command", style="dim italic")]
    if not delta.complete:
        label.append("  running...", style="dim")
        value_style = "dim"
    elif flash:
        value_style = f"bold reverse {_ACCENT}"
    else:
        value_style = f"bold {_ACCENT}"
    in_tokens = delta.input_tokens + delta.cache_creation_input_tokens
    body = _figure_grid(
        [
            ("IN", Text(_num(in_tokens), style=value_style)),
            ("OUT", Text(_num(delta.output_tokens), style=value_style)),
            ("CACHE READ",
             Text(_num(delta.cache_read_input_tokens), style=value_style)),
            ("COST", Text(_cost_figure(delta), style=value_style)),
        ],
        divider=True,
    )
    return [label, body]


def _expanded_block(
    summary: SessionSummary, frame: Frame, *, flash: bool
) -> Padding:
    """Everything inside the active row's expansion, indented under the row:
    the context gauge, then the reused hero (LAST PROMPT) section. The RECENT
    strip was removed product-wide in v0.6.0; ``frame.recent`` is left untouched
    (compute_frame still populates it) but the roster no longer renders it."""
    items = _context_lines(summary)
    items.append(Text(""))
    items.extend(_hero_section(frame, flash=flash))
    return Padding(Group(*items), (0, 0, 0, _COL_MARKER + 1))


def _footer(active: list[SessionSummary]) -> Table:
    """The ACTIVE-ONLY total: ``active: $X.XX · N.NNM tok``, with
    ``(+ unpriced)`` appended when ANY active session carries the flag (the
    dollar figure then covers the priceable turns only, and says so). Scope
    matches the header's active count exactly: closing and dropped sessions are
    excluded, while active rows hidden by the ROSTER_LIMIT cap are still summed
    in. No session count -- the header already states how many are active."""
    total_cost = sum(s.total_cost_usd for s in active)
    total_tokens = sum(s.total_tokens for s in active)
    figure = f"active: ${total_cost:.2f} · {total_tokens / 1e6:.2f}M tok"
    if any(s.unpriced for s in active):
        figure += " (+ unpriced)"
    grid = Table.grid(expand=True)
    grid.add_column(justify="right", ratio=1)
    grid.add_row(Text(figure, style="bold"))
    return grid


def render_roster(
    summaries: list[SessionSummary],
    frame: Frame,
    *,
    flash: bool = False,
    width: int | None = None,
    now: float | None = None,
) -> Panel:
    """Render the roster to a rich Panel. Pure given ``now``; no IO.

    ``summaries`` is the session-cache output, newest first, the active entry
    flagged; ``frame`` is the live-path Frame whose hero/RECENT content fills
    the active row's expansion. Liveness scope is applied here (see
    :func:`build_roster_view`): dropped sessions leave the roster, the header
    counts only the live ("active") ones, and closing sessions stay visible but
    uncounted. Rows beyond ROSTER_LIMIT collapse into a "+N more" line above
    the footer; the footer total is ACTIVE-ONLY (the same scope as the header
    count): closing and dropped sessions are excluded, while active rows hidden
    by the cap are still summed. ``now`` drives both the liveness scope
    and the humanized LAST column (defaults to the current time; tests pin it).
    No input handling and no key hints exist in this view.
    """
    if now is None:
        now = time.time()

    view = build_roster_view(summaries, now=now)
    roster = view.sessions

    items: list = [_active_header(view.active_count)]
    if roster:
        items.append(_header_row())
        shown = roster[:ROSTER_LIMIT]
        for summary in shown:
            items.append(_session_row(summary, now=now))
            if summary.is_active:
                items.append(_expanded_block(summary, frame, flash=flash))
        omitted = len(roster) - len(shown)
        if omitted > 0:
            items.append(Text(f"+{omitted} more", style="dim"))
    else:
        items.append(Text("no sessions in the last 7 days", style="dim italic"))

    items.append(Rule(style="dim"))
    items.append(_footer([s for s in roster if s.state == ACTIVE]))

    active = next((s for s in roster if s.is_active), None)
    subtitle = Text(active.file_name, style="dim") if active else None
    return Panel(
        Group(*items),
        title=Text("Tokey", style="bold"),
        subtitle=subtitle,
        box=box.ROUNDED,
        padding=(1, 4),
        width=width,
    )


def run(interval: float = 1.0) -> int:
    """Poll loop: the roster as the default and only view.

    Same skeleton as the single-panel loop it replaces: the ACTIVE session
    refreshes through the existing live path (find_active_transcript ->
    read_transcript -> DisplayState), so auto-follow and the session-switch
    reset behave exactly as before; the other rows come from the session
    cache, which re-parses a transcript only when its (mtime, size) moves. A
    tick that raises is logged and skipped; KeyboardInterrupt exits cleanly.
    """
    state = DisplayState()
    flash = _FlashState(interval=interval)
    cache = SessionCache()
    console = Console()
    try:
        with Live(console=console, auto_refresh=False, screen=False) as live:
            while True:
                try:
                    frame = state.update(_read_for_tick())
                    summaries = cache.summaries()
                    target_width = min(console.width, MAX_PANEL_WIDTH)
                    live.update(
                        render_roster(
                            summaries,
                            frame,
                            flash=flash.observe(frame),
                            width=target_width,
                        ),
                        refresh=True,
                    )
                except Exception:  # noqa: BLE001 - one bad tick must not kill us
                    _LOG.exception("roster tick failed; continuing")
                time.sleep(interval)
    except KeyboardInterrupt:
        pass
    return 0


def main() -> int:
    """Console-script entry point: the roster with its defaults."""
    return run()


if __name__ == "__main__":
    sys.exit(main())
