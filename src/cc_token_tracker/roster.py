"""Multi-session roster view: the v0.6 all-expanded tokey screen.

One panel, one compact block per live session, newest first (7-day window).
Every block stacks the same four-part shape, so a newly-started session just
adds another block:

    ▶ my-api-server                                            active
      73% ·· ████████░░░ · ~27k left
      Last: $0.142 · IN 12.4k · OUT 3.2k · CACHE 8.1k

The ``▶`` marks the auto-followed session (the newest transcript, exactly like
the v0.3+ auto-follow); the right-hand label is the session's liveness state.
The block is summary-driven: every figure comes from the per-session
:class:`cc_token_tracker.sessions.SessionSummary`, including the ``Last:`` line
(the session's most recent completed turn). There is no live ``Frame`` in this
view and no keyboard input.

Liveness scope (v0.6.0): each block carries an active/closing/dropped label from
its transcript mtime (:mod:`cc_token_tracker.liveness`). Dropped sessions leave
the roster; the header counts the live ("active") ones only; closing sessions
stay visible but uncounted. The footer total is ACTIVE-ONLY, the same scope as
the header count.

Honesty markers carried into every block:
- LAST cost: ``$?`` when the last turn's model is unpriceable; ``no completed
  turn yet`` when the transcript has not finished a turn.
- CONTEXT: ``?`` when the limit is unknown (model absent from the limits table)
  with no bar invented; a trailing ``?`` (``104%?``) when the estimate exceeds
  the documented window. The percent is an ESTIMATE from the last prompt's
  input-side token counts; see :mod:`cc_token_tracker.context`.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from cc_token_tracker.display import _ACCENT, MAX_PANEL_WIDTH
from cc_token_tracker.liveness import ACTIVE, DROPPED, classify_with_marker
from cc_token_tracker.sessions import SessionCache, SessionSummary
from cc_token_tracker.usage import (
    AccountUsage,
    Credits,
    UsageProvider,
    UsageWindow,
    usage_enabled,
)

__all__ = [
    "ROSTER_LIMIT",
    "RosterView",
    "account_usage_requested",
    "build_roster_view",
    "percent_figure",
    "render_roster",
    "run",
    "main",
]

_LOG = logging.getLogger(__name__)

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


def _header(active_count: int, interval: float, plan: str | None = None) -> Table:
    """Top line: ``tokey`` left, ``N active session(s) · [interval]`` right.

    When ``plan`` is known (account usage is on and returned a reading) the
    subscription badge is appended: ``... · Pro Plan``. With no plan the line is
    byte-identical to before, so the default install is unchanged.
    """
    grid = Table.grid(expand=True)
    grid.add_column(justify="left", ratio=1)
    grid.add_column(justify="right")
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
    filled = round(min(percent, 100.0) / 100.0 * _BAR_WIDTH)
    bar = (
        Text("█" * filled, style=_CONTEXT_COLOR)
        + Text("█" * (_BAR_WIDTH - filled), style=_BAR_EMPTY)
    )
    remaining_k = max(0, (summary.context_limit or 0) - (summary.context_used or 0)) // 1000
    return (
        Text.assemble((percent_figure(percent), f"bold {_CONTEXT_COLOR}"), (" ·· ", "dim"))
        + bar
        + Text.assemble((" · ", "dim"), (f"~{remaining_k}k left", "dim"))
    )


def _last_line(summary: SessionSummary) -> Text:
    """A block's ``Last:`` line: the most recent completed turn's figures.

    ``$?`` when that turn's model is unpriceable; ``no completed turn yet`` when
    the transcript has finished none. ``CACHE`` is shown only when the turn read
    cache (non-zero), matching the single-session hero's cache cell otherwise
    staying silent. IN folds cache-creation into input (done in the summary).
    """
    if summary.last_output_tokens is None:
        return Text.assemble(("Last: ", "dim"), ("no completed turn yet", "dim italic"))
    cost = "$?" if summary.last_cost_usd is None else f"${summary.last_cost_usd:.3f}"
    parts: list = [
        ("Last: ", "dim"),
        (cost, ""),
        (" · ", "dim"),
        (f"IN {_k(summary.last_input_tokens or 0)}", ""),
        (" · ", "dim"),
        (f"OUT {_k(summary.last_output_tokens)}", ""),
    ]
    if (summary.last_cache_read_tokens or 0) > 0:
        parts.append((" · ", "dim"))
        parts.append((f"CACHE {_k(summary.last_cache_read_tokens)}", ""))
    return Text.assemble(*parts)


def _sum_line(summary: SessionSummary) -> Text:
    """A block's ``Sum:`` line: the session-wide totals, same shape as ``Last:``.

    The dollars are the session total (each turn priced by its own model, then
    summed); a ``+`` suffix (``$1.234+``) flags a PARTIAL total when some
    token-bearing turn was unpriceable, matching the footer's ``(+ unpriced)``.
    IN folds cache-creation into input; CACHE shows only when the session read
    cache (non-zero), exactly like ``Last:``.
    """
    cost = f"${summary.total_cost_usd:.3f}"
    if summary.unpriced:
        cost += "+"
    parts: list = [
        ("Sum: ", "dim"),
        (cost, ""),
        (" · ", "dim"),
        (f"IN {_k(summary.sum_input_tokens)}", ""),
        (" · ", "dim"),
        (f"OUT {_k(summary.sum_output_tokens)}", ""),
    ]
    if summary.sum_cache_read_tokens > 0:
        parts.append((" · ", "dim"))
        parts.append((f"CACHE {_k(summary.sum_cache_read_tokens)}", ""))
    return Text.assemble(*parts)


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
    if summary.cwd.startswith(home + os.sep):
        return "~" + summary.cwd[len(home):]
    return summary.cwd


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
        Group(_context_line(summary), _last_line(summary), _sum_line(summary)),
        (0, 0, 0, _MARKER_WIDTH),
    )
    return Group(head, body)


def _usage_bar(percent: float, color: str) -> Text:
    """A filled/empty bar for a 0..100 usage percent, clamped at a full bar.

    Both halves are solid blocks: the filled run in ``color``, the remainder in a
    dark grey, so the bar reads as a row of lit/unlit cells (the mockup look)
    rather than dots. A distinct ``color`` per row keeps stacked bars from
    merging into one block.
    """
    filled = round(min(max(percent, 0.0), 100.0) / 100.0 * _USAGE_BAR_WIDTH)
    return (
        Text("█" * filled, style=color)
        + Text("█" * (_USAGE_BAR_WIDTH - filled), style=_BAR_EMPTY)
    )


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


def _usage_row(label: str, window: UsageWindow, now: float, color: str) -> tuple:
    """One labelled usage bar row: label · bar · percent · reset.

    ``color`` is the row's fixed accent (Session yellow, Weekly blue, matching
    the mockup); the bar and percent share it. The right cell is the real reset
    time -- the subscription windows are percentages only, so no dollar figure is
    placed here.
    """
    return (
        Text(label, style="bold"),
        _usage_bar(window.utilization, color),
        Text(f"{round(window.utilization)}%", style=f"bold {color}"),
        Text(_reset_text(window.resets_at, now), style="dim"),
    )


def _credits_row(credits: Credits) -> tuple:
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
        _usage_bar(percent, _CREDITS_COLOR),
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
    rows: list[tuple] = []
    if usage.session is not None:
        rows.append(_usage_row("Session limit", usage.session, now, _SESSION_COLOR))
    if usage.weekly is not None:
        rows.append(_usage_row("Weekly limit", usage.weekly, now, _WEEKLY_COLOR))
    if usage.weekly_opus is not None:
        rows.append(_usage_row("Weekly (Opus)", usage.weekly_opus, now, _WEEKLY_COLOR))
    if usage.weekly_sonnet is not None:
        rows.append(
            _usage_row("Weekly (Sonnet)", usage.weekly_sonnet, now, _WEEKLY_COLOR)
        )
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


def _footer(active: list[SessionSummary]) -> Table:
    """The ACTIVE-ONLY total: ``active: $X.XXX · N.Nk tok`` left, with a right
    ``(+ unpriced)`` flag when ANY active session carries it (the dollar figure
    then covers the priceable turns only). Scope matches the header's active
    count exactly: closing and dropped sessions are excluded, while active
    blocks hidden by the ROSTER_LIMIT cap are still summed in. No session count
    -- the header already states how many are active."""
    total_cost = sum(s.total_cost_usd for s in active)
    total_tokens = sum(s.total_tokens for s in active)
    left = Text(f"active: ${total_cost:.3f} · {_k(total_tokens)} tok", style="bold")
    right = (
        Text("(+ unpriced)", style="yellow")
        if any(s.unpriced for s in active)
        else Text("")
    )
    grid = Table.grid(expand=True)
    grid.add_column(justify="left", ratio=1)
    grid.add_column(justify="right")
    grid.add_row(left, right)
    return grid


def render_roster(
    summaries: list[SessionSummary],
    *,
    width: int | None = None,
    now: float | None = None,
    interval: float = 1.0,
    usage: AccountUsage | None = None,
    usage_status: str | None = None,
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

    items.append(_footer([s for s in roster if s.state == ACTIVE]))

    return Panel(
        Group(*items),
        box=box.ROUNDED,
        padding=(1, 2),
        width=width,
    )


def run(interval: float = 1.0, *, account_usage: bool = False) -> int:
    """Poll loop: the all-expanded roster as the default and only view.

    Each tick re-runs discovery and re-parses the active transcript through the
    session cache (which re-parses a non-active transcript only when its
    (mtime, size) moves), then renders. A newly-started session therefore
    appears within one tick with no restart; auto-follow tracks the newest
    transcript. A tick that raises is logged and skipped; KeyboardInterrupt
    exits cleanly.

    ``account_usage`` turns on the opt-in account-level usage block (the
    ``tokey cc`` subcommand sets it). When off (the default) no credentials are
    read and no network call is made.
    """
    cache = SessionCache()
    console = Console()
    provider = UsageProvider(enabled=account_usage)
    stop = threading.Event()
    if provider.enabled:
        # Account usage is fetched off the render path: a daemon thread refreshes
        # the provider on an interval while each tick reads provider.current()
        # instantly, so a slow endpoint never stalls the panel. First fetch runs
        # at once; the block appears as soon as it lands.
        def _refresh_loop() -> None:
            provider.refresh()
            while not stop.wait(USAGE_REFRESH_SECONDS):
                provider.refresh()

        threading.Thread(
            target=_refresh_loop, name="tokey-usage", daemon=True
        ).start()
    try:
        with Live(console=console, auto_refresh=False, screen=False) as live:
            while True:
                try:
                    summaries = cache.summaries()
                    target_width = min(console.width, MAX_PANEL_WIDTH)
                    live.update(
                        render_roster(
                            summaries,
                            width=target_width,
                            interval=interval,
                            usage=provider.current(),
                            usage_status=provider.status_message(),
                        ),
                        refresh=True,
                    )
                except Exception:  # noqa: BLE001 - one bad tick must not kill us
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


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point: the roster, with ``cc`` enabling account usage.

    ``tokey`` runs the plain session roster; ``tokey cc`` adds the account-level
    usage block. argv defaults to the process args; it is a parameter so tests
    can drive it.
    """
    if argv is None:
        argv = sys.argv[1:]
    return run(account_usage=account_usage_requested(argv))


if __name__ == "__main__":
    sys.exit(main())
