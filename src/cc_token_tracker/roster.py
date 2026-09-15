"""Multi-session roster view: tokey's one and only screen.

One panel, one compact block per live session, newest first (7-day window).
Every block stacks the same shape, so a newly-started session just adds another
block:

    ▶ my-api-server                                            active
      73% ·· ████████░░░ · ~27k left                        opus-4-8
      Last Prompt: $0.142 · IN 12.4k · OUT 3.2k · CACHE 8.1k
      Total: $4.021 · IN 240.6k · OUT 61.0k · CACHE 1980.4k

The ``▶`` marks the auto-followed session (the newest transcript); the
right-hand label is the session's liveness state. The block is summary-driven:
every figure comes from the per-session
:class:`cc_token_tracker.sessions.SessionSummary`, so nothing here recomputes a
token or a dollar. There is no keyboard input.

Liveness scope: each block carries an active/closing/dropped label
(:mod:`cc_token_tracker.liveness`). Dropped sessions leave the roster; the
header counts the live ("active") ones only; closing sessions stay visible but
uncounted. The footer total is ACTIVE-ONLY, the same scope as the header count.

Honesty markers carried into every block:
- LAST cost: ``$?`` when the last turn's model is unpriceable; ``no completed
  turn yet`` when the transcript has not finished a turn.
- CONTEXT: ``?`` when the limit is unknown (model absent from the limits table)
  with no bar invented; a trailing ``?`` (``104%?``) when the estimate exceeds
  the documented window. The percent is an ESTIMATE from the last prompt's
  input-side token counts; see :mod:`cc_token_tracker.context`. The short model
  label on the right of this row (``opus-4-8``) is the model the window belongs
  to -- the same record the estimate is drawn from; absent when none is known.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime

from rich import box
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from cc_token_tracker import __version__, history
from cc_token_tracker import mood as _mood
from cc_token_tracker.liveness import ACTIVE, DROPPED, classify_with_marker
from cc_token_tracker.pricing import normalize_model
from cc_token_tracker.sessions import SessionCache, SessionSummary
from cc_token_tracker.usage import (
    AccountUsage,
    Credits,
    UsageProvider,
    UsageWindow,
    usage_enabled,
)

__all__ = [
    "MAX_PANEL_WIDTH",
    "ROSTER_LIMIT",
    "RosterView",
    "account_usage_requested",
    "build_roster_view",
    "main",
    "mood_enabled",
    "percent_figure",
    "render_roster",
    "run",
    "version_requested",
]

_LOG = logging.getLogger(__name__)

# The one accent colour: the ▶ auto-follow marker and the "tokey" title. Every
# other colour in the panel is a gauge tint, so this reads as the app's own.
_ACCENT = "cyan"

# Upper bound on the rendered panel width. On a narrow terminal the panel uses
# the full width; on a wide one it caps here instead of stretching edge to edge.
# One knob. The impure console-width read lives in run, not in the renderers.
MAX_PANEL_WIDTH = 100

# At most this many session blocks render; overflow becomes a "+N more" line
# above the footer. The footer total still covers every active session.
ROSTER_LIMIT = 10

# Width of a block's context bar, in cells.
_BAR_WIDTH = 24

# Left indent (cells) for a block's body, so the context/Last lines line up
# under the project name rather than under the ▶ marker column.
_MARKER_WIDTH = 2

# Context gauge colour (distinct from the cyan ▶/title accent).
_CONTEXT_COLOR = "yellow"

# Account-usage bar width, wider than the per-session context bar so the
# account block reads as a distinct panel-spanning summary above the sessions.
_USAGE_BAR_WIDTH = 28

# Account-usage bar colours, fixed per row to match the mockup: Session yellow,
# Weekly blue, the credits add-on green. Distinct per-row colours also stop
# adjacent bars from merging into one block.
_SESSION_COLOR = "yellow"
_WEEKLY_COLOR = "blue"
_CREDITS_COLOR = "green"

# Unfilled bar cells (shared by the account and context bars): a dark grey solid
# block, so every bar reads as a row of lit/unlit cells rather than faint dots.
_BAR_EMPTY = "grey30"

# How often the background driver re-fetches account usage. Deliberately slow:
# the endpoint rate-limits aggressively (the web Usage panel refreshes manually),
# and the windows are 5-hour and 7-day, so they barely move minute to minute.
# Five minutes keeps us well under the limit while staying current enough.
USAGE_REFRESH_SECONDS = 300.0


@dataclass(frozen=True)
class RosterView:
    """One render pass's presentation scope over the session summaries.

    ``sessions`` is the on-screen roster: every summary whose liveness is not
    "dropped" (so active + closing), newest first, each carrying its freshly
    computed ``state``. ``active_count`` counts the "active" ones ONLY --
    closing sessions stay on screen as blocks but are never counted. Dropped
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

    Each summary is re-stamped with ``state = classify_with_marker(now,
    last_write, marker_event, marker_ts)`` (the field is presentation-only; see
    :class:`cc_token_tracker.sessions.SessionSummary`): a closed session drops at
    once, an open one stays active, and a session with no marker falls back to
    the transcript-mtime classification, unchanged. The roster keeps the
    non-dropped ones in the given order; the active count is the number of
    "active" survivors. Pure given ``now``: no IO, no re-parsing, no touching of
    the frozen cost outputs.
    """
    staged = [
        replace(
            summary,
            state=classify_with_marker(
                now,
                summary.last_write,
                summary.marker_event,
                summary.marker_ts,
            ),
        )
        for summary in summaries
    ]
    sessions = [summary for summary in staged if summary.state != DROPPED]
    active_count = sum(1 for summary in sessions if summary.state == ACTIVE)
    return RosterView(sessions=sessions, active_count=active_count)


def percent_figure(percent: float | None) -> str:
    """The context percent: ``NN%``, ``NNN%?`` past 100, ``?`` when unknown.

    An unknown limit yields ``?`` (the limits table never guesses). A percent
    above 100 keeps its number but gains a trailing ``?`` -- the estimate
    overflowed the documented window, and the marker says so instead of
    clamping to a clean-looking 100%.
    """
    if percent is None:
        return "?"
    figure = f"{round(percent)}%"
    return figure + "?" if percent > 100 else figure


def _k(tokens: int) -> str:
    """Token count in compact thousands: ``12.4k``, ``0.8k``, ``67.2k``."""
    return f"{tokens / 1000:.1f}k"


def _left_right_grid() -> Table:
    """A full-width two-column grid: left cell takes the slack, right cell hugs.

    The panel's recurring layout (header, context row, footer totals), in one
    place so every row shares the same edges and a change lands everywhere.
    """
    grid = Table.grid(expand=True)
    grid.add_column(justify="left", ratio=1)
    grid.add_column(justify="right")
    return grid


def _bar(percent: float, width: int, color: str) -> Text:
    """A filled/empty gauge bar for a 0..100 percent, clamped at both ends.

    Both halves are solid blocks: the filled run in ``color``, the remainder in
    a dark grey, so a bar reads as a row of lit/unlit cells rather than dots.
    Shared by the per-session context gauge and the account-usage rows, which
    differ only in width and colour.
    """
    filled = round(min(max(percent, 0.0), 100.0) / 100.0 * width)
    return Text("█" * filled, style=color) + Text(
        "█" * (width - filled), style=_BAR_EMPTY
    )


def _figures_line(
    label: str,
    cost: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
) -> Text:
    """One ``label: $cost · IN x · OUT y · CACHE z`` line.

    Shared by the block's ``Last Prompt:`` and ``Total:`` rows, which differ only
    in their label, how the dollar figure is formatted, and which totals they
    read. CACHE is omitted entirely when the cache-read count is zero, so a turn
    or session that read no cache stays silent rather than showing a bare
    ``0.0k``.
    """
    parts: list = [
        (f"{label}: ", "dim"),
        (cost, ""),
        (" · ", "dim"),
        (f"IN {_k(input_tokens)}", ""),
        (" · ", "dim"),
        (f"OUT {_k(output_tokens)}", ""),
    ]
    if cache_read_tokens > 0:
        parts.append((" · ", "dim"))
        parts.append((f"CACHE {_k(cache_read_tokens)}", ""))
    return Text.assemble(*parts)


def _header(active_count: int, interval: float, plan: str | None = None) -> Table:
    """Top line: ``tokey`` left, ``N active session(s) · [interval]`` right.

    When ``plan`` is known (account usage is on and returned a reading) the
    subscription badge is appended: ``... · Pro Plan``. With no plan the line is
    byte-identical to before, so the default install is unchanged.
    """
    grid = _left_right_grid()
    plural = "" if active_count == 1 else "s"
    parts: list = [
        (f"{active_count} active session{plural}", "dim"),
        (" · ", "dim"),
        (f"[{interval:.1f}s]", "dim"),
    ]
    if plan:
        parts.append((" · ", "dim"))
        parts.append((f"{plan.title()} Plan", "dim"))
    grid.add_row(Text("tokey", style=f"bold {_ACCENT}"), Text.assemble(*parts))
    return grid


def _context_line(summary: SessionSummary) -> Text:
    """A block's one-line context gauge: ``73% ·· ████░░ · ~27k left``.

    An unknown limit renders an honest ``context limit unknown for this model``
    with no bar invented. An over-100 estimate fills the bar and shows
    ``~0k left`` beside the ``NNN%?`` marker.
    """
    percent = summary.context_percent
    if percent is None:
        return Text("context limit unknown for this model", style="dim")
    remaining = (summary.context_limit or 0) - (summary.context_used or 0)
    remaining_k = max(0, remaining) // 1000
    return (
        Text.assemble(
            (percent_figure(percent), f"bold {_CONTEXT_COLOR}"), (" ·· ", "dim")
        )
        + _bar(percent, _BAR_WIDTH, _CONTEXT_COLOR)
        + Text.assemble((" · ", "dim"), (f"~{remaining_k}k left", "dim"))
    )


def _context_model_label(model: str | None) -> str:
    """Short model label for the context row: ``claude-opus-4-8`` -> ``opus-4-8``.

    Drops the trailing date suffix (via pricing's :func:`normalize_model`) and
    the leading ``claude-`` family prefix so the gauge stays compact. Returns
    ``""`` when no model is known, so the caller omits the label rather than
    rendering a blank cell."""
    if not model:
        return ""
    return normalize_model(model).removeprefix("claude-")


def _context_row(summary: SessionSummary) -> RenderableType:
    """The context gauge plus, right-aligned under the header's liveness label,
    the model the window belongs to.

    A two-column grid: the gauge (or the honest unknown-limit message) on the
    left, the short model label on the right. The grid expands to the block
    body's width, so the model's right edge lines up under ``active``. When no
    model is known the row is just the bare gauge, no empty right cell."""
    gauge = _context_line(summary)
    label = _context_model_label(summary.context_model)
    if not label:
        return gauge
    grid = _left_right_grid()
    grid.columns[0].overflow = "ellipsis"
    grid.columns[0].no_wrap = True
    grid.columns[1].no_wrap = True
    grid.add_row(gauge, Text(label, style="dim"))
    return grid


def _last_line(summary: SessionSummary) -> Text:
    """A block's ``Last Prompt:`` line: the most recent completed turn's figures.

    ``$?`` when that turn's model is unpriceable; ``no completed turn yet`` when
    the transcript has finished none. ``CACHE`` is shown only when the turn read
    cache (non-zero). IN folds cache-creation into input (done in the summary).
    """
    if summary.last_output_tokens is None:
        return Text.assemble(
            ("Last Prompt: ", "dim"), ("no completed turn yet", "dim italic")
        )
    cost = "$?" if summary.last_cost_usd is None else f"${summary.last_cost_usd:.3f}"
    return _figures_line(
        "Last Prompt",
        cost,
        input_tokens=summary.last_input_tokens or 0,
        output_tokens=summary.last_output_tokens,
        cache_read_tokens=summary.last_cache_read_tokens or 0,
    )


def _sum_line(summary: SessionSummary) -> Text:
    """A block's ``Total:`` line: the session-wide totals, same shape as
    ``Last Prompt:``.

    The dollars are the session total (each turn priced by its own model, then
    summed); a ``+`` suffix (``$1.234+``) flags a PARTIAL total when some
    token-bearing turn was unpriceable, matching the footer's ``(+ unpriced)``.
    """
    cost = f"${summary.total_cost_usd:.3f}"
    if summary.unpriced:
        cost += "+"
    return _figures_line(
        "Total",
        cost,
        input_tokens=summary.sum_input_tokens,
        output_tokens=summary.sum_output_tokens,
        cache_read_tokens=summary.sum_cache_read_tokens,
    )


def _project_title(summary: SessionSummary) -> str:
    """The session's display title: the real cwd as a ``~``-relative path.

    Falls back to the on-disk ``project`` dir name when no cwd was captured (an
    older transcript, or a not-yet-written session with no marker cwd). Using the
    real cwd avoids the lossy dash-encoding of the project directory name (where
    path separators, spaces, and real dashes all collapse to ``-``).
    """
    if not summary.cwd:
        return summary.project
    home = os.path.expanduser("~")
    if summary.cwd == home:
        return "~"
    cwd = summary.cwd
    if cwd.startswith(home + os.sep):
        cwd = "~" + cwd[len(home):]
    # Render with forward slashes so the title reads the same on every platform.
    # On Windows the captured cwd uses backslashes, which would otherwise show as
    # ``~\Desktop\tokey``; normalize to ``~/Desktop/tokey``.
    return cwd.replace(os.sep, "/")


def _session_block(summary: SessionSummary) -> Group:
    """One session's compact block: a header line (marker, project, liveness
    label) over the indented context and Last lines. The ``▶`` marks the
    auto-followed session; the right label is the liveness state."""
    is_live = summary.state == ACTIVE
    label = (
        Text("active", style="bold green")
        if is_live
        else Text("closing", style="dim")
    )
    head = Table.grid(expand=True, padding=0)
    head.add_column(width=_MARKER_WIDTH)
    head.add_column(justify="left", ratio=1, no_wrap=True, overflow="ellipsis")
    head.add_column(justify="right")
    head.add_row(
        Text("▶", style=_ACCENT) if summary.is_active else Text(""),
        Text(_project_title(summary), style="bold" if is_live else "dim"),
        label,
    )
    body = Padding(
        Group(_context_row(summary), _last_line(summary), _sum_line(summary)),
        (0, 0, 0, _MARKER_WIDTH),
    )
    return Group(head, body)


def _reset_text(resets_at: float | None, now: float) -> str:
    """A window's reset time, phrased like the Claude Usage panel.

    Under a day out it counts down (``resets in 4h 52m``); a day or more out it
    names the local weekday and time (``resets Fri 06:00``). None, or a time
    already passed, yields ``""`` so the row simply omits the reset rather than
    showing a stale or negative value.
    """
    if resets_at is None:
        return ""
    delta = resets_at - now
    if delta <= 0:
        return ""
    if delta < 86400:
        hours = int(delta // 3600)
        minutes = int((delta % 3600) // 60)
        return f"resets in {hours}h {minutes:02d}m"
    return "resets " + datetime.fromtimestamp(resets_at).strftime("%a %H:%M")


def _usage_row(
    label: str, window: UsageWindow, now: float, color: str
) -> tuple[Text, Text, Text, Text]:
    """One labelled usage bar row: label · bar · percent · reset.

    ``color`` is the row's fixed accent (Session yellow, Weekly blue); the bar
    and percent share it. The right cell is the real reset time -- the
    subscription windows are percentages only, so no dollar figure is placed
    here.
    """
    return (
        Text(label, style="bold"),
        _bar(window.utilization, _USAGE_BAR_WIDTH, color),
        Text(f"{round(window.utilization)}%", style=f"bold {color}"),
        Text(_reset_text(window.resets_at, now), style="dim"),
    )


def _credits_row(credits: Credits) -> tuple[Text, Text, Text, Text]:
    """The usage-credits row: the one place real dollars belong.

    The percent is the reported utilization, or used/limit when the endpoint
    leaves utilization null. The right cell shows the actual spend
    (``$1.20 / $10.00``) since credits, unlike the subscription windows, ARE
    denominated in currency.
    """
    used = credits.used or 0.0
    limit = credits.limit
    if credits.utilization is not None:
        percent = credits.utilization
    elif limit:
        percent = used / limit * 100.0
    else:
        percent = 0.0
    currency = (credits.currency or "USD").upper()
    sym = "$" if currency == "USD" else ""
    suffix = "" if sym else f" {currency}"
    amount = (
        f"({sym}{used:.2f} / {sym}{limit:.2f}{suffix})"
        if limit is not None
        else f"({sym}{used:.2f}{suffix})"
    )
    return (
        Text("Usage credits", style="bold"),
        _bar(percent, _USAGE_BAR_WIDTH, _CREDITS_COLOR),
        Text(f"{round(percent)}%", style=f"bold {_CREDITS_COLOR}"),
        Text(amount, style="dim"),
    )


def _account_block(usage: AccountUsage, now: float) -> Group | None:
    """The account-level usage block, or None when there is nothing to show.

    Renders only the windows the endpoint actually returned: Session and Weekly
    always (on Pro), the per-model weekly rows when a higher plan populates them,
    and the credits row only when the add-on is enabled. No window is invented
    and no dollar figure is attached to the subscription rows (those windows are
    percentages only). Returns None when no row qualifies, so the caller can omit
    the block and its divider entirely.
    """
    windows = (
        ("Session limit", usage.session, _SESSION_COLOR),
        ("Weekly limit", usage.weekly, _WEEKLY_COLOR),
        ("Weekly (Opus)", usage.weekly_opus, _WEEKLY_COLOR),
        ("Weekly (Sonnet)", usage.weekly_sonnet, _WEEKLY_COLOR),
    )
    rows: list[tuple[Text, Text, Text, Text]] = [
        _usage_row(label, window, now, color)
        for label, window, color in windows
        if window is not None
    ]
    if usage.credits is not None and usage.credits.enabled:
        rows.append(_credits_row(usage.credits))
    if not rows:
        return None
    grid = Table.grid(expand=True, padding=(0, 2))
    grid.add_column(justify="left", no_wrap=True)  # label
    grid.add_column(justify="left")                # bar
    grid.add_column(justify="right")               # percent
    grid.add_column(justify="left", ratio=1)       # reset / amount
    for row in rows:
        grid.add_row(*row)
    return Group(Text("Account-level Claude usage", style="dim"), grid)


def _footer(
    active: list[SessionSummary],
    *,
    now: float | None = None,
    width: int | None = None,
    mood: bool = True,
) -> RenderableType:
    """The ACTIVE-ONLY total: ``active: $X.XXX · N.Nk tok``, with an inline
    ``(+ unpriced)`` flag when ANY active session carries it (the dollar figure
    then covers the priceable turns only). Scope matches the header's active
    count exactly: closing and dropped sessions are excluded, while active
    blocks hidden by the ROSTER_LIMIT cap are still summed in. No session count
    -- the header already states how many are active.

    With ``mood`` on (the default) a white speech bubble and the light-blue
    Baymax head beneath it are parked on the right, above the totals line (see
    :func:`cc_token_tracker.mood.render_mood`, which owns that stack);
    ``now``/``width`` drive the lockstep rotation and the bubble fit. With
    ``mood`` off the footer is exactly the plain totals line."""
    total_cost = sum(summary.total_cost_usd for summary in active)
    total_tokens = sum(summary.total_tokens for summary in active)
    left = Text(f"active: ${total_cost:.3f} · {_k(total_tokens)} tok", style="bold")
    if any(summary.unpriced for summary in active):
        left.append("  (+ unpriced)", style="yellow")

    totals = _left_right_grid()
    totals.add_row(left, Text(""))
    if not mood:
        return totals
    if now is None:
        now = time.time()
    # Bubble parked top-right, the big face right beneath its tail, the total
    # on its own line bottom-left.
    panel_width = width if width is not None else MAX_PANEL_WIDTH
    return Group(_mood.render_mood(active, now, panel_width), totals)


def render_roster(
    summaries: list[SessionSummary],
    *,
    width: int | None = None,
    now: float | None = None,
    interval: float = 1.0,
    usage: AccountUsage | None = None,
    usage_status: str | None = None,
    mood: bool = True,
) -> Panel:
    """Render the all-expanded roster to a rich Panel. Pure given ``now``; no IO.

    ``summaries`` is the session-cache output, newest first, the auto-followed
    entry flagged ``is_active``. Liveness scope is applied here (see
    :func:`build_roster_view`): dropped sessions leave the roster, the header
    counts only the live ("active") ones, and closing sessions stay visible but
    uncounted. Every surviving session renders as a compact block; blocks beyond
    ROSTER_LIMIT collapse into a "+N more" line above the footer. The footer
    total is ACTIVE-ONLY (the same scope as the header count): closing and
    dropped sessions are excluded, while active blocks hidden by the cap are
    still summed. ``now`` drives the liveness scope (defaults to the current
    time; tests pin it); ``interval`` is shown in the header refresh tag.

    ``usage`` is the optional account-level reading (the opt-in subscription
    feature). When present it adds the plan badge to the header and an
    account-usage block above the session blocks; when None (the default) the
    panel is exactly the session-only roster. The block is omitted even when
    ``usage`` is given but carries no renderable window.
    """
    if now is None:
        now = time.time()

    view = build_roster_view(summaries, now=now)
    roster = view.sessions

    plan = usage.plan if usage is not None else None
    items: list = [_header(view.active_count, interval, plan), Rule()]
    block = _account_block(usage, now) if usage is not None else None
    if block is not None:
        items.append(block)
        items.append(Rule(style="dim"))
    elif usage_status:
        # Enabled but no reading to show yet: say so instead of a blank gap.
        items.append(Text(usage_status, style="dim italic"))
        items.append(Rule(style="dim"))
    if roster:
        shown = roster[:ROSTER_LIMIT]
        for summary in shown:
            items.append(_session_block(summary))
            items.append(Rule(style="dim"))
        omitted = len(roster) - len(shown)
        if omitted > 0:
            items.append(Text(f"+{omitted} more", style="dim"))
            items.append(Rule(style="dim"))
    else:
        items.append(Text("no sessions in the last 7 days", style="dim italic"))
        items.append(Rule(style="dim"))

    active_sessions = [summary for summary in roster if summary.state == ACTIVE]
    items.append(_footer(active_sessions, now=now, width=width, mood=mood))

    return Panel(
        Group(*items),
        box=box.ROUNDED,
        padding=(1, 2),
        width=width,
    )


def _start_usage_refresher(provider: UsageProvider) -> threading.Event:
    """Drive ``provider`` from a daemon thread; return its stop signal.

    Account usage is fetched OFF the render path: this thread refreshes on an
    interval while each tick reads ``provider.current()`` instantly, so a slow or
    hung endpoint never stalls the panel. The first fetch runs immediately, so
    the block appears as soon as it lands. A disabled provider starts no thread
    at all, which is what makes the default install cost nothing: no credential
    read, no network call. Setting the returned event ends the thread.
    """
    stop = threading.Event()
    if not provider.enabled:
        return stop

    def refresh_loop() -> None:
        provider.refresh()
        while not stop.wait(USAGE_REFRESH_SECONDS):
            provider.refresh()

    threading.Thread(target=refresh_loop, name="tokey-usage", daemon=True).start()
    return stop


def _scaled_tokens(tokens: int) -> str:
    """Token count at history scale: ``13.9M``, ``482.1k``, ``900``.

    The blocks' :func:`_k` is built for one turn or one session and renders a
    lifetime figure as ``13890.0k``, which the eye cannot read as 13.9 million.
    History sums run orders of magnitude larger, so the unit scales with the
    number instead of being fixed.
    """
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.1f}k"
    return str(tokens)


def _money(cost: float, unpriced: bool) -> str:
    """A dollar figure, with the roster's partial-total marker.

    ``+`` means the span contained a turn whose model was not in the pricing
    table, so the figure covers the priceable turns only -- the same contract
    the live blocks keep. Never render a flagged total as if it were complete.
    """
    return f"${cost:,.2f}{'+' if unpriced else ''}"


def render_log(*, db_path: str | None = None, days: int = 30) -> RenderableType:
    """The ``tokey log`` screen: spend history the live roster cannot remember.

    Two tables over :mod:`cc_token_tracker.history` -- recent days, then
    all-time per project. This is a one-shot render, not a Live loop: history
    only changes when a session ends.
    """
    from cc_token_tracker.history import daily_totals, project_totals

    by_day = daily_totals(limit=days, db_path=db_path)
    by_project = project_totals(db_path=db_path)

    if not by_day:
        return Padding(
            Text(
                "no history yet -- sessions are recorded when they end\n"
                "(run a session to completion, or start tokey to backfill)",
                style="dim",
            ),
            (1, 2),
        )

    day_table = Table(box=box.SIMPLE, expand=False, pad_edge=False)
    day_table.add_column("day", style="cyan")
    day_table.add_column("sessions", justify="right", style="dim")
    day_table.add_column("tokens", justify="right")
    day_table.add_column("cost", justify="right", style="bold")
    for row in by_day:
        day_table.add_row(
            row.day, str(row.sessions), _scaled_tokens(row.total_tokens),
            _money(row.cost_usd, row.unpriced),
        )

    project_table = Table(box=box.SIMPLE, expand=False, pad_edge=False)
    project_table.add_column("project", style="cyan")
    project_table.add_column("sessions", justify="right", style="dim")
    project_table.add_column("tokens", justify="right")
    project_table.add_column("cost", justify="right", style="bold")
    for row in by_project:
        project_table.add_row(
            row.label, str(row.sessions), _scaled_tokens(row.total_tokens),
            _money(row.cost_usd, row.unpriced),
        )

    span_cost = sum(row.cost_usd for row in by_day)
    span_unpriced = any(row.unpriced for row in by_day)
    footer = Text.assemble(
        (f"{len(by_day)} day(s) recorded", "dim"),
        ("  ·  ", "dim"),
        (_money(span_cost, span_unpriced), "bold"),
    )

    return Group(
        Padding(Text("tokey log", style="bold"), (1, 0, 0, 2)),
        Padding(day_table, (0, 2)),
        Padding(Text("by project (all time)", style="dim"), (0, 0, 0, 2)),
        Padding(project_table, (0, 2)),
        Padding(footer, (0, 2, 1, 2)),
    )


def log_requested(argv: list[str]) -> bool:
    """``tokey log`` prints the spend history and exits instead of looping."""
    return "log" in argv


def run(
    interval: float = 1.0,
    *,
    account_usage: bool = False,
    mood: bool = True,
) -> int:
    """Poll loop: the all-expanded roster as the default and only view.

    Each tick re-runs discovery and re-parses the active transcript through the
    session cache (which re-parses a non-active transcript only when its
    (mtime, size) moves), then renders. A newly-started session therefore
    appears within one tick with no restart; auto-follow tracks the newest
    transcript. A tick that raises is logged and skipped; KeyboardInterrupt
    exits cleanly.

    ``account_usage`` turns on the opt-in account-level usage block (the
    ``tokey cc`` subcommand sets it). When off (the default) no credentials are
    read and no network call is made. ``mood`` shows the live footer face and
    speech bubble (``--no-mood`` turns it off, leaving the plain totals line).
    """
    cache = SessionCache()
    console = Console()
    provider = UsageProvider(enabled=account_usage)
    stop = _start_usage_refresher(provider)

    # Backfill once at startup, not per tick. SessionEnd is the primary history
    # writer, but it never fires for a session killed by a crash, `kill -9`, or
    # a closed terminal -- exactly the sessions worth not losing. The UPSERT on
    # session_id makes this idempotent against the hook, and makes a row written
    # here for a still-running session self-correcting on a later pass.
    backfilled_once = False

    try:
        with Live(console=console, auto_refresh=False, screen=False) as live:
            while True:
                try:
                    summaries = cache.summaries()
                    if not backfilled_once:
                        backfilled_once = True
                        history.backfill(summaries)
                    now = time.time()
                    current_usage = provider.current()
                    target_width = min(console.width, MAX_PANEL_WIDTH)
                    live.update(
                        render_roster(
                            summaries,
                            width=target_width,
                            now=now,
                            interval=interval,
                            usage=current_usage,
                            usage_status=provider.status_message(),
                            mood=mood,
                        ),
                        refresh=True,
                    )
                except Exception:  # deliberately bare: one bad tick must not kill us
                    _LOG.exception("roster tick failed; continuing")
                time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
    return 0


def account_usage_requested(
    argv: list[str], env: dict[str, str] | None = None
) -> bool:
    """Whether to enable the account-usage block for this launch.

    On when the ``cc`` subcommand is given (``tokey cc``) or the
    ``TOKEY_ACCOUNT_USAGE`` env var is set (the env var stays supported for
    scripts and cron). Pure; the argv/env parsing is split out from ``main`` so
    it can be tested without entering the render loop.
    """
    return "cc" in argv or usage_enabled(env)


def mood_enabled(argv: list[str]) -> bool:
    """Whether the footer mood face + speech bubble are shown. On by default;
    ``--no-mood`` turns it off. Split out so it is testable without the loop."""
    return "--no-mood" not in argv


def version_requested(argv: list[str]) -> bool:
    """Whether the launch is just a version query (``--version``/``-V``).

    Split out so it is testable without entering the render loop; the caller
    prints :data:`cc_token_tracker.__version__` and exits before any IO."""
    return "--version" in argv or "-V" in argv


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point: the roster, with ``cc`` enabling account usage.

    ``tokey`` runs the plain session roster; ``tokey cc`` adds the account-level
    usage block; ``--no-mood`` hides the footer mood face and speech bubble;
    ``--version``/``-V`` prints the version and exits without entering the loop.
    argv defaults to the process args; it is a parameter so tests can drive it.
    """
    if argv is None:
        argv = sys.argv[1:]
    if version_requested(argv):
        print(f"tokey {__version__}")
        return 0
    if log_requested(argv):
        Console().print(render_log())
        return 0
    # The panel draws Unicode bars/arrows/box characters. When stdout is not
    # UTF-8 these crash with UnicodeEncodeError: on Windows Python defaults to the
    # locale codepage (cp1252), and on macOS/Linux a bare C/POSIX locale (cron, a
    # GUI-spawned shell, LANG unset) yields an ASCII stdout. Force UTF-8 whenever
    # the current encoding is not already UTF-8 so tokey renders the same
    # everywhere; the guard covers stdout streams that do not support reconfigure
    # (e.g. a test harness replacing it with a buffer).
    encoding = (getattr(sys.stdout, "encoding", None) or "").lower().replace("-", "")
    if encoding != "utf8":
        with contextlib.suppress(AttributeError, ValueError):
            sys.stdout.reconfigure(encoding="utf-8")
    return run(
        account_usage=account_usage_requested(argv),
        mood=mood_enabled(argv),
    )


if __name__ == "__main__":
    sys.exit(main())
