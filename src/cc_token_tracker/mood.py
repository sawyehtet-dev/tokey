"""Footer mood: a big multi-row face paired with a speech bubble, parked right.

A single curated pool of ``(mood, phrase)`` pairs rotates in lockstep: every
``ROTATE_SECONDS`` the mood and its line change together, so they never drift
out of sync. The phrase sits in a white speech bubble and a soft light-blue
Baymax head (a rounded body that emotes only through its two eyes and the thin
line joining them) is parked beneath it at the right edge of the footer. A braille
spinner trails the face only while a prompt is actively streaming (read from
transcript write-recency), so there is still a live signal without any new
parsing or threads.

Everything here is pure given ``now``, so the render path stays testable exactly
like the rest of the roster. Content is hand-curated; ``tests/test_mood.py``
guards the pool against offensive entries and em dashes (a standing project
rule).
"""

from __future__ import annotations

import textwrap
import time

from rich.align import Align
from rich.cells import cell_len
from rich.console import Group
from rich.text import Text

from cc_token_tracker.liveness import ACTIVE
from cc_token_tracker.sessions import SessionSummary

# Baymax wears a soft light blue so he has some life against the white bubble
# (pure white on white read as flat). CLAUDE_CORAL is kept for reference.
CLAUDE_CORAL = "#D97757"
BAYMAX_BLUE = "#8ECAE6"
FACE_COLOR = BAYMAX_BLUE
BUBBLE_COLOR = "white"

# A session counts as "working" while its transcript was appended within this
# many seconds. A streaming turn writes continuously; an idle one goes quiet.
ACTIVE_WRITE_WINDOW = 8.0

# Mood and phrase rotate together on this single cadence (seconds).
ROTATE_SECONDS = 8.0

# The braille spinner trailing the face while a prompt streams.
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# Inner width of Baymax's face line (cells between the soft sides).
_BAYMAX_INNER = 9
_LINE = "─" * 7  # the thin line joining Baymax's two eyes


def _baymax(faceline: str) -> str:
    """Build a soft three-row Baymax head: a ``.``-cornered top, the
    eyes-and-line face centered to a constant width so every mood aligns, and a
    backtick-cornered bottom. The ``( )`` sides bulge a cell past the corners for
    the inflated look."""
    pad = _BAYMAX_INNER - cell_len(faceline)
    left = pad // 2
    centered = " " * left + faceline + " " * (pad - left)
    top = " ." + "─" * _BAYMAX_INNER + ". "
    bottom = " `" + "─" * _BAYMAX_INNER + "` "
    return "\n".join((top, "( " + centered + " )", bottom))


# Baymax in coral: every mood is the same soft head, emoting only through the
# eyes, exactly like the real thing. Each phrase below maps to one of these.
FACES: dict[str, str] = {
    "happy": _baymax("◠" + _LINE + "◠"),
    "cool": _baymax("■" + _LINE + "■"),
    "sleepy": _baymax("˘" + _LINE + "˘"),
    "surprised": _baymax("◉" + _LINE + "◉"),
    "love": _baymax("♥" + _LINE + "♥"),
    "sad": _baymax("˙" + _LINE + "˙"),
    "cry": _baymax("╥" + _LINE + "╥"),
    "wink": _baymax("◠" + _LINE + "˘"),
    "shy": _baymax("´" + _LINE + "`"),
    "smug": _baymax("¬" + _LINE + "¬"),
    "think": _baymax("◔" + _LINE + "◔"),
    "music": _baymax("♪" + _LINE + "♪"),
    "dead": _baymax("✕" + _LINE + "✕"),
    "neutral": _baymax("●" + _LINE + "●"),
    "peek": _baymax("◕" + _LINE + "◕"),
    "proud": _baymax("★" + _LINE + "★"),
}

# Each entry is (mood, phrase). The mood picks the face; the pair always rotates
# as a unit, so the face and the bubble change in step.
MOODS: tuple[tuple[str, str], ...] = (
    ("cool", "you will never know how you sound to other people"),
    ("think", "the people who raised you were making it up too"),
    ("shy", "the door you hold open makes them walk faster, and it embarrasses you both"),
    ("happy", "kindness is mostly timing"),
    ("smug", "everyone in a crowd thinks they're the exception"),
    ("sad", "you keep apologizing for the wrong things, the real ones you defend"),
    ("smug", "a gift is partly an instruction"),
    ("surprised", "you only notice the fridge hum when it stops"),
    ("proud", "nobody is waiting for you to start, they're waiting for you to make it safe to"),
    ("think", "you can outgrow an answer and keep the question"),
    ("love", "the best parts of most days don't survive being told"),
    ("proud", "you became fluent and forgot it was ever hard"),
    ("smug", "you are someone's example of how not to, and you'll never know which thing"),
    ("neutral", "we keep secrets mostly from the people they'd help"),
    ("happy", "you can't choose what you find funny, which is the most honest thing about you"),
    ("music", "the version of a song you love is whichever one you heard first"),
    ("sad", "you've been the stranger in someone else's story about a bad day"),
    ("surprised", "you can't give someone a view, only a window, they do the looking"),
    ("surprised", "the second time is the first time you actually see it"),
    ("love", "you'll forgive yourself for the thing you're agonizing over, and not even remember it"),
    ("dead", "you only know your own voice from the outside through a recording you hate"),
    ("sleepy", "the friends you'd call at 3am, you never call at 3am"),
    ("peek", "you've walked past your future best friend dozens of times already"),
    ("shy", "you remember the embarrassing thing, they forgot it that same afternoon"),
    ("smug", "the advice you give easily is the advice you can't take"),
    ("sad", "everyone is the youngest sibling of something"),
    ("happy", "you can't tickle yourself, your own hand isn't a surprise to you"),
    ("music", "the song was about something specific and now it's about you"),
    ("think", "you've already met the last new person you'll ever love, or you haven't, and there's no way to tell which"),
    ("love", "people don't remember what you said, they remember what they had to feel to respond"),
    ("neutral", "you keep the receipt longer than the thing"),
    ("sleepy", "the room was always this quiet, you just brought noise to compare it to"),
    ("cool", "nobody grows up, they just get better at the costume"),
    ("surprised", "you'll reread this sentence and it'll mean something different because you will be different"),
    ("surprised", "the photo replaced the memory and you didn't notice the swap"),
    ("love", "you're nostalgic for days that were boring while you were in them"),
    ("cool", "you trust the map more than the road right in front of you"),
    ("cry", "you can love a place that wouldn't recognize you anymore"),
    ("sleepy", "half of maturity is just being too tired to react"),
    ("neutral", "you've forgotten more than most people will ever learn, and so has everyone"),
)


# --- Pure selection helpers ------------------------------------------------ #


def is_working(active: list[SessionSummary], now: float) -> bool:
    """True when any active session's transcript was written within the window.

    ``active`` is the ACTIVE-only session list the footer already builds; the
    state check is defensive in case a wider list is ever passed.
    """
    return any(
        s.state == ACTIVE and (now - s.last_write) < ACTIVE_WRITE_WINDOW
        for s in active
    )


def current_index(now: float) -> int:
    """The pool index for this tick; advances every ``ROTATE_SECONDS``."""
    return int(now // ROTATE_SECONDS) % len(MOODS)


def pick(now: float) -> tuple[str, str]:
    """The ``(mood, phrase)`` pair for this tick. Mood and phrase always come
    from the same entry, so the face and the bubble rotate in lockstep."""
    return MOODS[current_index(now)]


def accent(working: bool, now: float) -> str:
    """The trailing spinner glyph while working; empty while idle."""
    if not working:
        return ""
    return _SPINNER[int(now) % len(_SPINNER)]


# --- Renderables ----------------------------------------------------------- #


def render_bubble(phrase: str, width: int, color: str = BUBBLE_COLOR) -> Text:
    """A rounded white speech bubble hugging ``phrase``.

    Rounded corners with curved ``( )`` sides read as a balloon rather than a
    box; the bottom border carries a tail notch near the right, pointing down at
    the face parked beneath it. Long lines wrap to a readable width and the
    bubble grows as tall as needed.
    """
    maxw = max(12, min(56, width - 8))
    lines = textwrap.wrap(phrase, width=maxw) or [phrase]
    inner = max(cell_len(line) for line in lines)
    top = "╭" + "─" * (inner + 2) + "╮"
    mids = [
        "( " + line + " " * (inner - cell_len(line)) + " )" for line in lines
    ]
    left_dashes = max(1, inner - 1)
    bottom = "╰" + "─" * left_dashes + "┬" + "─" * 2 + "╯"
    return Text("\n".join((top, *mids, bottom)), style=f"bold {color}")


def face_text(working: bool, now: float) -> Text:
    """The big coral four-row face for this tick. While working, a spinner is
    tucked onto the bottom border so there is still live motion."""
    block = FACES[pick(now)[0]]
    if working:
        lines = block.split("\n")
        lines[-1] = lines[-1] + " " + accent(working, now)
        block = "\n".join(lines)
    return Text(block, style=f"bold {FACE_COLOR}")


def render_mood(
    active: list[SessionSummary],
    now: float | None = None,
    width: int = 80,
) -> Group:
    """Right-parked bubble stacked over the big face: the standalone mood render
    (used by the harness and tests). The footer composes the same pieces; see
    :func:`cc_token_tracker.roster._footer`.
    """
    if now is None:
        now = time.time()
    working = is_working(active, now)
    bubble = render_bubble(pick(now)[1], width)
    return Group(Align.right(bubble), Align.right(face_text(working, now)))


__all__ = [
    "ACTIVE_WRITE_WINDOW",
    "BAYMAX_BLUE",
    "BUBBLE_COLOR",
    "CLAUDE_CORAL",
    "FACE_COLOR",
    "FACES",
    "MOODS",
    "ROTATE_SECONDS",
    "accent",
    "current_index",
    "face_text",
    "is_working",
    "pick",
    "render_bubble",
    "render_mood",
]
