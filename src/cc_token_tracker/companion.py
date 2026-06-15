"""The --buddy mascot's state brain plus its render entry point.

Two pure pieces, no IO:

- :func:`mood` -- the brain. A map from a
  :class:`cc_token_tracker.roster.RosterView` (plus ``now`` and the optional
  account ``usage``) to one of the three mood constants. This is the part that
  reacts to what tokey sees; it is unchanged by the cosmetic art work.
- :func:`render_buddy` -- turns that mood, plus the frame counter, into the
  sprite's eye state, and asks :mod:`cc_token_tracker.mascot` (which owns the
  pixels and the half-block rendering) for the lines.

The mascot is a sitting pixel-art cat parked in the footer band (the layout lives
in :mod:`cc_token_tracker.roster`). It is OPT-IN: the roster only renders it when
``--buddy`` is passed or ``TOKEY_BUDDY=1`` is set (see
:func:`cc_token_tracker.roster.buddy_requested`); default installs never see it.

Expression: the cat's eyes carry the mood and the idle blink, so the body never
redraws. STRESSED holds the eyes wide; otherwise they are open, blinking for one
tick every :data:`BLINK_EVERY` frames. The frame counter is the integer second
(``int(now)``), supplied by the render loop on its normal 1s tick -- no faster
refresh, so the animation costs nothing beyond the redraw already happening.
"""

from __future__ import annotations

import time

from rich.console import Group

from cc_token_tracker import mascot
from cc_token_tracker.liveness import ACTIVE

__all__ = [
    "MOOD_IDLE",
    "MOOD_WORKING",
    "MOOD_STRESSED",
    "BUDDY_ROWS",
    "BUDDY_WIDTH",
    "BLINK_EVERY",
    "STREAM_FRESH_SECONDS",
    "STRESS_CONTEXT_PERCENT",
    "STRESS_USAGE_PERCENT",
    "mood",
    "eye_state",
    "render_buddy",
]

# The three moods, named so callers compare against a constant, not a literal.
MOOD_IDLE = "idle"
MOOD_WORKING = "working"
MOOD_STRESSED = "stressed"

# The mascot's fixed footprint, re-exported from the sprite module so the roster
# can reserve space without reaching into the pixels.
BUDDY_ROWS = mascot.SPRITE_ROWS
BUDDY_WIDTH = mascot.SPRITE_WIDTH

# Blink for one frame every this many frames. The frame counter is the integer
# second, so this is a blink roughly every BLINK_EVERY seconds.
BLINK_EVERY = 5

# A session counts as "streaming right now" while its transcript was written
# within this many seconds. The liveness ACTIVE window (10 min) is far too coarse
# for a live working pose; a turn being written touches the file every beat, so a
# few seconds of freshness is a tight, honest "a response is landing" signal.
STREAM_FRESH_SECONDS = 5.0

# Mood flips to STRESSED at or above these. Context is the per-session window
# estimate (may exceed 100, which is still stressed); usage is any account-level
# window's utilization (only present under ``tokey cc``).
STRESS_CONTEXT_PERCENT = 90.0
STRESS_USAGE_PERCENT = 90.0


def _usage_windows(usage) -> list:
    """Every account-usage window that carries a numeric utilization.

    Pulls the subscription Session / Weekly / per-model windows plus the credits
    add-on off an :class:`cc_token_tracker.usage.AccountUsage`, skipping any that
    are absent (``None``) or, for credits, whose utilization the endpoint left
    null. ``usage`` of ``None`` (the buddy running without ``tokey cc``) yields
    an empty list, so usage never forces a mood on its own then.
    """
    if usage is None:
        return []
    out: list[float] = []
    for window in (usage.session, usage.weekly, usage.weekly_opus, usage.weekly_sonnet):
        if window is not None:
            out.append(window.utilization)
    credits = usage.credits
    if credits is not None and credits.utilization is not None:
        out.append(credits.utilization)
    return out


def mood(view, *, now: float | None = None, usage=None) -> str:
    """Map a :class:`RosterView` to a mood: idle / working / stressed.

    Pure given ``now``. The order of precedence is fixed and meaningful:

    1. STRESSED wins if ANY on-screen session's context estimate is at or above
       :data:`STRESS_CONTEXT_PERCENT`, or any account-usage window is at or above
       :data:`STRESS_USAGE_PERCENT`. A near-limit is the alert that matters most.
    2. WORKING if any on-screen session was written within
       :data:`STREAM_FRESH_SECONDS` (a turn is landing). Only ACTIVE sessions
       qualify; a closing block's stale write never reads as working.
    3. IDLE otherwise -- including an empty roster (nothing open at all).
    """
    if now is None:
        now = time.time()

    for summary in view.sessions:
        percent = summary.context_percent
        if percent is not None and percent >= STRESS_CONTEXT_PERCENT:
            return MOOD_STRESSED
    if any(util >= STRESS_USAGE_PERCENT for util in _usage_windows(usage)):
        return MOOD_STRESSED

    for summary in view.sessions:
        if summary.state == ACTIVE and (now - summary.last_write) < STREAM_FRESH_SECONDS:
            return MOOD_WORKING

    return MOOD_IDLE


def eye_state(m: str, frame: int) -> str:
    """The sprite eye state for a mood and frame: wide under stress, otherwise
    open, blinking for one frame every :data:`BLINK_EVERY`. Pure."""
    if m == MOOD_STRESSED:
        return "wide"
    if frame % BLINK_EVERY == 0:
        return "blink"
    return "open"


def render_buddy(view, *, now: float | None = None, usage=None, frame: int = 0) -> Group:
    """The mascot as a fixed ``BUDDY_ROWS``-line renderable.

    Derives the mood from ``view`` (see :func:`mood`), maps it and the frame to an
    eye state (see :func:`eye_state`), and asks :mod:`cc_token_tracker.mascot` for
    the half-block sprite lines. Pure given ``now`` and ``frame``.
    """
    m = mood(view, now=now, usage=usage)
    return Group(*mascot.sprite_lines(eye_state(m, frame)))
