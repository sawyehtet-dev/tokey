"""Tests for cc_token_tracker.mood (multi-row mood faces + speech bubbles).

Everything in mood is pure given ``now``, so these pin the lockstep rotation,
the working spinner, the white bubble geometry, the four-row face heads, and a
vetting guard that keeps the curated pool free of offensive entries and em
dashes. The footer integration is checked through a non-terminal rich Console,
mirroring test_roster's snapshot style.
"""

import io
import unittest

from rich.cells import cell_len
from rich.console import Console, Group

from cc_token_tracker import mood
from cc_token_tracker.liveness import ACTIVE, CLOSING
from cc_token_tracker.roster import mood_enabled, render_roster
from cc_token_tracker.sessions import SessionSummary

NOW = 1_780_000_000.0


def make_summary(**overrides) -> SessionSummary:
    fields = dict(
        project="proj-a",
        file_name="s1.jsonl",
        total_tokens=12_300,
        total_cost_usd=0.042,
        unpriced=False,
        context_used=98_304,
        context_limit=200_000,
        context_percent=49.152,
        context_model="claude-opus-4-8",
        last_write=NOW,
        is_active=True,
    )
    fields.update(overrides)
    return SessionSummary(**fields)


class IsWorkingTests(unittest.TestCase):
    def test_recent_write_is_working(self):
        s = make_summary(last_write=NOW - 2.0, state=ACTIVE)
        self.assertTrue(mood.is_working([s], NOW))

    def test_stale_write_is_idle(self):
        s = make_summary(last_write=NOW - 20.0, state=ACTIVE)
        self.assertFalse(mood.is_working([s], NOW))

    def test_boundary(self):
        inside = make_summary(last_write=NOW - (mood.ACTIVE_WRITE_WINDOW - 0.1), state=ACTIVE)
        outside = make_summary(last_write=NOW - (mood.ACTIVE_WRITE_WINDOW + 0.1), state=ACTIVE)
        self.assertTrue(mood.is_working([inside], NOW))
        self.assertFalse(mood.is_working([outside], NOW))

    def test_empty_is_idle(self):
        self.assertFalse(mood.is_working([], NOW))

    def test_non_active_state_ignored(self):
        s = make_summary(last_write=NOW, state=CLOSING)
        self.assertFalse(mood.is_working([s], NOW))


class RotationTests(unittest.TestCase):
    def test_rotation_is_8_seconds(self):
        self.assertEqual(mood.ROTATE_SECONDS, 8.0)

    def test_pick_is_deterministic(self):
        self.assertEqual(mood.pick(NOW), mood.pick(NOW))

    def test_mood_and_phrase_are_paired_in_lockstep(self):
        for t in (0.0, 8.0, 50.0, 100.0):
            entry = mood.pick(t)
            self.assertIn(entry, mood.MOODS)

    def test_advances_on_its_period(self):
        self.assertEqual(mood.pick(0.0), mood.MOODS[0])
        self.assertEqual(mood.pick(mood.ROTATE_SECONDS - 0.01), mood.MOODS[0])
        self.assertEqual(mood.pick(mood.ROTATE_SECONDS), mood.MOODS[1])

    def test_index_wraps(self):
        n = len(mood.MOODS)
        mid = mood.ROTATE_SECONDS / 2
        self.assertEqual(mood.current_index(mid), 0)
        self.assertEqual(mood.current_index(n * mood.ROTATE_SECONDS + mid), 0)


class AccentTests(unittest.TestCase):
    def test_spinner_only_while_working(self):
        self.assertEqual(mood.accent(False, 0), "")
        self.assertIn(mood.accent(True, 0), mood._SPINNER)

    def test_spinner_advances_each_tick(self):
        self.assertNotEqual(mood.accent(True, 0), mood.accent(True, 1))


class FaceSetTests(unittest.TestCase):
    def test_every_mood_has_a_face(self):
        for key, _ in mood.MOODS:
            self.assertIn(key, mood.FACES)

    def test_faces_are_three_rows_of_equal_width(self):
        for key, block in mood.FACES.items():
            rows = block.split("\n")
            self.assertEqual(len(rows), 3, key)
            widths = {cell_len(r) for r in rows}
            self.assertEqual(len(widths), 1, f"{key} rows misaligned: {widths}")


class VettingTests(unittest.TestCase):
    """Guards the curated pool: clean, non-empty, unique, em-dash-free."""

    PHRASES = tuple(p for _, p in mood.MOODS)

    BANNED = (
        "nigg", "hitler", "sieg", "rape", "fuck", "cum", "penis",
        "gae", "milk?", "9/11", "8==", "certified gay", "roblox", "sigma",
    )

    def test_pool_size(self):
        self.assertGreaterEqual(len(mood.MOODS), 30)

    def test_no_empty_phrases(self):
        for phrase in self.PHRASES:
            self.assertTrue(phrase.strip())

    def test_phrases_unique(self):
        self.assertEqual(len(set(self.PHRASES)), len(self.PHRASES))

    def test_no_banned_content(self):
        for phrase in self.PHRASES:
            low = phrase.lower()
            for bad in self.BANNED:
                self.assertNotIn(bad, low, f"banned token {bad!r} in {phrase!r}")

    def test_no_em_dash(self):
        for phrase in self.PHRASES:
            self.assertNotIn("—", phrase, f"em dash in {phrase!r}")


class BubbleTests(unittest.TestCase):
    def test_bubble_has_borders_and_text(self):
        text = mood.render_bubble("kindness is mostly timing", 80).plain
        for ch in "╭╮╰╯┬()":
            self.assertIn(ch, text)
        self.assertIn("kindness is mostly timing", text)

    def test_bubble_is_white(self):
        bubble = mood.render_bubble("hi", 80)
        self.assertIn("white", str(bubble.style))

    def test_long_phrase_wraps_not_truncates(self):
        long = mood.MOODS[28][1]  # the longest phrase in the pool
        text = mood.render_bubble(long, 80).plain
        self.assertNotIn("…", text)
        for word in ("already", "person", "tell", "which"):
            self.assertIn(word, text)
        self.assertGreaterEqual(len(text.split("\n")), 4)

    def test_rows_stay_within_width(self):
        text = mood.render_bubble(mood.MOODS[28][1], 80).plain
        for line in text.split("\n"):
            self.assertLessEqual(cell_len(line), 80)


class FaceTests(unittest.TestCase):
    def test_face_text_is_baymax_blue_three_row_head(self):
        ft = mood.face_text(False, 0.0)
        self.assertEqual(ft.plain, mood.FACES[mood.MOODS[0][0]])
        self.assertEqual(len(ft.plain.split("\n")), 3)
        self.assertEqual(mood.FACE_COLOR, mood.BAYMAX_BLUE)
        self.assertTrue(mood.FACE_COLOR.startswith("#"))
        self.assertIn(mood.FACE_COLOR, str(ft.style))

    def test_face_text_tucks_spinner_while_working(self):
        ft = mood.face_text(True, 0.0)
        last = ft.plain.split("\n")[-1]
        self.assertIn(mood._SPINNER[0], last)

    def test_render_mood_is_a_group(self):
        g = mood.render_mood([make_summary(state=ACTIVE)], now=NOW, width=80)
        self.assertIsInstance(g, Group)


class FooterIntegrationTests(unittest.TestCase):
    def _render(self, *, mood_on: bool) -> str:
        buf = io.StringIO()
        console = Console(file=buf, width=100, force_terminal=False, no_color=True)
        panel = render_roster(
            [make_summary(state=ACTIVE)], width=100, now=NOW, mood=mood_on
        )
        console.print(panel)
        return buf.getvalue()

    def _eyes_row(self) -> str:
        return mood.FACES[mood.pick(NOW)[0]].split("\n")[1]

    def test_mood_on_shows_face_and_phrase(self):
        out = self._render(mood_on=True)
        self.assertIn(mood.pick(NOW)[1], out)
        self.assertIn(self._eyes_row(), out)
        self.assertIn("active:", out)

    def test_face_is_parked_right(self):
        out = self._render(mood_on=True)
        eyes = self._eyes_row()
        face_line = next(ln for ln in out.split("\n") if eyes in ln)
        self.assertNotIn("active:", face_line)
        self.assertGreater(face_line.index(eyes), 20)

    def test_mood_off_is_plain(self):
        out = self._render(mood_on=False)
        self.assertNotIn(mood.pick(NOW)[1], out)
        self.assertNotIn("┬", out)
        self.assertIn("active:", out)

    def test_mood_enabled_flag(self):
        self.assertTrue(mood_enabled([]))
        self.assertTrue(mood_enabled(["cc"]))
        self.assertFalse(mood_enabled(["--no-mood"]))


if __name__ == "__main__":
    unittest.main()
