"""Live token-usage display.

A long-running process that renders Claude Code token usage. Each tick it reads
the current transcript through the existing reader, runs the existing pipeline,
and shows the most-recent turn's cost (the hero), a short history of the prompts
behind it (the RECENT list, with a "+N more" line when some overflow), and the
session total for the whole current transcript.

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
from cc_token_tracker.pricing import normalize_model, turn_cost_usd
from cc_token_tracker.reader import (
    ReadResult,
    find_active_transcript,
    read_transcript,
)
from cc_token_tracker.segmentation import Turn, segment_turns
from cc_token_tracker.turn_cost import TurnCost, turn_costs

__all__ = [
    "Frame",
    "RecentEntry",
    "compute_frame",
    "DisplayState",
    "render_panel",
    "run",
    "main",
]

_LOG = logging.getLogger(__name__)

# One accent color carries the per-command delta (the differentiator) and its
# brief flash on a new prompt; everything else stays monochrome. See
# render_panel. The flash lasts about a second, derived from the poll interval.
_ACCENT = "cyan"
# Claude's signature rust/terracotta orange, used for the RECENT model tag only.
_MODEL_TAG_COLOR = "#D97757"
_FLASH_SECONDS = 1.0

# The history view keeps at most this many past turns behind the hero. One knob,
# easy to retune later; the renderer (a later ticket) decides how many it shows.
RECENT_LIMIT = 5

# Upper bound on the rendered panel width. On a narrow terminal the panel uses
# the full width; on a wide one it caps here instead of stretching edge to edge.
# One knob, easy to tune. The impure console-width read lives in run, not here.
MAX_PANEL_WIDTH = 100


@dataclass(frozen=True)
class RecentEntry:
    """One row of the v0.2 history view: a past turn's cost plus its prompt.

    cost reuses the existing TurnCost wholesale (do not copy out IN/OUT/CACHE
    fields). text is the typed-prompt snippet, populated in a later ticket; it
    defaults to empty so this commit only widens the shape.
    """

    cost: TurnCost
    text: str = ""


@dataclass(frozen=True)
class Frame:
    """One render's worth of state.

    delta is the most-recent turn's TurnCost (the per-command number) or None
    when the transcript has no turns yet. session_total is the transcript-wide
    total from account_usage. transcript_path is the transcript this frame
    describes, or None for the initial waiting frame. recent is the history
    view's backing tuple: the completed turns behind the hero, newest-first and
    capped, each a RecentEntry of cost plus typed-prompt snippet. recent_omitted
    is how many completed prompts are neither the hero nor in that capped tuple;
    render_panel draws it as the "+N more" overflow line. It defaults to 0 so
    existing constructions and the waiting frame hold.
    """

    delta: TurnCost | None
    session_total: int
    transcript_path: str | None
    recent: tuple[RecentEntry, ...] = ()
    recent_omitted: int = 0
    # Dollar total of the session: the SUM of each turn's individually-priced
    # cost (a session can mix models, so aggregate tokens times one rate would
    # be wrong). session_unpriced flags that at least one token-bearing turn
    # could not be priced -- the renderer marks the total partial rather than
    # silently undercounting. Both default for existing constructions and the
    # waiting frame.
    session_cost: float = 0.0
    session_unpriced: bool = False


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
    turns = segment_turns(result.records)
    costs = turn_costs(turns)
    delta = costs[-1] if costs else None
    recent, recent_omitted = _recent_entries(turns, costs)
    session_cost, session_unpriced = _session_cost(costs)
    return Frame(
        delta=delta,
        session_total=session_total,
        transcript_path=result.transcript_path,
        recent=recent,
        recent_omitted=recent_omitted,
        session_cost=session_cost,
        session_unpriced=session_unpriced,
    )


def _prompt_snippet(turn: Turn) -> str:
    """The turn's typed-prompt text, whitespace-collapsed; '' when absent.

    The opening record of a turn is the typed-prompt record segment_turns
    already selected, so we read its retained ``text`` directly and never
    re-derive which record that is. The full text is kept (no truncation -- that
    is the renderer's job); only runs of whitespace/newlines collapse to single
    spaces.
    """
    raw = turn.records[0].text if turn.records else None
    return " ".join(raw.split()) if raw else ""


def _recent_entries(
    turns: list[Turn], costs: list[TurnCost]
) -> tuple[tuple[RecentEntry, ...], int]:
    """The history view's backing tuple AND the count the cap dropped.

    The hero is the LAST turn -- ``compute_frame`` takes ``costs[-1]`` as the
    delta; ``recent`` is the completed turns behind it, newest-first, capped at
    ``RECENT_LIMIT``. The hero is dropped from ``recent`` only when it is itself
    a completed turn (no in-flight trailing turn). When a new prompt is in-flight
    the hero is that incomplete turn, so EVERY completed turn -- including the
    most recent -- belongs in ``recent``: a just-finished prompt shows up the
    instant the next one starts, not when it ends. Costs come straight from
    ``turn_costs`` (reused, never recomputed by hand); ``costs`` is aligned 1:1
    with ``turns``.

    The second return value is ``recent_omitted``: completed prompts that are
    neither the hero nor in the capped tuple, ``max(0, len(behind_hero) -
    len(entries))`` over the SAME ``behind_hero`` set the slice uses -- so the
    count can never disagree with what renders. No hero or empty recent both
    yield 0.
    """
    completed = [(turn, cost) for turn, cost in zip(turns, costs) if turn.complete]
    # The hero is the last turn (compute_frame's costs[-1]). Drop it from RECENT
    # only when it is itself completed; with an in-flight trailing turn the hero
    # is that incomplete turn, so no completed turn is the hero and none is dropped.
    hero_is_completed = bool(turns) and turns[-1].complete
    behind_hero = completed[:-1] if hero_is_completed else completed
    entries = tuple(
        RecentEntry(cost=cost, text=_prompt_snippet(turn))
        for turn, cost in reversed(behind_hero)
    )[:RECENT_LIMIT]
    recent_omitted = max(0, len(behind_hero) - len(entries))
    return entries, recent_omitted


class DisplayState:
    """Holds the last good Frame across ticks."""

    def __init__(self) -> None:
        self._last = _WAITING_FRAME

    @property
    def last_frame(self) -> Frame:
        return self._last

    def update(self, result: ReadResult) -> Frame:
        """Fold one ReadResult into the display state and return the frame.

        A no-op tick (transcript_path is None: no projects dir, no transcript
        yet, or the resolved transcript unreadable) HOLDS the last good frame
        unchanged. The hold is keyed off the missing path, never off empty
        records, so a glitchy tick cannot blank a real reading.

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


def _turn_usd(cost: TurnCost) -> float | None:
    """One turn's dollar cost via the existing pricing table, or None.

    Pricing is the existing :func:`turn_cost_usd` over the SAME four component
    counts the turn already renders -- nothing is recomputed here. No costUSD
    is passed: the parsed records do not carry one today, so the table compute
    applies (turn_cost_usd accepts one for when a caller has it).
    """
    return turn_cost_usd(
        cost.model,
        cost.input_tokens,
        cost.output_tokens,
        cost.cache_creation_input_tokens,
        cost.cache_read_input_tokens,
    )


def _cost_figure(delta: TurnCost) -> str:
    """A turn's dollar figure, or "$?" when the model is unknown.

    One rule for every per-turn cost cell -- the hero's COST field and each
    RECENT row alike: an unknown or absent model prices to None and renders as
    "$?", never $0.00. The token figures around it are unaffected.
    """
    cost = _turn_usd(delta)
    return "$?" if cost is None else f"${cost:.4f}"


# Family-name abbreviations for the RECENT model tag. Tags are derived, not
# enumerated per model id, so a new pricing row needs no second table here.
_FAMILY_TAGS = {"fable": "fab", "opus": "op", "sonnet": "sn", "haiku": "hk"}


def _model_tag(model: str | None) -> str:
    """Abbreviate a transcript model string to a short (<=6 char) tag, or "?".

    ``claude-opus-4-8`` -> ``op4.8``; ``claude-fable-5`` -> ``fab5``. A dated
    id normalizes first (reusing pricing's :func:`normalize_model`), so
    ``claude-haiku-4-5-20251001`` tags ``hk4.5``. Anything that does not parse
    as ``claude-<known family>-<version...>`` -- including None -- tags "?",
    the visual sibling of the "$?" pricing rule.
    """
    if model is None:
        return "?"
    parts = normalize_model(model).split("-")
    if len(parts) >= 2 and parts[0] == "claude" and parts[1] in _FAMILY_TAGS:
        return _FAMILY_TAGS[parts[1]] + ".".join(parts[2:])
    return "?"


def _session_cost(costs: list[TurnCost]) -> tuple[float, bool]:
    """(sum of per-turn dollar costs, whether any turn went unpriced).

    Each turn is priced individually with its OWN model, then the dollars are
    summed -- never aggregate tokens times a single rate, since a session can
    mix models. A turn that cannot be priced is left out of the sum and flips
    the unpriced flag so the renderer can mark the total as partial. EXCEPTION:
    a zero-token unpriceable turn (the just-opened in-flight prompt before any
    usage lands) contributes $0 exactly on any model, so it neither shifts the
    sum nor raises the flag -- otherwise the marker would flash on every new
    prompt.
    """
    total = 0.0
    unpriced = False
    for cost in costs:
        usd = _turn_usd(cost)
        if usd is not None:
            total += usd
        elif cost.turn_total:
            unpriced = True
    return total, unpriced


def _figure_grid(
    columns: list[tuple[str, Text]], *, divider: bool = False
) -> Table:
    """A label-over-value grid: dim labels on top, figures beneath, spread
    evenly across the panel width.

    With ``divider`` a thin dim vertical rule is interleaved between adjacent
    fields so they read as discrete cells rather than floating columns. It is
    a separator only -- no figure, label, value, or style changes.
    """
    grid = Table.grid(expand=True, padding=(0, 2))
    for index in range(len(columns)):
        if divider and index:
            grid.add_column(justify="center")  # thin rule between fields
        grid.add_column(justify="center", ratio=1)

    def _interleave(cells: list[Text]) -> list[Text]:
        row: list[Text] = []
        for index, cell in enumerate(cells):
            if divider and index:
                row.append(Text("│", style="dim"))
            row.append(cell)
        return row

    grid.add_row(*_interleave([Text(label, style="dim") for label, _ in columns]))
    grid.add_row(*_interleave([value for _, value in columns]))
    return grid


def _total_row(value: int) -> Table:
    """The session total as one full-inner-width row: a dim ``TOTAL TOKENS``
    label pinned left, the figure pinned right.

    Reads off the same value the panel showed before (``frame.session_total``);
    this is layout only -- the figure keeps its bold emphasis.
    """
    grid = Table.grid(expand=True)
    grid.add_column(justify="left", ratio=1)
    grid.add_column(justify="right")
    grid.add_row(
        Text("TOTAL TOKENS", style="dim"),
        Text(_num(value), style="bold"),
    )
    return grid


def _total_cost_row(cost: float, unpriced: bool) -> Table:
    """The session dollar total beneath TOTAL TOKENS, same left/right layout.

    Reads Frame.session_cost as-is -- the per-turn-summed dollars computed in
    compute_frame, never recomputed here. With ``unpriced`` a trailing
    "(+ unpriced)" marks the figure as the priceable turns only, so a session
    containing an unpriceable turn shows a marked partial total instead of a
    silent undercount.
    """
    grid = Table.grid(expand=True)
    grid.add_column(justify="left", ratio=1)
    grid.add_column(justify="right")
    figure = f"${cost:.4f}"
    if unpriced:
        figure += " (+ unpriced)"
    grid.add_row(
        Text("TOTAL COST", style="dim"),
        Text(figure, style="bold"),
    )
    return grid


def _recent_rows(recent: tuple[RecentEntry, ...]) -> Table:
    """Render recent entries, one line each: a dollar figure plus the snippet.

    The figure is the turn's own dollar cost via ``_cost_figure`` -- the SAME
    rule as the hero's COST cell, so an unpriceable turn shows "$?", never
    $0.00. Between figure and snippet sits the turn's short model tag
    (``_model_tag``: "op4.8", "fab5", ... or "?" when unknown), sized to its
    content so the snippet keeps the remaining width -- truncation behavior is
    unchanged, just a slightly narrower budget. The snippet is the typed
    prompt. Order is rendered AS
    GIVEN -- ``compute_frame`` already made ``recent`` newest-first, capped, and
    hero-excluded, so nothing is re-sorted, re-capped, or re-sliced here.

    The figure column sizes to its content and stays fully visible, in a
    purple/magenta accent so the cost stands out from the snippet; the snippet
    column takes the remaining width and truncates with an ellipsis at whatever
    inner width rich measures (``no_wrap``/``overflow``), never wrapping and never
    a hardcoded character count.
    """
    grid = Table.grid(expand=True, padding=(0, 2))
    grid.add_column(justify="right")          # figure: sized to content, always shown
    grid.add_column(justify="left")           # model tag: sized to content
    grid.add_column(justify="left", ratio=1)  # snippet: remaining width, truncates
    for entry in recent:
        grid.add_row(
            Text(_cost_figure(entry.cost), style="magenta"),
            Text(_model_tag(entry.cost.model), style=_MODEL_TAG_COLOR),
            Text(entry.text, style="dim", no_wrap=True, overflow="ellipsis"),
        )
    return grid


def render_panel(
    frame: Frame, *, flash: bool = False, width: int | None = None
) -> Panel:
    """Render one Frame to a rich Panel. Pure: Frame in, renderable out.

    No IO, no clock, no global state. The per-command delta is the visual focus
    and the only thing in the accent color; ``flash`` (decided by the loop, never
    here) briefly brightens it when a new completed delta lands. IN folds input
    and cache-creation together; CACHE READ is shown separately. COST is the
    turn's dollar figure from pricing over the delta's own components ("$?"
    when the model is unknown); it changes no token figure. The session row
    shows only the TOTAL the Frame exposes -- session IN/OUT are not on Frame and
    are deliberately not recomputed here (that would cross into accounting).

    When the Frame carries recent entries, a RECENT list renders between the hero
    and the session row -- the prompts behind the hero, newest-first, each a cost
    plus a typed-prompt snippet -- followed by a dim "+N more" line whenever
    frame.recent_omitted is non-zero. Both are read straight off the Frame; the
    renderer never recomputes the history or the overflow count.

    ``width`` bounds the panel: when given (run passes the capped target width),
    the Panel renders at exactly that width and its inner content -- including the
    snippet truncation -- measures against it. When None the panel expands to fill
    its container as before. render_panel never reads the console itself; the
    width is handed in so this stays pure.
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
                ("COST", Text(_cost_figure(delta), style=value_style)),
            ],
            divider=True,
        )

    # SESSION TOTAL: only the whole-transcript TOTAL is exposed on Frame.
    session_body = _total_row(frame.session_total)

    # hero -> divider -> [RECENT (-> "+N more") -> divider] -> SESSION TOTAL. The
    # RECENT block appears ONLY when frame.recent is non-empty; with no recent
    # entries the group is byte-identical to the v0.1 hero+total layout (no empty
    # box, no placeholder). recent is rendered exactly as compute_frame supplied
    # it. The "+N more" overflow line sits at the bottom of the RECENT section
    # (after the rows, before the divider) and reads frame.recent_omitted as-is --
    # the renderer never recomputes the count. It is omitted when the count is 0,
    # and absent entirely when there is no RECENT section.
    items: list = [last_label, last_body, Rule(style="dim")]
    if frame.recent:
        items.append(Text("RECENT", style="bold"))
        items.append(_recent_rows(frame.recent))
        if frame.recent_omitted:
            items.append(Text(f"+{frame.recent_omitted} more", style="dim"))
        items.append(Rule(style="dim"))
    items.append(Text("SESSION TOTAL", style="bold"))
    items.append(session_body)
    # TOTAL COST sits beneath TOTAL TOKENS, gated like the hero figures: the
    # waiting frame (no turns at all) keeps its figure-free layout.
    if delta is not None:
        items.append(_total_cost_row(frame.session_cost, frame.session_unpriced))
    body = Group(*items)

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
        width=width,
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


def _read_for_tick() -> ReadResult:
    """Resolve this tick's transcript by recency and read it into a ReadResult.

    find_active_transcript picks the most recently modified transcript under
    ~/.claude/projects (no configuration, no extra setup); read_transcript does
    the full re-read, parse, and no-op pass. When nothing resolves -- no projects
    dir or no transcript yet -- find_active_transcript returns None and
    read_transcript yields the empty no-op ReadResult, which DisplayState holds
    as the idle "waiting for first command" frame. Resolver and read stay
    separate so a changed path between ticks drives the existing DisplayState
    session-switch/reset semantics unchanged.
    """
    return read_transcript(find_active_transcript())


def run(interval: float = 1.0) -> int:
    """Poll loop: discover the active transcript, fold, render, sleep.

    Each tick resolves the most recently modified transcript under
    ~/.claude/projects directly (no configuration, no extra setup) and reads it.
    The Live context redraws the panel in place each tick -- no per-tick newline.
    A single tick that raises is logged and skipped so one bad read cannot kill a
    long-running process. KeyboardInterrupt exits the Live cleanly, leaves the
    terminal usable, and returns 0.
    """
    state = DisplayState()
    flash = _FlashState(interval=interval)
    console = Console()
    try:
        with Live(console=console, auto_refresh=False, screen=False) as live:
            while True:
                try:
                    frame = state.update(_read_for_tick())
                    # Impure console-width read lives here (run owns the console).
                    # Cap responsively: full width when narrow, MAX_PANEL_WIDTH
                    # when wide. render_panel stays pure -- it just gets the number.
                    target_width = min(console.width, MAX_PANEL_WIDTH)
                    live.update(
                        render_panel(
                            frame, flash=flash.observe(frame), width=target_width
                        ),
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
