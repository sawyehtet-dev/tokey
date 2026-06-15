"""Sprite data and half-block renderer for the --buddy mascot (cosmetic layer).

Render-side only: this module owns the cat's *pixels and palette*, kept separate
from the panel render code so the art is tunable without touching the layout.
The state brain (idle/working/stressed) lives in
:mod:`cc_token_tracker.companion`; this module just turns a pixel grid into rich
:class:`~rich.text.Text` lines.

Technique: each text cell is the upper-half block ``▀`` with the FOREGROUND set to
the top pixel's colour and the BACKGROUND to the bottom pixel's colour, so one
character cell carries two vertically stacked, roughly square pixels. A sprite
``W`` wide and ``H`` tall (``H`` even) renders in ``W`` columns and ``H / 2`` text
rows. A transparent pixel sets no colour on its half, so it shows through to the
panel background and the cat blends into the box.

The frames are CONVERTED from a reference sprite sheet, not free-drawn: the eight
hex values in :data:`PALETTE` are sampled straight off the sheet's palette swatch,
and each grid below is a hand-cleaned downsample of one frame off that sheet. The
art is AUTHORED WIDER THAN TALL (20x16, so 20 columns x 8 text rows) on purpose:
this terminal's half-block pixels are taller than wide, so a squashed grid
stretches back to round. Tune the proportion from a screenshot by editing the
plain rows here -- do not downsample at render time. Pixels are single chars keyed
into :data:`PALETTE`; ``" "`` is transparent.

Each mood is its own frame in :data:`FRAMES`, selectable by name. Only ``idle`` is
wired into the shipped ``--buddy`` output (see :mod:`cc_token_tracker.companion`);
the rest exist to be previewed and, once a trigger spec is approved, switched in.
The idle eyes are an OVERLAY (see :data:`EYE_COLS` / :func:`pixels`) so a blink or
a wide-eyed stress pose is a few-pixel change, never a redraw of the body; the
other frames bake their own expression (closed, surprised, asleep) into the grid.
"""

from __future__ import annotations

from rich.text import Text

__all__ = [
    "PALETTE",
    "FRAMES",
    "MOODS",
    "SPRITE_WIDTH",
    "SPRITE_ROWS",
    "EYE_COLS",
    "GLINT_COLS",
    "EYE_STATES",
    "pixels",
    "sprite_lines",
]

# char -> hex. The eight colours sampled from the reference sheet's palette
# swatch (the exact source of truth): orange body, cream belly/muzzle, dark brown
# outline/stripes, burnt orange for shading and grounding, pink for nose / inner
# ear / blush / heart, near-black eyes, white highlights (glint, sparkle, "!",
# "z"), and a neutral grey held in reserve. " " is transparent.
PALETTE = {
    "o": "#f08f33",   # orange (body)
    "c": "#f5deb8",   # cream (belly, chest, muzzle)
    "b": "#422b1b",   # dark brown (outline, stripes)
    "d": "#c35f25",   # burnt orange (shading, grounding)
    "p": "#eb8b83",   # pink (nose, inner ear, blush, heart)
    "x": "#131313",   # near-black (eyes)
    "w": "#fafafa",   # white (eye glint, sparkle, "!", "z")
    "g": "#5c5f62",   # grey (reserved)
}

_TRANSPARENT = " "
_UPPER = "▀"
_LOWER = "▄"

# IDLE row, frame 1 (front-facing sit): the default resting cat, drawn to match
# the reference hamster sprite. The eye rows (5, 6) are left plain orange here --
# the eyes are overlaid per state so the resting cat keeps its blink. Round head
# with a dark-brown forehead cap that dips to a point between the ears, pink-inner
# ears with brown tips, a small pink nose over a big cream chest, a short tail
# flicking up at the lower-right, and burnt-orange grounding at the feet.
_IDLE = [
    "   bb          bb   ",   # 0  ear tips (dark rim)
    "  bppo        oppb  ",   # 1  ears: brown rim, pink inner
    "  bppooobbbboooppb  ",   # 2  ear base; brown forehead cap
    " bdoooooobboooooodb ",   # 3  forehead cap dips to centre
    " boooooooooooooooob ",   # 4  forehead
    " boooooooooooooooob ",   # 5  eye band (overlaid)
    " boooooooooooooooob ",   # 6  eye band (overlaid)
    " boooocccppcccoooob ",   # 7  cream muzzle, pink nose
    "  boooccccccccooob  ",   # 8  muzzle / chest begins
    "  bdoooccccccccoodb ",   # 9  cream chest, burnt-orange flanks
    "   bdooccccccccodb  ",   # 10 chest
    "   booocccccccoob  o",   # 11 chest; tail tip flicks up
    "   booocccccccoob od",   # 12 belly; tail curl
    "   boocccccccccobod ",   # 13 belly / front paws; tail
    "   bdoccccccccoddo  ",   # 14 feet; tail base
    "    bbddooooddbb    ",   # 15 grounding
]

# EXTRAS sparkle frame: closed happy eyes, blushing, sparkle dots flanking.
_HAPPY = [
    "    bb        bb    ",
    "   bbpb      bpbb   ",
    "   bppbbbbbbbbppb   ",
    "   bbooobbbbooobb   ",
    "    booooooooooob   ",
    "o   boooccccooob   o",   # sparkle dots at the margins
    "    booxxooxxooob   ",   # closed ^^ eyes
    "    boooooooooob    ",
    "    boooccccooob    ",
    "    booccccccoob    ",
    "  o  boccccccob  o  ",   # sparkle dots
    "  b  bdoccccodb  b  ",
    "  bx  booccoob  xb  ",
    "   bdxooooooooxdb   ",
    "    bbdoooooodbb    ",
    "      bbxxxxbb      ",
]

# EXTRAS surprised frame: a "!" above, wide eyes, a small open mouth.
_SURPRISED = [
    "          w         ",   # ! stroke
    "          ww        ",   # ! base
    "   bbb        bbb   ",
    "  bbppb      bppbb  ",
    "  bppbbbbbbbbbbppb  ",
    "  booooobbbbooooob  ",
    " boooooooooooooooob ",
    " booowxxooooxxwooob ",   # wide eyes, white glint
    " booocxxooooxxcooob ",
    " booooocppccooooob  ",
    "  boooccccccccooob  ",
    "  bdooccoxxocccoodb ",   # small open mouth
    "   bdooccccccoodb   ",
    "   boooccccccoob    ",
    "   bdocccccccodb    ",
    "    bbddooooddbb    ",
]

# EXTRAS heart frame: face turned, one eye, a raised paw, a heart upper-right.
_AFFECTION = [
    "  bbb               ",
    " bbppb        pddp  ",   # heart top
    " bppbbbbbb   pddddpp",
    " bxoooobooob ddddddd",
    " booooooooob  ddddd ",
    "boooooooooob   ddd  ",
    "boxwoooxooob    d   ",   # eye with glint
    "boxxoooxooob        ",
    "booccppcooob        ",
    "bocccccccoob   bcb  ",   # raised paw
    " bccccccccob  bcccb ",
    " bdooccccdb   boccb ",
    "  bbocccoddb  boob  ",
    "   booccoooddboob   ",
    "   bdocoooodddb     ",
    "    bbdoooodbb      ",
]

# EXTRAS sleeping frame: lying down, eyes shut, "z" marks drifting up-right.
_SLEEPING = [
    "                    ",
    "                 w  ",   # z marks
    "                ww  ",
    "               ww   ",
    "            ww      ",
    "           ww       ",
    "                    ",
    "  bbbbb      bbb    ",   # ear / back
    " bppbbboooobbpb     ",   # pink ear
    " bpbooooooooobbbbb  ",
    " booooooooooooooodb ",
    "boocccooooooboooodob",   # cream cheek, tail
    "boccpccooooobdoooodb",   # pink nose
    "bocccccooooobdoooob ",
    " bocccccoddbboooobb ",
    "  bbxxxxbb  bbbbb   ",
]

FRAMES = {
    "idle": _IDLE,
    "happy": _HAPPY,
    "surprised": _SURPRISED,
    "affection": _AFFECTION,
    "sleeping": _SLEEPING,
}
MOODS = tuple(FRAMES)

SPRITE_WIDTH = len(_IDLE[0])      # 20
SPRITE_ROWS = len(_IDLE) // 2     # 8 text rows

# The idle eyes are overlaid on the plain orange eye band so the resting cat can
# blink without a body redraw. Two 2px-wide eyes (left cols 5-6, right cols
# 13-14) drawn in the near-black eye colour, each with a white catch-light glint
# on its inner-top pixel. Per state, the rows that light up: open is 2px, blink
# collapses to the lower line (a calm closed eye), wide adds a row up into the
# forehead for a startled stress pose. The glint sits on the top row of the lit
# eye (None for a blink). Only the idle frame is overlaid; the other frames bake
# their expression into the grid.
EYE_COLS = (5, 6, 13, 14)
GLINT_COLS = (6, 13)
EYE_STATES = {
    "open": (5, 6),
    "blink": (6,),
    "wide": (4, 5, 6),
}
_GLINT_ROW = {"open": 5, "blink": None, "wide": 4}


def pixels(eye_state: str = "open", *, mood: str = "idle") -> list[list[str]]:
    """The full pixel grid for a mood (and, for ``idle``, an eye state).

    Returns a fresh grid so callers never alias the frame data. For ``idle`` the
    eyes (and their glint) are overlaid per ``eye_state``; every other frame bakes
    its own expression, so ``eye_state`` is ignored there. Pure.
    """
    grid = [list(row) for row in FRAMES[mood]]
    if mood == "idle":
        for r in EYE_STATES[eye_state]:
            for c in EYE_COLS:
                grid[r][c] = "x"
        glint_row = _GLINT_ROW[eye_state]
        if glint_row is not None:
            for c in GLINT_COLS:
                grid[glint_row][c] = "w"
    return grid


def sprite_lines(eye_state: str = "open", *, mood: str = "idle") -> list[Text]:
    """Render a frame to ``SPRITE_ROWS`` rich Text lines via ``▀`` half-blocks.

    Each cell pairs two stacked pixels: foreground = top colour, background =
    bottom colour. A transparent half leaves that side unset so the panel
    background shows through (the cat blends into the box). ``eye_state`` only
    affects the ``idle`` frame (see :func:`pixels`). Pure.
    """
    grid = pixels(eye_state, mood=mood)
    lines: list[Text] = []
    for r in range(0, len(grid), 2):
        top, bottom = grid[r], grid[r + 1]
        text = Text(no_wrap=True)
        for c in range(SPRITE_WIDTH):
            tc = PALETTE.get(top[c])
            bc = PALETTE.get(bottom[c])
            if tc is None and bc is None:
                text.append(" ")
            elif tc is not None and bc is not None:
                text.append(_UPPER, style=f"{tc} on {bc}")
            elif tc is not None:
                text.append(_UPPER, style=tc)
            else:
                text.append(_LOWER, style=bc)
        lines.append(text)
    return lines
