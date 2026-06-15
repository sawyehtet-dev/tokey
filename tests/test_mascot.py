"""Tests for cc_token_tracker.mascot (sprite data + half-block renderer).

Pure throughout: the pixel grid and the ``▀``/``▄`` conversion are exercised
directly, so the sprite's dimensions, its block-glyph-only output, its flat
palette, and transparency (a clear pixel sets no colour and shows the panel
through) are all pinned without rendering a panel.
"""

import unittest

from rich.cells import cell_len

from cc_token_tracker import mascot

# The renderer only ever emits these glyphs: the two half blocks plus space.
HALF_GLYPHS = set(" ▀▄")


def cell_styles(line):
    """Every style string attached to a non-blank cell of a rendered Text line."""
    return [str(s.style) for s in line.spans if line.plain[s.start:s.end].strip()]


class SpriteData(unittest.TestCase):
    def test_palette_is_eight_named_colours(self):
        self.assertEqual(set(mascot.PALETTE),
                         {"o", "c", "b", "d", "p", "x", "w", "g"})
        for hexcolour in mascot.PALETTE.values():
            self.assertRegex(hexcolour, r"^#[0-9a-fA-F]{6}$")

    def test_base_grid_is_rectangular(self):
        grid = mascot.pixels("open")
        self.assertEqual(len(grid), mascot.SPRITE_ROWS * 2)
        for row in grid:
            self.assertEqual(len(row), mascot.SPRITE_WIDTH)

    def test_every_frame_is_rectangular(self):
        for mood, frame in mascot.FRAMES.items():
            self.assertEqual(len(frame), mascot.SPRITE_ROWS * 2, msg=mood)
            for row in frame:
                self.assertEqual(len(row), mascot.SPRITE_WIDTH, msg=f"{mood}:{row!r}")

    def test_grid_chars_are_palette_or_transparent(self):
        allowed = set(mascot.PALETTE) | {" "}
        for mood in mascot.FRAMES:
            for row in mascot.pixels(mood=mood):
                self.assertTrue(set(row) <= allowed, msg=f"{mood}:{''.join(row)!r}")

    def test_eyes_are_eye_colour_with_white_glint(self):
        # The idle eye cells are the near-black eye colour, except the catch-light
        # glint, which is white and sits on the eye's top row.
        grid = mascot.pixels("open")
        glint_row = mascot._GLINT_ROW["open"]
        for r in mascot.EYE_STATES["open"]:
            for c in mascot.EYE_COLS:
                expected = "w" if (r == glint_row and c in mascot.GLINT_COLS) else "x"
                self.assertEqual(grid[r][c], expected, msg=f"({r},{c})")
        # The glint really is present (so the eyes have a highlight, not just dark).
        self.assertTrue(any(grid[glint_row][c] == "w" for c in mascot.GLINT_COLS))

    def test_eye_overlay_is_idle_only(self):
        # A non-idle frame is returned verbatim: the eye overlay never touches it.
        self.assertEqual(mascot.pixels("open", mood="happy"),
                         [list(row) for row in mascot.FRAMES["happy"]])

    def test_pixels_does_not_alias_base(self):
        # A returned grid is a fresh copy; mutating it must not leak into the next.
        g1 = mascot.pixels("open")
        g1[0][0] = "o"
        g2 = mascot.pixels("open")
        self.assertEqual(g2[0][0], " ")


class HalfBlockRender(unittest.TestCase):
    def test_dimensions(self):
        lines = mascot.sprite_lines("open")
        self.assertEqual(len(lines), mascot.SPRITE_ROWS)
        for line in lines:
            self.assertEqual(cell_len(line.plain), mascot.SPRITE_WIDTH)

    def test_every_frame_renders_to_block_glyphs(self):
        for mood in mascot.FRAMES:
            lines = mascot.sprite_lines(mood=mood)
            self.assertEqual(len(lines), mascot.SPRITE_ROWS, msg=mood)
            for line in lines:
                self.assertEqual(cell_len(line.plain), mascot.SPRITE_WIDTH, msg=mood)
                self.assertTrue(set(line.plain) <= HALF_GLYPHS, msg=f"{mood}:{line.plain!r}")

    def test_only_half_block_glyphs(self):
        for state in mascot.EYE_STATES:
            for line in mascot.sprite_lines(state):
                self.assertTrue(set(line.plain) <= HALF_GLYPHS, msg=repr(line.plain))

    def test_only_palette_colours_used(self):
        # Every colour named in any cell style is a palette hex (fg, or fg on bg).
        palette = set(mascot.PALETTE.values())
        for state in mascot.EYE_STATES:
            for line in mascot.sprite_lines(state):
                for style in cell_styles(line):
                    for token in style.replace(" on ", " ").split():
                        self.assertIn(token, palette, msg=f"{style!r}")

    def test_transparent_pixels_are_unstyled_spaces(self):
        # The wide margins are space with no style, so the panel shows through.
        line = mascot.sprite_lines("open")[0]
        self.assertIn(" ", line.plain)
        for span in line.spans:
            if line.plain[span.start:span.end] == " ":
                self.assertFalse(str(span.style).strip(), msg="space should be unstyled")

    def test_eye_states_change_the_render(self):
        # open / blink / wide differ in the styled output (the eye cells recolour
        # even though the glyph stays a half block).
        def sig(state):
            return [[(line.plain[i], str(s.style))
                     for line in mascot.sprite_lines(state)
                     for s in line.spans if s.start <= i < s.end]
                    for i in range(mascot.SPRITE_WIDTH)]
        self.assertNotEqual(sig("open"), sig("blink"))
        self.assertNotEqual(sig("open"), sig("wide"))
        self.assertNotEqual(sig("blink"), sig("wide"))


if __name__ == "__main__":
    unittest.main()
