"""Tests for cc_token_tracker.companion (the mascot's state brain + entry point).

The mood map (idle / working / stressed) and its precedence are pinned with
hand-built RosterViews, no IO. The render side is checked for its fixed
height, the mood->eye-state mapping, the idle blink off the frame counter, and
the roster wiring: the cat parks bottom-right, never widens the box, never
overlaps the active total, and reserves a fixed number of rows.
"""

import io
import unittest

from rich.console import Console

from cc_token_tracker import companion
from cc_token_tracker.companion import (
    BLINK_EVERY,
    BUDDY_ROWS,
    MOOD_IDLE,
    MOOD_STRESSED,
    MOOD_WORKING,
    STREAM_FRESH_SECONDS,
    eye_state,
    mood,
    render_buddy,
)
from cc_token_tracker.liveness import ACTIVE, CLOSING
from cc_token_tracker.roster import RosterView, buddy_requested, render_roster
from cc_token_tracker.sessions import SessionSummary
from cc_token_tracker.usage import AccountUsage, Credits, UsageWindow

NOW = 1_780_000_000.0


def make_summary(**overrides):
    fields = dict(
        project="proj-a",
        file_name="s1.jsonl",
        total_tokens=123_456,
        total_cost_usd=1.2345,
        unpriced=False,
        context_used=98_304,
        context_limit=200_000,
        context_percent=49.0,
        context_model="claude-opus-4-8",
        last_write=NOW - 240,
        is_active=False,
        state=ACTIVE,
    )
    fields.update(overrides)
    return SessionSummary(**fields)


def view(*summaries):
    active = sum(1 for s in summaries if s.state == ACTIVE)
    return RosterView(sessions=list(summaries), active_count=active)


def usage(**windows):
    fields = dict(plan="pro", session=None, weekly=None)
    fields.update(windows)
    return AccountUsage(**fields)


def styled(group):
    """A full (glyph, style) signature of a rendered buddy Group, so renders that
    differ only by colour (e.g. an eye recolour) still compare unequal."""
    out = []
    for line in group.renderables:
        row = []
        for i, ch in enumerate(line.plain):
            style = next((str(s.style) for s in line.spans if s.start <= i < s.end), "")
            row.append((ch, style))
        out.append(tuple(row))
    return tuple(out)


class MoodMap(unittest.TestCase):
    def test_empty_roster_is_idle(self):
        self.assertEqual(mood(view(), now=NOW), MOOD_IDLE)

    def test_quiet_session_is_idle(self):
        s = make_summary(last_write=NOW - 120, context_percent=40.0)
        self.assertEqual(mood(view(s), now=NOW), MOOD_IDLE)

    def test_fresh_write_is_working(self):
        s = make_summary(last_write=NOW - 1.0, context_percent=40.0)
        self.assertEqual(mood(view(s), now=NOW), MOOD_WORKING)

    def test_streaming_window_is_half_open_upper(self):
        at = make_summary(last_write=NOW - STREAM_FRESH_SECONDS)
        self.assertEqual(mood(view(at), now=NOW), MOOD_IDLE)
        inside = make_summary(last_write=NOW - (STREAM_FRESH_SECONDS - 0.01))
        self.assertEqual(mood(view(inside), now=NOW), MOOD_WORKING)

    def test_fresh_write_on_closing_session_is_not_working(self):
        s = make_summary(last_write=NOW - 1.0, state=CLOSING, context_percent=40.0)
        self.assertEqual(mood(view(s), now=NOW), MOOD_IDLE)

    def test_high_context_is_stressed(self):
        s = make_summary(last_write=NOW - 120, context_percent=92.0)
        self.assertEqual(mood(view(s), now=NOW), MOOD_STRESSED)

    def test_over_100_context_is_stressed(self):
        s = make_summary(last_write=NOW - 120, context_percent=104.0)
        self.assertEqual(mood(view(s), now=NOW), MOOD_STRESSED)

    def test_unknown_context_does_not_stress(self):
        s = make_summary(last_write=NOW - 120, context_percent=None)
        self.assertEqual(mood(view(s), now=NOW), MOOD_IDLE)

    def test_high_usage_window_is_stressed(self):
        s = make_summary(last_write=NOW - 120, context_percent=10.0)
        u = usage(weekly=UsageWindow(utilization=95.0, resets_at=None))
        self.assertEqual(mood(view(s), now=NOW, usage=u), MOOD_STRESSED)

    def test_high_credits_is_stressed(self):
        s = make_summary(last_write=NOW - 120, context_percent=10.0)
        u = usage(credits=Credits(enabled=True, utilization=99.0, used=9.9,
                                  limit=10.0, currency="USD"))
        self.assertEqual(mood(view(s), now=NOW, usage=u), MOOD_STRESSED)

    def test_null_credit_utilization_does_not_stress(self):
        s = make_summary(last_write=NOW - 120, context_percent=10.0)
        u = usage(credits=Credits(enabled=True, utilization=None, used=1.0,
                                  limit=10.0, currency="USD"))
        self.assertEqual(mood(view(s), now=NOW, usage=u), MOOD_IDLE)

    def test_low_usage_does_not_stress(self):
        s = make_summary(last_write=NOW - 120, context_percent=10.0)
        u = usage(weekly=UsageWindow(utilization=31.0, resets_at=None))
        self.assertEqual(mood(view(s), now=NOW, usage=u), MOOD_IDLE)

    def test_stressed_beats_working(self):
        s = make_summary(last_write=NOW - 1.0, context_percent=95.0)
        self.assertEqual(mood(view(s), now=NOW), MOOD_STRESSED)


class EyeState(unittest.TestCase):
    def test_stressed_is_wide(self):
        self.assertEqual(eye_state(MOOD_STRESSED, 0), "wide")
        self.assertEqual(eye_state(MOOD_STRESSED, BLINK_EVERY), "wide")

    def test_blink_on_the_period(self):
        self.assertEqual(eye_state(MOOD_IDLE, BLINK_EVERY), "blink")
        self.assertEqual(eye_state(MOOD_WORKING, BLINK_EVERY * 3), "blink")

    def test_open_off_the_period(self):
        self.assertEqual(eye_state(MOOD_IDLE, 1), "open")
        self.assertEqual(eye_state(MOOD_WORKING, BLINK_EVERY + 1), "open")


class RenderBuddy(unittest.TestCase):
    def test_fixed_height(self):
        for v in (view(make_summary(last_write=NOW - 1.0)),
                  view(make_summary(context_percent=95.0)),
                  view(make_summary(last_write=NOW - 300))):
            self.assertEqual(len(render_buddy(v, now=NOW).renderables), BUDDY_ROWS)

    def test_stressed_render_differs_from_calm(self):
        calm = render_buddy(view(make_summary(last_write=NOW - 300)), now=NOW, frame=1)
        tense = render_buddy(view(make_summary(context_percent=95.0)), now=NOW, frame=1)
        self.assertNotEqual(styled(calm), styled(tense))

    def test_blink_frame_differs_from_open(self):
        v = view(make_summary(last_write=NOW - 300))
        blink = render_buddy(v, now=NOW, frame=BLINK_EVERY)   # blink
        openf = render_buddy(v, now=NOW, frame=1)             # open
        self.assertNotEqual(styled(blink), styled(openf))


class OptIn(unittest.TestCase):
    def test_flag_enables(self):
        self.assertTrue(buddy_requested(["--buddy"], env={}))

    def test_env_truthy_enables(self):
        for val in ("1", "true", "TRUE", "yes", "on"):
            self.assertTrue(buddy_requested([], env={"TOKEY_BUDDY": val}))

    def test_off_by_default(self):
        self.assertFalse(buddy_requested([], env={}))
        self.assertFalse(buddy_requested(["cc"], env={"TOKEY_BUDDY": "0"}))
        self.assertFalse(buddy_requested([], env={"TOKEY_BUDDY": "no"}))


class RosterWiring(unittest.TestCase):
    # The half block is unique to the mascot (the panel's bars are full blocks),
    # so it is a clean "the cat is here" sentinel.
    CAT = "▀"

    def render(self, summaries=None, *, width=100, **kwargs):
        if summaries is None:
            summaries = [make_summary()]
        panel = render_roster(summaries, width=width, now=NOW, **kwargs)
        console = Console(width=width, file=io.StringIO(), force_terminal=False)
        console.print(panel)
        return console.file.getvalue()

    def test_no_sprite_without_buddy_frame(self):
        self.assertNotIn(self.CAT, self.render())

    def test_sprite_present_with_buddy_frame(self):
        out = self.render(buddy_frame=1)
        self.assertIn(self.CAT, out)
        self.assertIn("active:", out)  # the total is not overlapped

    def test_box_width_unchanged_by_buddy(self):
        plain = self.render(width=100)
        withcat = self.render(width=100, buddy_frame=1)
        self.assertEqual(max(map(len, plain.splitlines())),
                         max(map(len, withcat.splitlines())))

    def test_buddy_reserves_fixed_rows(self):
        # The band is BUDDY_ROWS tall in place of the 1-line footer, regardless
        # of the roster, so the panel height is stable.
        plain = self.render().count("\n")
        withcat = self.render(buddy_frame=1).count("\n")
        self.assertEqual(withcat - plain, BUDDY_ROWS - 1)

    def test_buddy_reacts_to_view(self):
        fresh = self.render([make_summary(last_write=NOW - 1.0)], buddy_frame=1)
        stale = self.render([make_summary(last_write=NOW - 300)], buddy_frame=1)
        self.assertIn(self.CAT, fresh)
        self.assertIn(self.CAT, stale)


if __name__ == "__main__":
    unittest.main()
